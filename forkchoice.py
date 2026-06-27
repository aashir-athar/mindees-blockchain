"""
Mindees fork choice  --  Phase 9.

The chain so far has been a single line. Reality has forks: a validator can equivocate
(sign two different blocks at the same height) or a network partition can heal into two
competing histories. Fork choice is the rule that makes every honest node converge on the
SAME canonical chain anyway.

Rule: heaviest branch wins, where a block's weight contribution is the proposer's stake at
the moment it was produced (stake-weighted, the PoS analogue of Bitcoin's most-work). Ties
break on the smaller block hash, so the choice is fully deterministic. When a heavier branch
appears, the engine reorgs: it rebuilds canonical state along the new branch.

Implementation (ponytail): we keep every accepted block in a tree and validate / rebuild
state by REPLAYING from genesis along a branch, reusing the already-tested ProofOfStakeChain
engine unchanged. That is O(branch length) per operation -- fine at this scale.
  # ponytail: replay-from-genesis per block; cache per-block state or apply incrementally
  # if chains ever get long enough for the replay cost to matter.

Nothing-at-stake (a validator can back every fork for free) is NOT solved here -- that needs
slashing + finality, the next phases. Fork choice only guarantees deterministic convergence.

Self-testing: run directly ->  python forkchoice.py
"""
from __future__ import annotations

from typing import Dict, List

from consensus import ProofOfStakeChain
from core import Block, ValidationError


class ConflictingFinalityError(ValidationError):
    """Raised when a branch finalizes a checkpoint conflicting with the established one.

    This is an attributable safety fault (>=1/3 of source stake must have double/surround
    voted). The node halts ingestion for operator weak-subjectivity recovery rather than
    silently latching a per-node fork.
    """


