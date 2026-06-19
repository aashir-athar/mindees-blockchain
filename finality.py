"""
Mindees finality gadget gates  --  Phase 11 (Casper-FFG).

Exercises the accountable finality gadget end to end. Finality votes are ordinary signed
transactions to VOTE_SENTINEL carrying an FFG link {source, target} in tx.evidence; a
checkpoint (a block at an epoch boundary) JUSTIFIES at >= 2/3 of the source checkpoint's
active stake, and FINALIZES when its direct epoch child justifies. Fork choice may never
reorg at or below a finalized checkpoint.

Honest bound: ACCOUNTABLE safety under < 1/3 Byzantine stake -- two conflicting checkpoints
finalize only if >= 1/3 of source-active stake casts a slashable double/surround vote.
Liveness: under < 2/3 participation finality STALLS but block production keeps going; it
never deadlocks and never finalizes wrongly. NOT solved in v1: inactivity-leak recovery,
BLS aggregation, weak-subjectivity sync (see consensus.py honest-scope notes).

Run directly ->  python finality.py
"""
from __future__ import annotations

from consensus import (
    REPORTER_BPS,
    ProofOfStakeChain,
    seal_block,
    verify_vote_fault,
    vote_slash_tx,
    vote_tx,
)
from core import COIN, MAX_SUPPLY_UNITS, VOTE_SENTINEL, Transaction, ValidationError, Wallet
from forkchoice import BlockTree

GTS = 1_700_000_000
_S = 1000 * COIN  # stake per validator

V1, V2, V3 = Wallet.from_secret(1), Wallet.from_secret(2), Wallet.from_secret(3)
ALICE, TREASURY = Wallet.from_secret(4), Wallet.from_secret(5)
VALS = {V1.address: V1, V2.address: V2, V3.address: V3}


def _genesis_args(epoch=2, unbonding_blocks=10):
    allocations = {
        ALICE.address: MAX_SUPPLY_UNITS - 3 * _S,
        V1.address: _S, V2.address: _S, V3.address: _S,
    }
    stakes = {V1.address: _S, V2.address: _S, V3.address: _S}
    return (allocations, stakes, GTS, None, unbonding_blocks, TREASURY.address, epoch)


def _make_chain(epoch=2, unbonding_blocks=10) -> ProofOfStakeChain:
    return ProofOfStakeChain(*_genesis_args(epoch, unbonding_blocks))


def _votes(chain, voters, s, sh, t, th):
    return [vote_tx(v, s, sh, t, th, chain.nonces.get(v.address, 0)) for v in voters]


def _produce(chain, txs, ts):
    return chain.add_block(txs, VALS[chain.next_validator()], ts)


def _happy_path(chain):
    """Drive a chain to finalize the height-2 checkpoint. Returns (genesis, c2, c4)."""
    g = chain.head.hash
    _produce(chain, [], GTS + 1)                                  # height 1
    _produce(chain, [], GTS + 2)                                  # height 2 -> C2
    c2 = chain.head.hash
    _produce(chain, _votes(chain, [V1, V2], g, 0, c2, 2), GTS + 3)  # justify C2
    _produce(chain, [], GTS + 4)                                  # height 4 -> C4
    c4 = chain.head.hash
    _produce(chain, _votes(chain, [V1, V2], c2, 2, c4, 4), GTS + 5)  # justify C4 -> finalize C2
    return g, c2, c4


def _gate_justify_finalize():
    chain = _make_chain()
    g, c2, c4 = _happy_path(chain)
    assert chain.is_justified(c2) and chain.is_justified(c4)
    assert chain.finalized_checkpoint() == (c2, 2)   # C2 is final, supply intact
    assert chain.total_supply() == MAX_SUPPLY_UNITS


