"""
Mindees P2P gossip  --  Phase 4.

This is where Mindees stops being one process and becomes a network: many nodes,
each independently validating every transaction and block, relaying what is new and
dropping what is invalid. Decentralised independent validation is the axis BTC owns
and a new chain has to *earn*; this phase is the first brick of it.

What is here (ponytail: the correctness-critical core, fully testable, deterministic):
  * A wire codec (JSON dicts) for transactions and blocks.
  * Gossip: receive -> validate -> if new, apply/admit -> relay to other peers.
    Dedup via per-node "seen" sets stops the relay looping forever in a mesh.
  * Block propagation: a received block is validated by the SAME consensus rules the
    producer used (proposer election, signature, supply) before it is accepted.
  * Chain sync: a node that joins late pulls the blocks it is missing and replays them.
  * A thin TCP ingest server proving the wire format works over a real socket.

The full multi-peer TCP daemon (outbound dials, peer discovery, persistence) belongs
with the runnable node in Phase 6; the transport here is deliberately pluggable so the
gossip logic above is transport-agnostic and does not depend on sockets to be correct.

Self-testing: run directly ->  python network.py
"""
from __future__ import annotations

import json
import socket
import socketserver
import threading
import time
from typing import List

from core import Block, Transaction, ValidationError

# --------------------------------------------------------------------------- #
# Wire codec
# --------------------------------------------------------------------------- #
_TX_FIELDS = ("sender", "recipient", "amount", "fee", "nonce", "public_key", "signature", "evidence")


def encode_tx(tx: Transaction) -> dict:
    return {k: getattr(tx, k) for k in _TX_FIELDS}


def decode_tx(d: dict) -> Transaction:
    return Transaction(**{k: d[k] for k in _TX_FIELDS})


def encode_block(b: Block) -> dict:
    return {
        "index": b.index,
        "previous_hash": b.previous_hash,
        "timestamp": b.timestamp,
        "transactions": [encode_tx(t) for t in b.transactions],
        "validator": b.validator,
        "nonce": b.nonce,
        "merkle_root": b.merkle_root,
        "proposer_pubkey": b.proposer_pubkey,
        "validator_sig": b.validator_sig,
    }


def decode_block(d: dict) -> Block:
    return Block(
        index=d["index"],
        previous_hash=d["previous_hash"],
        timestamp=d["timestamp"],
        transactions=[decode_tx(t) for t in d["transactions"]],
        validator=d["validator"],
        nonce=d.get("nonce", 0),
        merkle_root=d["merkle_root"],  # preserved so the block hash is identical
        proposer_pubkey=d.get("proposer_pubkey", ""),
        validator_sig=d.get("validator_sig", ""),
    )


# --------------------------------------------------------------------------- #
# Gossiping node (transport-agnostic; peers are any objects with _recv_* methods)
# --------------------------------------------------------------------------- #
class Node:
    def __init__(self, node_id: str, chain, mempool) -> None:
        self.id = node_id
        self.chain = chain
        self.mempool = mempool
        self.peers: List["Node"] = []
        self.seen_tx: set = set()
        self.seen_block: set = set()

    def connect(self, other: "Node") -> None:
        if other not in self.peers:
            self.peers.append(other)
        if self not in other.peers:
            other.peers.append(self)

    # -- transactions ------------------------------------------------------ #
    def broadcast_tx(self, tx: Transaction) -> None:
        self._recv_tx(encode_tx(tx), origin=None)

    def _recv_tx(self, data: dict, origin) -> None:
        tx = decode_tx(data)
        if tx.txid in self.seen_tx:
            return  # already gossiped through here -> stop the loop
        self.seen_tx.add(tx.txid)
        try:
            self.mempool.add(tx, self.chain)
        except ValidationError:
            return  # invalid -> drop, and do NOT relay junk
        for peer in self.peers:
            if peer is not origin:
                peer._recv_tx(data, origin=self)

    # -- blocks ------------------------------------------------------------ #
    def produce_block(self, validator_wallet, timestamp: int) -> Block:
        txs = self.mempool.select(self.chain)
        block = self.chain.add_block(txs, validator_wallet, timestamp)
        self.seen_block.add(block.hash)
        self.mempool.update(self.chain)
        data = encode_block(block)
        for peer in self.peers:
            peer._recv_block(data, origin=self)
        return block

    def _recv_block(self, data: dict, origin) -> None:
        block = decode_block(data)
        if block.hash in self.seen_block:
            return
        self.seen_block.add(block.hash)
        try:
            self.chain.submit_block(block)  # same consensus rules as the producer
        except ValidationError:
            return  # invalid -> drop, and do NOT relay
        self.mempool.update(self.chain)
        for peer in self.peers:
            if peer is not origin:
                peer._recv_block(data, origin=self)

    # -- sync -------------------------------------------------------------- #
    def blocks_after(self, index: int) -> List[dict]:
        return [encode_block(b) for b in self.chain.chain if b.index > index]

    def sync_from(self, peer: "Node") -> int:
        applied = 0
        for data in peer.blocks_after(self.chain.head.index):
            block = decode_block(data)
            if block.index <= self.chain.head.index:
                continue
            self.chain.submit_block(block)
            self.seen_block.add(block.hash)
            applied += 1
        self.mempool.update(self.chain)
        return applied