class BlockTree:
    def __init__(self, allocations, initial_stakes=None, timestamp=0, vesting=None,
                 unbonding_blocks=0, treasury_address=None, epoch=32, checkpoint=None):
        self._genesis_args = (allocations, initial_stakes, timestamp, vesting,
                              unbonding_blocks, treasury_address, epoch)
        self.canonical = ProofOfStakeChain(*self._genesis_args)  # genesis-only chain
        self.genesis_hash = self.canonical.head.hash
        self.head_hash = self.genesis_hash
        # Weak-subjectivity checkpoint: an operator-trusted (hash, height). Any block at that
        # height with a different hash is rejected, so a long-range fork built from
        # since-exited stake can never reproduce the checkpoint and become canonical.
        self.ws_checkpoint = tuple(checkpoint) if checkpoint else None
        # Finality frontier: nothing at or below this is ever reorged. Rebuilt from the
        # canonical chain's committed FFG state, never trusted from memory across restart.
        self.finalized_hash = self.genesis_hash
        self.finalized_height = 0

        self.blocks: Dict[str, Block] = {}              # hash -> Block (genesis omitted)
        self.parent: Dict[str, str] = {}                # hash -> parent hash
        self.weight: Dict[str, int] = {self.genesis_hash: 0}
        self.height: Dict[str, int] = {self.genesis_hash: 0}

    # -- queries ----------------------------------------------------------- #
    @property
    def head(self) -> Block:
        return self.canonical.head

    def chain_at(self, block_hash: str) -> ProofOfStakeChain:
        """Replay a fresh canonical-rules chain from genesis up to `block_hash`."""
        path: List[str] = []
        h = block_hash
        while h != self.genesis_hash:
            if h not in self.parent:
                raise ValidationError(f"unknown block {h}")
            path.append(h)
            h = self.parent[h]
        path.reverse()
        chain = ProofOfStakeChain(*self._genesis_args)
        for bh in path:
            chain.submit_block(self.blocks[bh])
        return chain

    # -- mutation ---------------------------------------------------------- #
    def add_block(self, block: Block) -> bool:
        """Validate a block against its parent's state, store it, and reorg if needed.

        Returns True if newly accepted, False if already known. Raises on invalid/orphan.
        """
        bh = block.hash
        if bh in self.weight:
            return False
        ph = block.previous_hash
        if ph not in self.weight:
            raise ValidationError("unknown parent (orphan block)")

        # Finality guard #1: a block may not conflict with the finalized prefix. A block at
        # or below the finalized height must BE the finalized block; a block above it must
        # descend from the finalized checkpoint. (A conflicting block useful only as slash
        # evidence travels inside a slash tx, not into the tree.)
        new_height = self.height[ph] + 1
        if new_height <= self.finalized_height and bh != self.finalized_hash:
            raise ValidationError("block conflicts with the finalized prefix")
        if new_height > self.finalized_height and not self._descends_from_finalized(ph):
            raise ValidationError("block does not descend from the finalized checkpoint")
        # Weak-subjectivity: the block at the trusted checkpoint height must be the trusted hash.
        if self.ws_checkpoint and new_height == self.ws_checkpoint[1] and bh != self.ws_checkpoint[0]:
            raise ValidationError("block conflicts with the weak-subjectivity checkpoint")

        # Validate against the PARENT's state (not necessarily the current head).
        parent_chain = self.chain_at(ph)
        proposer_stake = parent_chain.stake_of(block.validator)  # weight from parent state
        parent_chain.submit_block(block)  # raises ValidationError if the block is invalid

        self.blocks[bh] = block
        self.parent[bh] = ph
        self.height[bh] = new_height
        self.weight[bh] = self.weight[ph] + proposer_stake
        # Advance the finalized frontier from the new block's branch BEFORE choosing the
        # head, so the head scan filters against the current frontier.
        fh, fhgt = parent_chain.finalized_checkpoint()
        if fhgt > self.finalized_height:
            # Safety backstop: the new finalized checkpoint MUST extend the current one. If
            # this branch finalized something that conflicts with our finalized prefix, that
            # is an attributable >=1/3 stake fault -- halt loudly rather than silently fork.
            branch = parent_chain.chain
            if (self.finalized_height < len(branch)
                    and branch[self.finalized_height].hash != self.finalized_hash):
                raise ConflictingFinalityError(
                    f"conflicting finality: branch finalized {fh}@{fhgt} but "
                    f"{self.finalized_hash}@{self.finalized_height} is already final"
                )
            self.finalized_hash, self.finalized_height = fh, fhgt
        self._update_head()
        return True

    def _better(self, a: str, b: str) -> bool:
        if self.weight[a] != self.weight[b]:
            return self.weight[a] > self.weight[b]
        return a < b  # deterministic tie-break

    def _descends_from_finalized(self, block_hash: str) -> bool:
        cur = block_hash
        while self.height.get(cur, 0) > self.finalized_height:
            cur = self.parent.get(cur)
            if cur is None:
                return False
        return cur == self.finalized_hash

    def _update_head(self) -> None:
        # Finality guard #2: only leaves that descend from the finalized checkpoint are
        # eligible. A heavier branch that conflicts with finality can NEVER win.
        best = self.finalized_hash
        for h in self.weight:
            if self._descends_from_finalized(h) and self._better(h, best):
                best = h
        if best != self.head_hash:
            self.head_hash = best
            self.canonical = self.chain_at(best)  # reorg: rebuild canonical state


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #
def _demo() -> None:
    from consensus import seal_block
    from core import COIN, MAX_SUPPLY_UNITS, Wallet

    v1, v2 = Wallet.from_secret(1), Wallet.from_secret(2)
    wallets = {v1.address: v1, v2.address: v2}
    allocations = {v1.address: MAX_SUPPLY_UNITS - 1000 * COIN, v2.address: 1000 * COIN}
    initial_stakes = {v1.address: 1000 * COIN, v2.address: 1000 * COIN}
    ts = 1_700_000_000

    tree = BlockTree(allocations, initial_stakes, ts)
    assert tree.head.index == 0

    # Equivocation: the elected proposer for height 1 signs TWO different blocks
    # (different timestamps) on the same genesis parent. Both are individually valid.
    elected = tree.canonical.next_validator()
    proposer = wallets[elected]
    block_a = seal_block(tree.genesis_hash, 1, proposer, [], ts + 1)
    block_b = seal_block(tree.genesis_hash, 1, proposer, [], ts + 2)
    assert block_a.hash != block_b.hash

    assert tree.add_block(block_a) is True
    assert tree.add_block(block_b) is True
    assert tree.add_block(block_a) is False  # already known

    # Both weigh the same (same proposer stake) -> deterministic tie-break = smaller hash.
    assert tree.head.index == 1
    assert tree.head_hash == min(block_a.hash, block_b.hash)

    # Extend the CURRENTLY-LOSING branch so it outweighs the canonical one -> reorg.
    loser = block_b if tree.head_hash == block_a.hash else block_a
    loser_chain = tree.chain_at(loser.hash)
    elected2 = loser_chain.next_validator()
    block_c = seal_block(loser.hash, 2, wallets[elected2], [], ts + 3)
    assert tree.add_block(block_c) is True

    # The two-block branch (weight = 2 stakes) now beats the one-block branch (1 stake).
    assert tree.head_hash == block_c.hash
    assert tree.head.index == 2
    assert tree.canonical.head.previous_hash == loser.hash
    assert tree.canonical.total_supply() == MAX_SUPPLY_UNITS  # state intact after reorg

    # Orphans (unknown parent) are refused.
    orphan = seal_block("00" * 32, 1, proposer, [], ts + 9)
    try:
        tree.add_block(orphan)
        raise AssertionError("orphan block should be refused")
    except ValidationError:
        pass

    # Weak-subjectivity checkpoint: a node pinned to a trusted (hash, height) rejects any
    # different block at that height -- a long-range fork can't reproduce it and can't take over.
    g = BlockTree(allocations, initial_stakes, ts)
    p = wallets[g.canonical.next_validator()]
    trusted = seal_block(g.genesis_hash, 1, p, [], ts + 1)
    g.add_block(trusted)
    pinned = BlockTree(allocations, initial_stakes, ts, checkpoint=(trusted.hash, 1))
    assert pinned.add_block(trusted) is True            # the trusted block at height 1 is fine
    impostor = seal_block(pinned.genesis_hash, 1, p, [], ts + 777)  # different block, same height
    assert impostor.hash != trusted.hash
    try:
        pinned.add_block(impostor)
        raise AssertionError("a block conflicting with the checkpoint must be rejected")
    except ValidationError:
        pass

    print("ALL CHECKS PASSED")
    print("  fork choice: equivocation forks resolved deterministically, heavier branch wins")
    print(f"  reorg rebuilt canonical state to height {tree.head.index}, supply intact")


if __name__ == "__main__":
    _demo()
