"""
Mindees node daemon + JSON-RPC API  --  Phase 6.

One runnable process that ties the whole stack together:
  BlockStore (P5) -> chain (P1) + PoS (P2) + Mempool (P3), reachable over a small
  JSON-RPC HTTP API that a wallet (or another tool) can call.

  python node.py init  --data ./chaindata --to <ADDRESS> --stake 100000
  python node.py serve --data ./chaindata --validator-secret <HEX> --autoproduce

Design (ponytail): stdlib http.server, no web framework, no async. The RPC surface is
a flat method table over NodeService; everything it returns is already JSON. Block
production is gated on actually being the elected validator -- the node cannot mint a
block out of turn even if you ask it to.

Self-testing: run directly ->  python node.py
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from consensus import seal_block
from core import COIN, DECIMALS, MAX_SUPPLY_UNITS, NAME, SYMBOL, ValidationError, Wallet
from forkchoice import BlockTree
from mempool import Mempool
from network import decode_block, decode_tx, encode_block
from storage import BlockStore


class NodeService:
    def __init__(
        self,
        store: BlockStore,
        validator_wallet: Optional[Wallet] = None,
        autoproduce: bool = False,
    ) -> None:
        self.store = store
        # Fork choice + finality live in the block tree; the store is just durable bytes.
        # Rebuild the tree by replaying every persisted block (handles forks, unlike a
        # linear load), so finalized state is reconstructed deterministically on boot.
        self.tree = BlockTree(*store.genesis_params())
        for block in store.iter_blocks():
            self.tree.add_block(block)
        self.mempool = Mempool()
        self.validator = validator_wallet
        self.autoproduce = autoproduce

    @property
    def chain(self):
        """The canonical chain selected by fork choice (respecting finality)."""
        return self.tree.canonical

    # -- read methods ------------------------------------------------------ #
    def info(self, _params=None) -> dict:
        return {
            "name": NAME,
            "symbol": SYMBOL,
            "decimals": DECIMALS,
            "height": self.chain.head.index,
            "head": self.chain.head.hash,
            "finalized_height": self.tree.finalized_height,
            "finalized": self.tree.finalized_hash,
            "supply": self.chain.total_supply(),
            "max_supply": MAX_SUPPLY_UNITS,
            "mempool": len(self.mempool),
            "next_validator": self._safe_next_validator(),
        }

    def balance(self, params) -> int:
        return self.chain.balance_of(params["address"])

    def nonce(self, params) -> int:
        return self.chain.nonces.get(params["address"], 0)

    def stake(self, params) -> int:
        return self.chain.stake_of(params["address"])

    def spendable(self, params) -> int:
        return self.chain.spendable_of(params["address"])

    def locked(self, params) -> int:
        return self.chain.locked_of(params["address"])

    def get_block(self, params) -> dict:
        index = params["index"]
        if not 0 <= index < len(self.chain.chain):
            raise ValidationError(f"no block at index {index}")
        return encode_block(self.chain.chain[index])

    # -- write methods ----------------------------------------------------- #
    def submit_tx(self, params) -> dict:
        tx = decode_tx(params["tx"])
        self.mempool.add(tx, self.chain)  # raises ValidationError if invalid
        result = {"txid": tx.txid, "mined": None}
        if self.autoproduce and self._is_our_turn():
            result["mined"] = self.produce_block({})
        return result

    def produce_block(self, params) -> dict:
        if self.validator is None:
            raise ValidationError("node is not configured as a validator")
        if not self._is_our_turn():
            raise ValidationError(f"not this node's turn (elected {self.chain.next_validator()})")
        timestamp = (params or {}).get("timestamp") or int(time.time())
        txs = self.mempool.select(self.chain)
        block = seal_block(self.tree.head.hash, self.tree.head.index + 1, self.validator, txs, timestamp)
        self.tree.add_block(block)             # fork choice + finality + validation
        self.store.append(block)               # durable before we report success
        self.mempool.update(self.chain)
        return encode_block(block)

    def receive_block(self, params) -> dict:
        """Accept a block gossiped from a peer: validate via the tree, persist if new."""
        block = decode_block(params["block"])
        accepted = self.tree.add_block(block)  # raises on invalid/orphan/finality conflict
        if accepted:
            self.store.append(block)
            self.mempool.update(self.chain)
        return {
            "accepted": accepted,
            "height": self.tree.head.index,
            "head": self.tree.head.hash,
            "finalized_height": self.tree.finalized_height,
        }

    # -- helpers ----------------------------------------------------------- #
    def _is_our_turn(self) -> bool:
        return self.validator is not None and self.chain.next_validator() == self.validator.address

    def _safe_next_validator(self) -> Optional[str]:
        try:
            return self.chain.next_validator()
        except ValidationError:
            return None  # no active validators yet


# --------------------------------------------------------------------------- #
# JSON-RPC over HTTP
# --------------------------------------------------------------------------- #
_WRITE_METHODS = {"submit_tx", "receive_block"}  # state-changing -> require auth when a token is set
_MAX_RPC_BODY = 8_000_000


class _RPCHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        request_id = None
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > _MAX_RPC_BODY:
                self._reply({"id": None, "error": "request too large"})
                return
            req = json.loads(self.rfile.read(length) or b"{}")
            request_id = req.get("id")
            method = req["method"]
            if method in _WRITE_METHODS and self.server.rpc_token:
                auth = self.headers.get("Authorization", "")
                token = auth[7:] if auth.startswith("Bearer ") else ""
                if not hmac.compare_digest(token, self.server.rpc_token):
                    self._reply({"id": request_id, "error": "unauthorized"})
                    return
            self._reply({"id": request_id, "result": self.server.dispatch(method, req.get("params") or {})})
        except Exception as exc:  # any failure -> JSON error, never a 500 stack trace
            self._reply({"id": request_id, "error": str(exc)})

    def _reply(self, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:  # keep the node quiet
        pass


class JSONRPCServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, service: NodeService, host: str = "127.0.0.1", port: int = 0, token=None):
        super().__init__((host, port), _RPCHandler)
        self.service = service
        self.rpc_token = token  # if set, write methods require a matching Bearer token
        # NOTE: produce_block is intentionally NOT exposed over RPC -- block production is
        # driven only by the internal validator loop, so no client can mint out of turn / MEV.
        self.methods = {
            "info": service.info,
            "balance": service.balance,
            "nonce": service.nonce,
            "stake": service.stake,
            "spendable": service.spendable,
            "locked": service.locked,
            "get_block": service.get_block,
            "submit_tx": service.submit_tx,
            "receive_block": service.receive_block,
        }

    def dispatch(self, method: str, params: dict):
        fn = self.methods.get(method)
        if fn is None:
            raise ValidationError(f"unknown method: {method}")
        return fn(params)


def rpc_call(url: str, method: str, token=None, **params):
    body = json.dumps({"method": method, "params": params, "id": 1}).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=5) as resp:
        payload = json.loads(resp.read().decode())
    if payload.get("error"):
        raise RuntimeError(payload["error"])
    return payload["result"]


# --------------------------------------------------------------------------- #
# CLI entrypoint
# --------------------------------------------------------------------------- #
def _cli(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="node", description="Mindees node")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="create genesis allocating full supply to one address")
    p_init.add_argument("--data", required=True)
    p_init.add_argument("--to", required=True, help="address that receives all 1,000,000 MIND")
    p_init.add_argument("--stake", type=int, default=0, help="coins that address stakes to validate")
    p_init.add_argument("--timestamp", type=int, default=1_700_000_000)

    p_serve = sub.add_parser("serve", help="run the node + JSON-RPC server")
    p_serve.add_argument("--data", required=True)
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8645)
    p_serve.add_argument("--validator-secret", default=None, help="hex secret (prefer --validator-keystore)")
    p_serve.add_argument("--validator-keystore", default=None,
                         help="encrypted validator keystore ($MINDEES_PASSPHRASE)")
    p_serve.add_argument("--autoproduce", action="store_true", help="mine a block on each tx")

    args = parser.parse_args(argv)

    if args.cmd == "init":
        store = BlockStore(args.data)
        store.write_genesis(
            allocations={args.to: MAX_SUPPLY_UNITS},
            initial_stakes={args.to: args.stake * COIN} if args.stake else {},
            timestamp=args.timestamp,
        )
        print(f"genesis written to {args.data}: {MAX_SUPPLY_UNITS // COIN:,} {SYMBOL} -> {args.to}")
        if args.stake:
            print(f"  staked {args.stake:,} {SYMBOL} as validator")
        return

    if args.cmd == "serve":
        store = BlockStore(args.data)
        secret = args.validator_secret
        if args.validator_keystore:
            from keystore import decrypt_secret, load_keystore
            passphrase = os.environ.get("MINDEES_PASSPHRASE")
            if not passphrase:
                raise SystemExit("set $MINDEES_PASSPHRASE to unlock the validator keystore")
            secret = decrypt_secret(load_keystore(args.validator_keystore), passphrase)
        validator = Wallet.from_secret(int(secret, 16)) if secret else None

        token = os.environ.get("MINDEES_RPC_TOKEN")
        # A reachable RPC with no auth token would let anyone submit txs / push blocks.
        is_loopback = args.host in ("127.0.0.1", "localhost", "::1")
        if not is_loopback and not token:
            raise SystemExit("refusing to serve a non-loopback RPC without $MINDEES_RPC_TOKEN")

        service = NodeService(store, validator, autoproduce=args.autoproduce)
        server = JSONRPCServer(service, args.host, args.port, token=token)
        info = service.info()
        print(f"{NAME} node serving on http://{args.host}:{args.port}/  height={info['height']}  "
              f"auth={'on' if token else 'off (loopback)'}")
        if validator:
            print(f"  validator={validator.address} autoproduce={args.autoproduce}")
        server.serve_forever()


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #
def _demo() -> None:
    import shutil
    import tempfile
    import threading

    from core import Transaction

    alice, bob = Wallet.from_secret(1), Wallet.from_secret(2)
    v1 = Wallet.from_secret(3)  # the single validator -> always elected

    tmp = tempfile.mkdtemp(prefix="mindees_node_")
    server = None
    try:
        store = BlockStore(tmp)
        store.write_genesis(
            allocations={alice.address: MAX_SUPPLY_UNITS - 1000 * COIN, v1.address: 1000 * COIN},
            initial_stakes={v1.address: 1000 * COIN},
            timestamp=1_700_000_000,
        )
        service = NodeService(store, validator_wallet=v1)
        server = JSONRPCServer(service)
        host, port = server.server_address
        url = f"http://{host}:{port}/"
        threading.Thread(target=server.serve_forever, daemon=True).start()

        # Read API (now tree-backed, with a finality frontier).
        info = rpc_call(url, "info")
        assert info["height"] == 0 and info["supply"] == MAX_SUPPLY_UNITS
        assert info["next_validator"] == v1.address
        assert info["finalized_height"] == 0  # genesis finalized by definition

        # Submit a signed transfer through the RPC.
        n = rpc_call(url, "nonce", address=alice.address)
        from network import encode_tx
        tx = Transaction(alice.address, bob.address, 100 * COIN, 1 * COIN, n).sign(alice)
        res = rpc_call(url, "submit_tx", tx=encode_tx(tx))
        assert res["txid"] == tx.txid

        # produce_block is NOT an RPC method (production is internal-only) -> client can't mint.
        try:
            rpc_call(url, "produce_block", timestamp=1)
            raise AssertionError("produce_block must not be exposed over RPC")
        except RuntimeError:
            pass
        # The validator produces a block internally; the transfer settles.
        block = service.produce_block({"timestamp": 1_700_000_010})
        assert block["index"] == 1
        assert rpc_call(url, "balance", address=bob.address) == 100 * COIN
        assert rpc_call(url, "info")["supply"] == MAX_SUPPLY_UNITS

        # It was persisted: a brand-new service over the same data rebuilds the tree to height 1.
        assert store.height() == 1
        restarted = NodeService(store)
        assert restarted.chain.head.index == 1
        assert restarted.chain.balance_of(bob.address) == 100 * COIN

        # Gossip: a second node on a fresh store with the same genesis accepts the block.
        import shutil

        tmp2 = tempfile.mkdtemp(prefix="mindees_node2_")
        try:
            store2 = BlockStore(tmp2)
            store2.write_genesis(
                allocations={alice.address: MAX_SUPPLY_UNITS - 1000 * COIN, v1.address: 1000 * COIN},
                initial_stakes={v1.address: 1000 * COIN},
                timestamp=1_700_000_000,
            )
            node2 = NodeService(store2)
            res2 = node2.receive_block({"block": block})
            assert res2["accepted"] and res2["height"] == 1
            assert node2.chain.balance_of(bob.address) == 100 * COIN
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)

        # Invalid input is reported as a clean RPC error, not a crash.
        forged = Transaction(alice.address, bob.address, 1 * COIN, 0, 99)
        forged.public_key = bob.public_key_hex
        forged.signature = bob.sign(forged._signing_payload()).hex()
        try:
            rpc_call(url, "submit_tx", tx=encode_tx(forged))
            raise AssertionError("forged tx should be rejected by the node")
        except RuntimeError:
            pass

        # Producing out of turn is refused (bob is not the elected validator).
        bob_service = NodeService(store, validator_wallet=bob)
        try:
            bob_service.produce_block({})
            raise AssertionError("non-elected node should not produce a block")
        except ValidationError:
            pass

        # RPC auth: a token-protected server rejects an unauthenticated write, accepts the read.
        tmp3 = tempfile.mkdtemp(prefix="mindees_auth_")
        auth_server = None
        try:
            s3 = BlockStore(tmp3)
            s3.write_genesis(
                allocations={alice.address: MAX_SUPPLY_UNITS - 1000 * COIN, v1.address: 1000 * COIN},
                initial_stakes={v1.address: 1000 * COIN}, timestamp=1_700_000_000,
            )
            auth_server = JSONRPCServer(NodeService(s3), token="sekret")
            ahost, aport = auth_server.server_address
            aurl = f"http://{ahost}:{aport}/"
            threading.Thread(target=auth_server.serve_forever, daemon=True).start()
            t2 = Transaction(alice.address, bob.address, 1 * COIN, 0, 0).sign(alice)
            try:
                rpc_call(aurl, "submit_tx", tx=encode_tx(t2))           # no token
                raise AssertionError("write without token must be unauthorized")
            except RuntimeError:
                pass
            assert rpc_call(aurl, "info")["height"] == 0                # reads need no token
            assert rpc_call(aurl, "submit_tx", token="sekret", tx=encode_tx(t2))["txid"] == t2.txid
        finally:
            if auth_server is not None:
                auth_server.shutdown()
                auth_server.server_close()
            shutil.rmtree(tmp3, ignore_errors=True)

        print("ALL CHECKS PASSED")
        print("  node daemon: JSON-RPC read/write, validator-gated production, persisted")
        print(f"  served {NAME} on {url} height -> {rpc_call(url, 'info')['height']}")
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        _cli()
    else:
        _demo()