# --------------------------------------------------------------------------- #
# Thin TCP ingest (one message per connection: {"type": "tx"|"block", "data": ...})
# --------------------------------------------------------------------------- #
class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline()
        if not line:
            return
        msg = json.loads(line.decode())
        node = self.server.node  # type: ignore[attr-defined]
        if msg["type"] == "tx":
            node._recv_tx(msg["data"], origin=None)
        elif msg["type"] == "block":
            node._recv_block(msg["data"], origin=None)


class TCPIngest(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, node: Node, host: str = "127.0.0.1", port: int = 0):
        super().__init__((host, port), _Handler)
        self.node = node


def tcp_send(host: str, port: int, msg: dict) -> None:
    with socket.create_connection((host, port), timeout=2) as s:
        s.sendall((json.dumps(msg) + "\n").encode())


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #
def _new_chain():
    from core import COIN, MAX_SUPPLY_UNITS, Wallet
    from consensus import ProofOfStakeChain

    alice = Wallet.from_secret(1)
    v1, v2 = Wallet.from_secret(3), Wallet.from_secret(4)
    allocations = {
        alice.address: MAX_SUPPLY_UNITS - 2000 * COIN,
        v1.address: 1000 * COIN,
        v2.address: 1000 * COIN,
    }
    chain = ProofOfStakeChain(
        allocations,
        initial_stakes={v1.address: 1000 * COIN, v2.address: 1000 * COIN},
        timestamp=1_700_000_000,
    )
    return chain


def _demo() -> None:
    from core import COIN, Wallet
    from mempool import Mempool

    alice = Wallet.from_secret(1)
    bob = Wallet.from_secret(2)
    v1, v2 = Wallet.from_secret(3), Wallet.from_secret(4)
    wallets = {w.address: w for w in (alice, bob, v1, v2)}

    # Wire codec round-trips preserve identity (txid / block hash unchanged).
    tx = Transaction(alice.address, bob.address, 100 * COIN, 1 * COIN, 0).sign(alice)
    assert decode_tx(encode_tx(tx)).txid == tx.txid

    # Three nodes in a line A - B - C (tests relay + dedup across a hop).
    a = Node("A", _new_chain(), Mempool())
    b = Node("B", _new_chain(), Mempool())
    c = Node("C", _new_chain(), Mempool())
    a.connect(b)
    b.connect(c)
    assert a.chain.head.hash == b.chain.head.hash == c.chain.head.hash  # identical genesis

    # Gossip a tx from A: it must reach C two hops away, and not duplicate.
    a.broadcast_tx(tx)
    assert len(a.mempool) == len(b.mempool) == len(c.mempool) == 1
    a.broadcast_tx(tx)  # re-broadcast is a no-op everywhere (dedup)
    assert len(a.mempool) == len(b.mempool) == len(c.mempool) == 1

    # Block codec round-trips before we rely on it propagating.
    elected = a.chain.next_validator()
    probe = Block(index=1, previous_hash=a.chain.head.hash, timestamp=1, transactions=[tx], validator=elected)
    assert decode_block(encode_block(probe)).hash == probe.hash

    # A produces the next block; it must propagate A -> B -> C to an identical head.
    a.produce_block(wallets[elected], timestamp=1_700_000_010)
    assert a.chain.head.index == 1
    assert a.chain.head.hash == b.chain.head.hash == c.chain.head.hash
    assert b.chain.balance_of(bob.address) == 100 * COIN  # B applied A's block
    for node in (a, b, c):
        assert node.chain.total_supply() == _new_chain().total_supply()
        assert len(node.mempool) == 0  # mined tx pruned everywhere

    # An invalid block (proposer has zero stake -> can never be elected) is rejected
    # AND not relayed past B.
    bad = Block(
        index=b.chain.head.index + 1,
        previous_hash=b.chain.head.hash,
        timestamp=1_700_000_020,
        transactions=[],
        validator=bob.address,  # bob holds no stake; never an eligible proposer
    )
    bad.proposer_pubkey = bob.public_key_hex
    bad.validator_sig = bob.sign(bytes.fromhex(bad.hash)).hex()
    c_height_before = c.chain.head.index
    b._recv_block(encode_block(bad), origin=None)
    assert b.chain.head.index == 1  # B refused it
    assert c.chain.head.index == c_height_before  # never relayed to C

    # A late joiner D syncs the whole chain from A by replaying missing blocks.
    d = Node("D", _new_chain(), Mempool())
    applied = d.sync_from(a)
    assert applied == 1
    assert d.chain.head.hash == a.chain.head.hash
    assert d.chain.total_supply() == a.chain.total_supply()

    # The wire format actually works over a real TCP socket (loopback ingest).
    e = Node("E", _new_chain(), Mempool())
    server = TCPIngest(e)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        wire_tx = Transaction(alice.address, bob.address, 5 * COIN, 0, 0).sign(alice)
        tcp_send(host, port, {"type": "tx", "data": encode_tx(wire_tx)})
        deadline = time.time() + 2.0
        while len(e.mempool) == 0 and time.time() < deadline:
            time.sleep(0.01)
        assert len(e.mempool) == 1  # tx arrived over the socket and was admitted
    finally:
        server.shutdown()
        server.server_close()

    print("ALL CHECKS PASSED")
    print("  P2P gossip: relay+dedup across hops, block propagation, sync, TCP ingest")
    print("  invalid blocks/txs are dropped and never relayed")


if __name__ == "__main__":
    _demo()
