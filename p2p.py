"""
Mindees TCP peer mesh  --  the live multi-node layer.

Wraps a NodeService with a real socket so blocks and transactions gossip across a network
instead of being handed over in-process. Each node listens on a TCP port, relays new
messages to its peers, and drops what it has already seen (so relays don't loop). Block
validation, fork choice, and finality all live in NodeService.tree -- this layer is pure
transport, exactly like the in-process gossip in network.py but over sockets.

  node = P2PNode(NodeService(store, validator_wallet=v))
  node.start(); node.connect(peer_host, peer_port)
  node.produce_and_gossip()      # validator: make a block and broadcast it
  node.submit_tx(signed_tx)      # client: admit a tx and broadcast it

ponytail: one message per connection (line-delimited JSON), best-effort send (a down peer
is skipped, no retry queue in v1), bounded de-dup memory. Peer discovery, outbound retry,
and authentication are deferred -- this is the minimum that makes a real multi-node network
converge. Block-production scheduling (whose turn, on a clock) stays the caller's job.

Run directly ->  python p2p.py
"""
from __future__ import annotations

import json
import os
import socketserver
import threading
import time

from consensus import vote_tx
from core import ValidationError, sha256
from network import decode_block, decode_tx, encode_block, encode_tx, tcp_send

_SEEN_CAP = 100_000   # bound de-dup memory (DoS): forget the oldest beyond this
_MAX_PEERS = 256      # bound the peer table (DoS): ignore new peers beyond this
_MAX_ORPHANS = 5_000  # bound buffered out-of-order blocks (DoS)
_MAX_MSG_BYTES = 4_000_000  # reject oversize gossip lines (DoS)
_MAX_SYNC_BATCH = 1_000  # blocks per get_blocks response; requester pages by re-asking


def _block_wire_id(block) -> str:
    """De-dup id over the FULL sealed block (hash + signature + pubkey).

    block.hash deliberately excludes the validator signature, so an attacker could send an
    unsigned twin with the same block.hash to poison a hash-keyed cache and censor the real
    block. Including the signature here makes a forged twin a DIFFERENT id, so it can't.
    """
    return sha256((block.hash + block.validator_sig + block.proposer_pubkey).encode()).hex()


class _Handler(socketserver.StreamRequestHandler):
    timeout = 10  # drop slowloris / stalled peers

    def handle(self) -> None:
        # Bound the read, require a terminated line, and never let malformed/oversize/slow
        # input crash this handler thread (it serves untrusted peers).
        try:
            line = self.rfile.readline(_MAX_MSG_BYTES + 1)
            if not line or len(line) > _MAX_MSG_BYTES or not line.endswith(b"\n"):
                return
            msg = json.loads(line.decode())
            if not isinstance(msg, dict):
                return
            self.server.p2p._on_message(msg, self.client_address[0])  # type: ignore[attr-defined]
        except (ValueError, KeyError, TypeError, ValidationError, OSError):
            return


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, p2p, host, port):
        super().__init__((host, port), _Handler)
        self.p2p = p2p