def _gate_determinism():
    # Build on a tree (exercises chain_at replay), then reload from disk; finality must match.
    import shutil
    import tempfile

    from storage import BlockStore

    tree = BlockTree(*_genesis_args())

    def tprod(txs, ts):
        proposer = VALS[tree.canonical.next_validator()]
        blk = seal_block(tree.head.hash, tree.head.index + 1, proposer, txs, ts)
        tree.add_block(blk)
        return blk

    def tvotes(voters, s, sh, t, th):
        return [vote_tx(v, s, sh, t, th, tree.canonical.nonces.get(v.address, 0)) for v in voters]

    g = tree.genesis_hash
    tprod([], GTS + 1)
    tprod([], GTS + 2)
    c2 = tree.head.hash
    tprod(tvotes([V1, V2], g, 0, c2, 2), GTS + 3)
    tprod([], GTS + 4)
    c4 = tree.head.hash
    tprod(tvotes([V1, V2], c2, 2, c4, 4), GTS + 5)

    assert tree.canonical.finalized_checkpoint() == (c2, 2)
    assert tree.finalized_hash == c2 and tree.finalized_height == 2
    # chain_at replays are deterministic.
    assert tree.chain_at(tree.head_hash).finalized_checkpoint() == (c2, 2)

    # Disk reload (a BARE ProofOfStakeChain, NO tree) must derive identical finality purely
    # from replayed blocks -- the determinism fix (snapshots in _apply_block_pre, no chain_at).
    tmp = tempfile.mkdtemp(prefix="mindees_ffg_")
    try:
        store = BlockStore(tmp)
        allocations, stakes, ts0, vesting, ub, treas, epoch = _genesis_args()
        store.write_genesis(allocations, stakes, ts0, vesting, ub, treas, epoch)
        for block in tree.canonical.chain[1:]:
            store.append(block)
        reloaded = store.load()
        assert reloaded.finalized_checkpoint() == (c2, 2)
        assert reloaded.is_justified(c2) and reloaded.is_justified(c4)
        assert reloaded.total_supply() == MAX_SUPPLY_UNITS
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _gate_irreversibility():
    # GUARD #1: a block at/below the finalized height that isn't the finalized block is refused.
    tree = BlockTree(*_genesis_args())

    def tprod(txs, ts):
        proposer = VALS[tree.canonical.next_validator()]
        blk = seal_block(tree.head.hash, tree.head.index + 1, proposer, txs, ts)
        tree.add_block(blk)
        return blk

    def tvotes(voters, s, sh, t, th):
        return [vote_tx(v, s, sh, t, th, tree.canonical.nonces.get(v.address, 0)) for v in voters]

    g = tree.genesis_hash
    h1_proposer = VALS[tree.canonical.next_validator()]  # the elected proposer for height 1
    tprod([], GTS + 1)
    tprod([], GTS + 2)
    c2 = tree.head.hash
    tprod(tvotes([V1, V2], g, 0, c2, 2), GTS + 3)
    tprod([], GTS + 4)
    c4 = tree.head.hash
    tprod(tvotes([V1, V2], c2, 2, c4, 4), GTS + 5)
    assert tree.finalized_height == 2

    # A conflicting block at height 1 (an equivocation of the canonical h1) is below the
    # finalized height -> rejected, so the finalized prefix can't be rewritten.
    conflicting = seal_block(g, 1, h1_proposer, [], GTS + 999)
    try:
        tree.add_block(conflicting)
        raise AssertionError("a block below the finalized height must be rejected")
    except ValidationError:
        pass
    assert tree.head.index >= 4  # head still on the finalized branch
    assert tree.canonical.total_supply() == MAX_SUPPLY_UNITS

    # GUARD #2 (independent): a heavier NON-descendant of the finalized checkpoint can never
    # win the head, even when it is already in the tree. Build two branches, B heavier, then
    # finalize A1 and confirm the head moves OFF heavier B onto A1.
    t2 = BlockTree(*_genesis_args())
    p1 = VALS[t2.canonical.next_validator()]
    a1 = seal_block(t2.genesis_hash, 1, p1, [], GTS + 1)
    t2.add_block(a1)
    b1 = seal_block(t2.genesis_hash, 1, p1, [], GTS + 2)  # equivocation, different hash
    t2.add_block(b1)
    b2 = seal_block(b1.hash, 2, VALS[t2.chain_at(b1.hash).next_validator()], [], GTS + 3)
    t2.add_block(b2)
    b3 = seal_block(b2.hash, 3, VALS[t2.chain_at(b2.hash).next_validator()], [], GTS + 4)
    t2.add_block(b3)
    assert t2.head_hash == b3.hash        # heavier branch B currently wins
    # Simulate A1 becoming finalized; the descendant filter must drop heavier B.
    t2.finalized_hash, t2.finalized_height = a1.hash, 1
    t2._update_head()
    assert t2.head_hash == a1.hash        # finality beats weight


