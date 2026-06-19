"""
Mindees persistence  --  Phase 5.

The chain has to survive a process restart. The store is the dumbest durable thing
that is also correct:

  * genesis.json     -- the genesis parameters (allocations, initial stakes, timestamp),
                        written once, atomically. The whole chain's validity is anchored
                        to reconstructing the exact same genesis state.
  * blocks.jsonl     -- one JSON-encoded block per line, append-only, fsync'd on write.

Loading replays every block through the SAME consensus validation a peer would apply
(`chain.submit_block`). So persistence can never inject inconsistent state, and a
tampered block file refuses to boot instead of loading bad balances -- the store is
tamper-evident for free, no extra hashing of our own on top of the chain's.

ponytail: flat files + stdlib json. No LevelDB/RocksDB, no sqlite -- the chain replays
in milliseconds at this scale and re-validation is the durability guarantee. Swap in a
keyed store only when load time actually hurts (millions of blocks).

Self-testing: run directly ->  python storage.py
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Dict, Optional

from consensus import ProofOfStakeChain
from core import ValidationError
from network import decode_block, encode_block

GENESIS_FILE = "genesis.json"
BLOCKS_FILE = "blocks.jsonl"


def _atomic_write(path: str, text: str) -> None:
    """Write via temp file + os.replace so a crash can't leave a half-written genesis."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class BlockStore:
    def __init__(self, directory: str) -> None:
        self.dir = directory
        os.makedirs(directory, exist_ok=True)
        self.genesis_path = os.path.join(directory, GENESIS_FILE)
        self.blocks_path = os.path.join(directory, BLOCKS_FILE)

    def write_genesis(
        self,
        allocations: Dict[str, int],
        initial_stakes: Optional[Dict[str, int]],
        timestamp: int,
        vesting: Optional[Dict[str, tuple]] = None,
        unbonding_blocks: int = 0,
        treasury_address: Optional[str] = None,
        epoch: int = 32,
    ) -> None:
        if os.path.exists(self.genesis_path):
            raise ValidationError("genesis already initialised")
        payload = {
            "allocations": allocations,
            "initial_stakes": initial_stakes or {},
            "timestamp": timestamp,
            # grants stored as lists (JSON has no tuples); rebuilt as tuples on load
            "vesting": {addr: list(grant) for addr, grant in (vesting or {}).items()},
            "unbonding_blocks": unbonding_blocks,
            "treasury_address": treasury_address,
            "epoch": epoch,
        }
        _atomic_write(self.genesis_path, json.dumps(payload, sort_keys=True))

    def append(self, block) -> None:
        """Durably append one block. fsync so an accepted block isn't lost on crash."""
        line = json.dumps(encode_block(block))
        with open(self.blocks_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())

    def height(self) -> int:
        if not os.path.exists(self.blocks_path):
            return 0
        with open(self.blocks_path, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())

    def genesis_params(self) -> tuple:
        """The genesis args tuple (also the BlockTree constructor args), read from disk."""
        with open(self.genesis_path, encoding="utf-8") as f:
            g = json.load(f)
        vesting = {addr: tuple(grant) for addr, grant in g.get("vesting", {}).items()}
        return (
            g["allocations"], g["initial_stakes"], g["timestamp"], vesting,
            g.get("unbonding_blocks", 0), g.get("treasury_address"), g.get("epoch", 32),
        )

    def iter_blocks(self):
        """Yield every persisted block in append order (parents before children)."""
        if not os.path.exists(self.blocks_path):
            return
        with open(self.blocks_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield decode_block(json.loads(line))

    def load(self) -> ProofOfStakeChain:
        """Rebuild a single linear chain from disk, re-validating every block."""
        chain = ProofOfStakeChain(*self.genesis_params())
        if os.path.exists(self.blocks_path):
            with open(self.blocks_path, encoding="utf-8") as f:
                for lineno, line in enumerate(f, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    block = decode_block(json.loads(line))
                    try:
                        chain.submit_block(block)
                    except ValidationError as exc:
                        raise ValidationError(f"corrupt block store at line {lineno}: {exc}") from exc
        return chain


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #
def _demo() -> None:
    import shutil

    from core import COIN, MAX_SUPPLY_UNITS, Transaction, Wallet
    from mempool import Mempool

    alice, bob = Wallet.from_secret(1), Wallet.from_secret(2)
    v1, v2 = Wallet.from_secret(3), Wallet.from_secret(4)
    wallets = {w.address: w for w in (alice, bob, v1, v2)}
    allocations = {
        alice.address: MAX_SUPPLY_UNITS - 2000 * COIN,
        v1.address: 1000 * COIN,
        v2.address: 1000 * COIN,
    }
    initial_stakes = {v1.address: 1000 * COIN, v2.address: 1000 * COIN}

    tmp = tempfile.mkdtemp(prefix="mindees_")
    try:
        store = BlockStore(tmp)
        store.write_genesis(allocations, initial_stakes, 1_700_000_000)

        # Re-initialising over an existing genesis is refused.
        try:
            store.write_genesis(allocations, initial_stakes, 1)
            raise AssertionError("duplicate genesis should be refused")
        except ValidationError:
            pass

        chain = store.load()  # genesis only
        assert chain.head.index == 0
        assert chain.total_supply() == MAX_SUPPLY_UNITS

        # Produce and persist three blocks.
        mp = Mempool()
        for i in range(3):
            tx = Transaction(alice.address, bob.address, (i + 1) * COIN, 1 * COIN, i).sign(alice)
            mp.add(tx, chain)
            elected = chain.next_validator()
            block = chain.add_block(mp.select(chain), wallets[elected], 1_700_000_010 + i * 10)
            mp.update(chain)
            store.append(block)
        assert store.height() == 3
        head_before = chain.head.hash
        bob_before = chain.balance_of(bob.address)

        # RESTART: rebuild the entire chain purely from disk.
        reloaded = store.load()
        assert reloaded.head.index == 3
        assert reloaded.head.hash == head_before
        assert reloaded.balance_of(bob.address) == bob_before
        assert reloaded.nonces[alice.address] == chain.nonces[alice.address]
        assert reloaded.stakes == chain.stakes
        assert reloaded.total_supply() == MAX_SUPPLY_UNITS
        assert reloaded.is_valid_chain()

        # TAMPER: edit a persisted block on disk -> load must refuse to boot.
        with open(store.blocks_path, encoding="utf-8") as f:
            lines = f.readlines()
        forged = json.loads(lines[1])
        forged["transactions"][0]["amount"] = 999_999 * COIN  # mint money out of thin air
        lines[1] = json.dumps(forged) + "\n"
        with open(store.blocks_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        try:
            store.load()
            raise AssertionError("tampered block store should fail to load")
        except ValidationError:
            pass

        print("ALL CHECKS PASSED")
        print("  persistence: genesis + append-only blocks, full replay rebuilds state")
        print(f"  tamper-evident on load; restart restored head {head_before[:16]}...")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    _demo()