class P2PNode:
    def __init__(self, service, host: str = "127.0.0.1", port: int = 0, advertise: str = ""):
        self.service = service
        self.peers: list = []
        self.seen_tx: dict = {}
        self.seen_block: dict = {}
        self.orphans: dict = {}  # previous_hash -> [block wire dicts] awaiting their parent
        self._voted_targets: set = set()  # checkpoint heights we've already voted -> no double-vote
        self.server = _Server(self, host, port)
        self.host, self.port = self.server.server_address
        # The dialable address this node announces to others. For a real deploy pass a
        # reachable host; on localhost the bind address is fine.
        self.advertise = advertise or f"{self.host}:{self.port}"
        self._thread = None

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> None:
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def connect(self, host: str, port: int) -> None:
        if (host, port) not in self.peers:
            self.peers.append((host, port))

    # -- gossip ------------------------------------------------------------ #
    def submit_tx(self, tx) -> None:
        """A local client submits a transaction: admit it, then broadcast."""
        self.service.mempool.add(tx, self.service.chain)  # raises if invalid
        self._remember(self.seen_tx, tx.txid)
        self._broadcast({"type": "tx", "data": encode_tx(tx)})

    def produce_and_gossip(self, timestamp: int = 0) -> dict:
        """A validator produces the next block and broadcasts it."""
        block = self.service.produce_block({"timestamp": timestamp} if timestamp else {})
        self._remember(self.seen_block, decode_block(block).hash)
        self._broadcast({"type": "block", "data": block})
        return block

    def announce(self) -> None:
        """Tell peers who we are and who we know, so the mesh self-forms from a seed."""
        known = [f"{h}:{p}" for h, p in self.peers]
        self._broadcast({"type": "hello", "addr": self.advertise, "peers": known})

    def _learn_peers(self, msg: dict) -> None:
        for addr in [msg.get("addr")] + msg.get("peers", []):
            if not addr or addr == self.advertise:
                continue
            host, sep, port = addr.rpartition(":")
            if not sep or not port.isdigit():
                continue
            peer = (host, int(port))
            if peer not in self.peers and len(self.peers) < _MAX_PEERS:
                self.peers.append(peer)

    def auto_vote(self) -> None:
        """Cast a finality vote for the latest checkpoint, driving finality forward.

        Fault-free by construction: we vote at most once per target HEIGHT (no double vote),
        and the source is the highest justified checkpoint which only rises (no surround vote),
        so an honest auto-voter can never slash itself even across transient forks.
        """
        v = self.service.validator
        if v is None:
            return
        chain = self.service.chain
        if v.address not in chain.stakes:
            return
        epoch = chain.epoch
        target = next((b for b in reversed(chain.chain)
                       if b.index > 0 and b.index % epoch == 0), None)
        if target is None or target.index in self._voted_targets:
            return
        # Source = highest justified checkpoint strictly below the target (defaults to finalized).
        src_hash, src_height = chain.finalized
        for b in chain.chain:
            if (b.index < target.index and b.index % epoch == 0
                    and b.hash in chain.justified and b.index >= src_height):
                src_hash, src_height = b.hash, b.index
        if src_height >= target.index:
            return
        # Nonce accounts for our votes already waiting in the mempool.
        pending = sum(1 for t in self.service.mempool.pool.values() if t.sender == v.address)
        nonce = chain.nonces.get(v.address, 0) + pending
        try:
            self.submit_tx(vote_tx(v, src_hash, src_height, target.hash, target.index, nonce))
            self._voted_targets.add(target.index)
        except ValidationError:
            pass

    def tick(self, timestamp: int = 0):
        """Produce + gossip the next block IFF this node is the elected validator.

        A deployment calls tick() on a slot timer; whoever is elected for the current head
        produces, everyone else no-ops. That is the whole autonomous-block-production loop.
        """
        validator = self.service.validator
        if validator is None:
            return None
        try:
            elected = self.service.chain.next_validator()
        except ValidationError:
            return None  # no active validators
        if elected != validator.address:
            return None
        return self.produce_and_gossip(timestamp)

    def _on_message(self, msg: dict, client_ip=None) -> None:
        kind = msg.get("type")
        if kind == "hello":
            self._learn_peers(msg)
            return
        if kind == "tx":
            tx = decode_tx(msg.get("data"))   # raises ValidationError on malformed -> caught by handler
            if not self._remember(self.seen_tx, tx.txid):
                return  # already seen -> stop the relay loop
            try:
                self.service.mempool.add(tx, self.service.chain)
            except ValidationError:
                return  # invalid -> drop, don't relay
            self._broadcast(msg)
        elif kind == "block":
            block = decode_block(msg.get("data"))
            # De-dup on a signature-inclusive id so a forged unsigned twin can't censor.
            if not self._remember(self.seen_block, _block_wire_id(block)):
                return
            self._apply_or_buffer(block, msg["data"])
        elif kind == "get_blocks":
            self._serve_blocks(msg, client_ip)

    # -- block sync (late joiners) ----------------------------------------- #
    def request_sync(self) -> None:
        """Ask peers for any blocks above our height (catch up a lagging/new node)."""
        self._broadcast({
            "type": "get_blocks",
            "since": self.service.chain.head.index,
            "reply": self.advertise,
        })

    def _serve_blocks(self, msg: dict, client_ip=None) -> None:
        reply = msg.get("reply", "")
        host, sep, port = reply.rpartition(":")
        if not sep or not port.isdigit():
            return
        # Anti-reflection: only ever send blocks back to the IP that asked, never to an
        # attacker-chosen third party. (client_ip is the real source of this connection.)
        if client_ip is not None and host != client_ip:
            return
        since = msg.get("since", 0)
        if not isinstance(since, int):
            return
        # Send our canonical blocks above `since`, in order, capped per response (the
        # requester pages by re-asking with a higher `since`).
        sent = 0
        for block in self.service.tree.canonical.chain:
            if block.index > since:
                try:
                    tcp_send(host, int(port), {"type": "block", "data": encode_block(block)})
                except OSError:
                    return
                sent += 1
                if sent >= _MAX_SYNC_BATCH:
                    return

    def _apply_or_buffer(self, block, data: dict) -> None:
        tree = self.service.tree
        if block.previous_hash not in tree.weight:
            # Parent unknown -> stash the orphan and ask peers to catch us up.
            if len(self.orphans) < _MAX_ORPHANS:
                self.orphans.setdefault(block.previous_hash, []).append(data)
            self.request_sync()
            return
        try:
            result = self.service.receive_block({"block": data})
        except ValidationError:
            return  # invalid / finality conflict -> drop, don't relay
        if result["accepted"]:
            self._broadcast({"type": "block", "data": data})
            self._drain_orphans(block.hash)

    def _drain_orphans(self, parent_hash: str) -> None:
        for data in self.orphans.pop(parent_hash, []):
            child = decode_block(data)
            try:
                result = self.service.receive_block({"block": data})
            except ValidationError:
                continue
            if result["accepted"]:
                self._broadcast({"type": "block", "data": data})
                self._drain_orphans(child.hash)

    # -- internals --------------------------------------------------------- #
    def _broadcast(self, msg: dict) -> None:
        for host, port in self.peers:
            try:
                tcp_send(host, port, msg)
            except OSError:
                pass  # peer unreachable; best-effort, no retry queue in v1

    def _remember(self, seen: dict, key: str) -> bool:
        if key in seen:
            return False
        seen[key] = True
        if len(seen) > _SEEN_CAP:
            seen.pop(next(iter(seen)))  # FIFO eviction (dict keeps insertion order)
        return True


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #
def _wait_for(cond, timeout: float = 4.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


def _demo() -> None:
    import shutil
    import tempfile

    from core import COIN, MAX_SUPPLY_UNITS, Transaction, Wallet
    from node import NodeService
    from storage import BlockStore

    alice, bob = Wallet.from_secret(1), Wallet.from_secret(2)
    v1 = Wallet.from_secret(3)  # single validator -> always elected, only one can produce

    def fresh_store(tmp):
        store = BlockStore(tmp)
        store.write_genesis(
            allocations={alice.address: MAX_SUPPLY_UNITS - 1000 * COIN, v1.address: 1000 * COIN},
            initial_stakes={v1.address: 1000 * COIN},
            timestamp=1_700_000_000,
        )
        return store

    dirs = [tempfile.mkdtemp(prefix=f"mindees_p2p{i}_") for i in range(3)]
    nodes = []
    try:
        a = P2PNode(NodeService(fresh_store(dirs[0]), validator_wallet=v1))
        b = P2PNode(NodeService(fresh_store(dirs[1])))
        c = P2PNode(NodeService(fresh_store(dirs[2])))
        nodes = [a, b, c]
        for n in nodes:
            n.start()

        # Line topology A - B - C (so a message must be RELAYED through B to reach C).
        a.connect(b.host, b.port)
        b.connect(a.host, a.port)
        b.connect(c.host, c.port)
        c.connect(b.host, b.port)

        # A client tx submitted at A must reach C's mempool two hops away.
        tx = Transaction(alice.address, bob.address, 100 * COIN, 0, 0).sign(alice)
        a.submit_tx(tx)
        assert _wait_for(lambda: len(c.service.mempool) == 1), "tx did not propagate to C"

        # A (the validator) produces a block; it must propagate A -> B -> C to one head.
        a.produce_and_gossip(timestamp=1_700_000_010)
        assert _wait_for(lambda: c.service.chain.head.index == 1), "block did not reach C"
        head = a.service.chain.head.hash
        assert b.service.chain.head.hash == head and c.service.chain.head.hash == head
        assert c.service.chain.balance_of(bob.address) == 100 * COIN
        for n in nodes:
            assert n.service.chain.total_supply() == MAX_SUPPLY_UNITS

        # Re-broadcasting the same block is a no-op everywhere (de-dup).
        a._broadcast({"type": "block", "data": a.service.get_block({"index": 1})})
        assert _wait_for(lambda: b.service.chain.head.index == 1)

        # Dedup-poisoning defense: a forged twin (same block.hash, different signature) has a
        # DIFFERENT wire-id, so it cannot censor the real block in the seen cache.
        from consensus import seal_block
        real = a.service.tree.canonical.chain[1]
        twin = seal_block(real.previous_hash, real.index, v1, list(real.transactions), real.timestamp)
        assert twin.hash == real.hash and _block_wire_id(twin) != _block_wire_id(real)

        # Peer discovery: A knows only B; B announces its peers (A, C); A learns C through B.
        assert (c.host, c.port) not in a.peers
        b.announce()
        assert _wait_for(lambda: (c.host, c.port) in a.peers), "A did not discover C via B"

        # Advance the chain a few more blocks (A is the validator).
        for h in range(2, 5):
            a.produce_and_gossip(timestamp=1_700_000_000 + h)
        assert _wait_for(lambda: a.service.chain.head.index == 4)

        # Late joiner: a brand-new node D starts behind, connects, and SYNCS the whole chain.
        d_dir = tempfile.mkdtemp(prefix="mindees_p2pD_")
        dirs.append(d_dir)
        d = P2PNode(NodeService(fresh_store(d_dir)))
        nodes.append(d)
        d.start()
        d.connect(a.host, a.port)
        d.request_sync()                       # ask A for everything above genesis
        assert _wait_for(lambda: d.service.chain.head.index == 4), "late joiner did not sync"
        assert d.service.chain.head.hash == a.service.chain.head.hash
        assert d.service.chain.total_supply() == MAX_SUPPLY_UNITS

        print("ALL CHECKS PASSED")
        print("  p2p: tx + block gossip over TCP, relayed across hops, converges to one head")
        print("  peer discovery: the mesh self-forms from a seed (A learned C through B)")
        print("  block sync: a late-joining node caught up the full chain from a peer")
        print(f"  nodes synced to height {a.service.chain.head.index}, supply intact")
    finally:
        for n in nodes:
            n.stop()
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)