def _gate_vote_fault_slashing():
    chain = _make_chain()
    g, c2, c4 = _happy_path(chain)  # V1, V2 voted honestly; V3 has not

    # DOUBLE vote: V3 signs two links with the same target height but different targets.
    da = vote_tx(V3, g, 0, "aa" * 32, 2, 0)
    db = vote_tx(V3, g, 0, "bb" * 32, 2, 0)
    offender, offense_id = verify_vote_fault(da, db)
    assert offender == V3.address and isinstance(offense_id, str)  # hex str, not a tuple

    # SURROUND vote: one link strictly surrounds the other.
    sa = vote_tx(V3, g, 0, "cc" * 32, 8, 0)   # (0 -> 8)
    sb = vote_tx(V3, "dd" * 32, 2, "ee" * 32, 4, 0)  # (2 -> 4), surrounded by (0 -> 8)
    off2, _ = verify_vote_fault(sa, sb)
    assert off2 == V3.address

    # HONEST single voter is NOT slashable: V1's two sequential votes don't surround/double.
    h1 = vote_tx(V1, g, 0, c2, 2, 0)
    h2 = vote_tx(V1, c2, 2, c4, 4, 0)
    try:
        verify_vote_fault(h1, h2)
        raise AssertionError("honest non-overlapping votes must not be a fault")
    except ValidationError:
        pass

    # Slash V3 for the double vote; supply preserved, bond redistributed 5%/95%.
    v3_stake = chain.stake_of(V3.address)
    treas_before = chain.balance_of(TREASURY.address)
    rep_before = chain.balance_of(ALICE.address)
    slash = vote_slash_tx(ALICE, da, db, chain.nonces.get(ALICE.address, 0))
    _produce(chain, [slash], GTS + 10)
    assert chain.stake_of(V3.address) == 0
    assert chain.balance_of(ALICE.address) == rep_before + v3_stake * REPORTER_BPS // 10000
    assert chain.balance_of(TREASURY.address) == treas_before + v3_stake - v3_stake * REPORTER_BPS // 10000
    assert chain.total_supply() == MAX_SUPPLY_UNITS

    # Re-reporting the same offense is rejected (dedup).
    try:
        _produce(chain, [vote_slash_tx(ALICE, da, db, chain.nonces.get(ALICE.address, 0))], GTS + 11)
        raise AssertionError("double slash of the same vote-fault must be rejected")
    except ValidationError:
        pass


def _gate_liveness():
    chain = _make_chain()
    g = chain.head.hash
    for i in range(1, 7):                  # 6 blocks, NO votes -> finality must stall
        _produce(chain, [], GTS + i)
    assert chain.head.index == 6
    assert chain.finalized_checkpoint() == (g, 0)   # stalled, but the chain kept advancing

    # Votes resume (late votes for old checkpoints are still valid) -> finality progresses.
    c2 = chain.chain[2].hash
    c4 = chain.chain[4].hash
    _produce(chain, _votes(chain, [V1, V2], g, 0, c2, 2), GTS + 7)
    assert chain.is_justified(c2)
    _produce(chain, _votes(chain, [V1, V2], c2, 2, c4, 4), GTS + 8)
    assert chain.finalized_checkpoint() == (c2, 2)   # finality recovered, no deadlock


def _gate_vote_admission():
    chain = _make_chain()
    g = chain.head.hash
    _produce(chain, [], GTS + 1)
    _produce(chain, [], GTS + 2)
    c2 = chain.head.hash

    # Stateless shape: amount != 0 or empty evidence is invalid.
    assert not Transaction(V1.address, VOTE_SENTINEL, 5, 0, 0, evidence="x").sign(V1).is_valid()
    assert not Transaction(V1.address, VOTE_SENTINEL, 0, 0, 0).sign(V1).is_valid()

    n = chain.nonces.get(V1.address, 0)
    # Non-epoch-boundary target height.
    try:
        _produce(chain, [vote_tx(V1, g, 0, c2, 3, n)], GTS + 3)
        raise AssertionError("non-epoch target must be rejected")
    except ValidationError:
        pass
    # Zero-stake voter (alice is not a validator).
    try:
        _produce(chain, [vote_tx(ALICE, g, 0, c2, 2, chain.nonces.get(ALICE.address, 0))], GTS + 4)
        raise AssertionError("zero-stake voter must be rejected")
    except ValidationError:
        pass
    # Source not yet justified: vote C2 -> C4 before C2 is justified.
    _produce(chain, [], GTS + 5)  # height 3
    _produce(chain, [], GTS + 6)  # height 4 -> C4
    c4 = chain.head.hash
    try:
        _produce(chain, [vote_tx(V1, c2, 2, c4, 4, chain.nonces.get(V1.address, 0))], GTS + 7)
        raise AssertionError("vote from an unjustified source must be rejected")
    except ValidationError:
        pass
    # Conflicting second vote for the same (voter, target_height).
    _produce(chain, _votes(chain, [V1, V2], g, 0, c2, 2), GTS + 8)  # justify C2
    try:
        _produce(chain, [vote_tx(V1, g, 0, "ff" * 32, 2, chain.nonces.get(V1.address, 0))], GTS + 9)
        raise AssertionError("a conflicting second vote for the same target height must be rejected")
    except ValidationError:
        pass


def _demo() -> None:
    _gate_justify_finalize()
    _gate_determinism()
    _gate_irreversibility()
    _gate_vote_fault_slashing()
    _gate_liveness()
    _gate_vote_admission()
    print("ALL CHECKS PASSED")
    print("  finality (Casper-FFG): justify -> finalize, deterministic across reload + replay")
    print("  irreversible below finalized (both fork-choice guards); vote faults slashed")
    print("  accountable safety < 1/3 Byzantine; liveness stalls but never deadlocks; supply intact")


if __name__ == "__main__":
    _demo()