def _demo_autonomous() -> None:
    """Three staked validators self-produce blocks on a slot tick and stay converged."""
    import shutil
    import tempfile

    from core import COIN, MAX_SUPPLY_UNITS, Wallet
    from node import NodeService
    from storage import BlockStore

    alice = Wallet.from_secret(4)
    vs = [Wallet.from_secret(1), Wallet.from_secret(2), Wallet.from_secret(3)]
    stake = 1000 * COIN

    def fresh_store(tmp):
        store = BlockStore(tmp)
        store.write_genesis(
            allocations={alice.address: MAX_SUPPLY_UNITS - 3 * stake, **{v.address: stake for v in vs}},
            initial_stakes={v.address: stake for v in vs},
            timestamp=1_700_000_000, epoch=2,  # short epoch so finality advances quickly here
        )
        return store

    dirs = [tempfile.mkdtemp(prefix=f"mindees_auto{i}_") for i in range(3)]
    nodes = []
    try:
        nodes = [P2PNode(NodeService(fresh_store(dirs[i]), validator_wallet=vs[i])) for i in range(3)]
        for n in nodes:
            n.start()
        # Fully connect the mesh.
        for n in nodes:
            for m in nodes:
                if m is not n:
                    n.connect(m.host, m.port)

        # Run slots: the elected validator produces; everyone votes; the rest relay.
        for h in range(1, 13):
            for n in nodes:
                n.tick(timestamp=1_700_000_000 + h)
            assert _wait_for(lambda h=h: all(x.service.chain.head.index >= h for x in nodes)), \
                f"network failed to advance to height {h}"
            for n in nodes:
                n.auto_vote()

        assert _wait_for(lambda: all(x.service.tree.finalized_height > 0 for x in nodes)), \
            "finality did not advance autonomously"
        heads = {n.service.chain.head.hash for n in nodes}
        assert len(heads) == 1  # all three agree on the head
        for n in nodes:
            assert n.service.chain.total_supply() == MAX_SUPPLY_UNITS

        fh = nodes[0].service.tree.finalized_height
        print("ALL CHECKS PASSED")
        print("  autonomous: 3 validators self-produce AND self-vote on a slot tick")
        print(f"  height {nodes[0].service.chain.head.index}, finalized through height {fh}, supply intact")
    finally:
        for n in nodes:
            n.stop()
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)


def _cli(argv=None) -> None:
    """Launch a live network node: serve, connect to peers, and self-produce on a slot timer.

      python p2p.py serve --data ./chaindata --port 9000 \
          --validator-secret <HEX> --peer 127.0.0.1:9001 --slot 2
    """
    import argparse

    from node import NodeService
    from storage import BlockStore

    parser = argparse.ArgumentParser(prog="p2p", description="Mindees live network node")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve", help="run a gossiping, self-producing network node")
    p.add_argument("--data", required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--validator-secret", default=None, help="hex secret to produce blocks")
    p.add_argument("--validator-keystore", default=None,
                   help="encrypted validator keystore ($MINDEES_PASSPHRASE) -- preferred over a raw secret")
    p.add_argument("--advertise", default=None, help="dialable host:port to announce (default: bind address)")
    p.add_argument("--peer", action="append", default=[], help="peer host:port (repeatable)")
    p.add_argument("--checkpoint", default=None,
                   help="weak-subjectivity checkpoint HASH:HEIGHT (reject conflicting history)")
    p.add_argument("--slot", type=float, default=2.0, help="seconds between production ticks")
    args = parser.parse_args(argv)

    from core import Wallet

    # Prefer an encrypted keystore: the validator secret is never on the command line or in env.
    secret = args.validator_secret
    if args.validator_keystore:
        from keystore import decrypt_secret, load_keystore
        passphrase = os.environ.get("MINDEES_PASSPHRASE")
        if not passphrase:
            raise SystemExit("set $MINDEES_PASSPHRASE to unlock the validator keystore")
        secret = decrypt_secret(load_keystore(args.validator_keystore), passphrase)

    validator = Wallet.from_secret(int(secret, 16)) if secret else None
    checkpoint = None
    if args.checkpoint:
        h, _, height = args.checkpoint.rpartition(":")
        checkpoint = (h, int(height))
    node = P2PNode(NodeService(BlockStore(args.data), validator, checkpoint=checkpoint),
                   args.host, args.port, advertise=args.advertise or "")
    node.start()
    for spec in args.peer:
        host, _, port = spec.rpartition(":")
        node.connect(host, int(port))
    print(f"Mindees node on {node.host}:{node.port}  advertise={node.advertise}  peers={args.peer}  "
          f"validator={'yes' if validator else 'no'}  height={node.service.chain.head.index}")
    try:
        while True:
            node.announce()                  # gossip peers so the mesh self-forms
            node.request_sync()               # catch up if we are behind
            node.tick(int(time.time()))       # produce a block if it is our turn
            node.auto_vote()                  # vote for the latest checkpoint -> drives finality
            time.sleep(args.slot)
    except KeyboardInterrupt:
        node.stop()
        print("\nstopped")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        _cli()
    else:
        _demo()
        _demo_autonomous()
