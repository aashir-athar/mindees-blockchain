"""
Mindees mempool + fee market  --  Phase 3.

The pool of pending transactions a validator draws from when it builds a block,
plus the rule that decides *which* pending transactions get in: a fee market.

Design (ponytail: the smallest thing that is actually correct):
  * Admission is cheap and stateless-ish -- signature/shape must be valid, the fee
    must clear a floor, and the nonce must not already be spent. Everything else
    (does the sender really have the funds, is the block valid) is decided by the
    chain when the block is applied; the mempool only has to avoid proposing a
    block the chain will reject.
  * Selection IS the fee market: across senders we always take the highest-fee
    transaction available, but within a single sender we never reorder nonces.
    Higher fee => included sooner. That is the entire incentive.
  * A block has a capacity (uniform tx weight for now). Whatever doesn't fit waits
    for the next block, which is exactly how a fee market is supposed to behave.

Self-testing: run directly ->  python mempool.py
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from consensus import SLASH_SENTINEL, UNSTAKE_SENTINEL, VOTE_SENTINEL
from core import Blockchain, Transaction, ValidationError

# ponytail: uniform tx weight + zero fee floor. Swap MAX_TXS_PER_BLOCK for a gas
# meter when transactions stop being uniform cost; raise MIN_FEE if spam appears.
MAX_TXS_PER_BLOCK = 5000
MIN_FEE = 0
# DoS bound: a full pool keeps only the highest-fee transactions. A new tx is admitted
# only if it out-bids the cheapest one already queued, which is then evicted.
# ponytail: O(n) min-scan on insert when full -- fine until the pool is huge; switch to
# a fee-indexed heap if eviction ever shows up in a profile.
MAX_POOL_SIZE = 50_000


def _cost(tx: Transaction, unbonding_blocks: int = 0) -> Tuple[int, int]:
    """(minimum liquid balance required before this tx, change to liquid balance after).

    Mirrors the chain's apply rules so selection never proposes an unaffordable set:
      * transfer / stake : debit amount + fee
      * slash            : only the fee leaves liquid balance (amount is 0)
      * unstake (instant): only the fee leaves; amount returns to balance
      * unstake (delayed): only the fee leaves; amount goes to unbonding, NOT back to balance
    """
    if tx.recipient in (SLASH_SENTINEL, VOTE_SENTINEL):
        return tx.fee, -tx.fee
    if tx.recipient == UNSTAKE_SENTINEL:
        if unbonding_blocks > 0:
            return tx.fee, -tx.fee
        return tx.fee, tx.amount - tx.fee
    return tx.amount + tx.fee, -(tx.amount + tx.fee)


class Mempool:
    def __init__(self) -> None:
        self.pool: Dict[str, Transaction] = {}  # txid -> tx

    def __len__(self) -> int:
        return len(self.pool)

    def add(self, tx: Transaction, chain: Blockchain) -> bool:
        """Admit a transaction. Returns False if already known; raises on invalid."""
        if not tx.is_valid():
            raise ValidationError("rejected: invalid signature or shape")
        if tx.fee < MIN_FEE:
            raise ValidationError(f"rejected: fee below floor {MIN_FEE}")
        if tx.nonce < chain.nonces.get(tx.sender, 0):
            raise ValidationError("rejected: nonce already spent")
        if tx.txid in self.pool:
            return False
        if len(self.pool) >= MAX_POOL_SIZE:
            cheapest = min(self.pool.values(), key=lambda t: t.fee)
            if tx.fee <= cheapest.fee:
                raise ValidationError("rejected: mempool full and fee does not out-bid")
            del self.pool[cheapest.txid]
        self.pool[tx.txid] = tx
        return True

    def select(self, chain: Blockchain, max_count: int = MAX_TXS_PER_BLOCK) -> List[Transaction]:
        """Build a block-ready, fee-ordered, nonce-respecting, affordable tx list."""
        by_sender: Dict[str, List[Transaction]] = {}
        for tx in self.pool.values():
            by_sender.setdefault(tx.sender, []).append(tx)

        # Per sender: the contiguous, affordable run starting at their next nonce.
        ready: Dict[str, List[Transaction]] = {}
        for sender, txs in by_sender.items():
            txs.sort(key=lambda t: t.nonce)
            expected = chain.nonces.get(sender, 0)
            balance = chain.balance_of(sender)
            run: List[Transaction] = []
            unbonding_blocks = getattr(chain, "unbonding_blocks", 0)
            for t in txs:
                if t.nonce != expected:
                    break  # nonce gap (or already spent) -> stop this sender
                required_before, delta = _cost(t, unbonding_blocks)
                if balance < required_before:
                    break  # can't afford -> stop this sender
                balance += delta
                run.append(t)
                expected += 1
            if run:
                ready[sender] = run

        # Fee-priority merge: repeatedly take the highest-fee head across senders,
        # which keeps global fee ordering while preserving per-sender nonce order.
        heads = {s: 0 for s in ready}
        selected: List[Transaction] = []
        while len(selected) < max_count:
            best_sender = None
            best_fee = -1
            for sender, idx in heads.items():
                if idx < len(ready[sender]) and ready[sender][idx].fee > best_fee:
                    best_fee = ready[sender][idx].fee
                    best_sender = sender
            if best_sender is None:
                break
            selected.append(ready[best_sender][heads[best_sender]])
            heads[best_sender] += 1
        return selected

    def update(self, chain: Blockchain) -> None:
        """Drop transactions a new block consumed or that turned invalid."""
        self.pool = {
            txid: tx
            for txid, tx in self.pool.items()
            if tx.nonce >= chain.nonces.get(tx.sender, 0) and tx.is_valid()
        }


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #
def _demo() -> None:
    from core import COIN, MAX_SUPPLY_UNITS, Wallet

    alice = Wallet.from_secret(1)
    bob = Wallet.from_secret(2)
    carol = Wallet.from_secret(3)

    chain = Blockchain({alice.address: MAX_SUPPLY_UNITS}, timestamp=1_700_000_000)
    # Fund Bob so two senders compete in the fee market.
    seed_tx = Transaction(alice.address, bob.address, 1000 * COIN, 0, 0).sign(alice)
    chain.add_block([seed_tx], validator=alice.address, timestamp=1_700_000_010)
    assert chain.balance_of(bob.address) == 1000 * COIN  # alice nonce now 1, bob nonce 0

    mp = Mempool()

    # Admission rules.
    a1 = Transaction(alice.address, carol.address, 10 * COIN, 2 * COIN, 1).sign(alice)
    a2 = Transaction(alice.address, carol.address, 10 * COIN, 9 * COIN, 2).sign(alice)
    b0 = Transaction(bob.address, carol.address, 5 * COIN, 5 * COIN, 0).sign(bob)
    b1 = Transaction(bob.address, carol.address, 5 * COIN, 1 * COIN, 1).sign(bob)
    assert mp.add(a1, chain) and mp.add(a2, chain) and mp.add(b0, chain) and mp.add(b1, chain)
    assert mp.add(a1, chain) is False  # duplicate is idempotent, not an error
    assert len(mp) == 4

    # Stale nonce rejected (alice's nonce 0 was already spent by the seed block).
    stale = Transaction(alice.address, carol.address, 1 * COIN, 1 * COIN, 0).sign(alice)
    try:
        mp.add(stale, chain)
        raise AssertionError("stale nonce should be rejected")
    except ValidationError:
        pass

    # Forged signature rejected.
    forged = Transaction(alice.address, carol.address, 1 * COIN, 1 * COIN, 3)
    forged.public_key = bob.public_key_hex
    forged.signature = bob.sign(forged._signing_payload()).hex()
    try:
        mp.add(forged, chain)
        raise AssertionError("forged tx should be rejected")
    except ValidationError:
        pass

    # Fee market: highest-fee head wins across senders; nonce order kept per sender.
    selected = mp.select(chain, max_count=10)
    assert [t.txid for t in selected] == [b0.txid, a1.txid, a2.txid, b1.txid]
    # b0(fee5) beats a1(fee2) first; then a1 before a2 (alice nonce order);
    # a2(fee9) beats b1(fee1); b1 last.
    assert _sender_nonces_ordered(selected)

    # The selected set is actually applicable (atomic block accepts it).
    chain.add_block(selected, validator=carol.address, timestamp=1_700_000_020)
    assert chain.total_supply() == MAX_SUPPLY_UNITS
    assert chain.nonces[alice.address] == 3 and chain.nonces[bob.address] == 2

    # Pruning: everything we mined leaves the pool.
    mp.update(chain)
    assert len(mp) == 0

    # Capacity: only `max_count` come back.
    mp2 = Mempool()
    txs = [Transaction(alice.address, bob.address, 1 * COIN, i, 3 + i).sign(alice) for i in range(5)]
    for t in txs:
        mp2.add(t, chain)
    assert len(mp2.select(chain, max_count=2)) == 2

    # Balance limit: a sender's run stops when they can no longer afford the next tx.
    poor = Wallet.from_secret(9)
    chain.add_block(
        [Transaction(alice.address, poor.address, 15 * COIN, 0, 3).sign(alice)],
        validator=alice.address,
        timestamp=1_700_000_030,
    )
    mp3 = Mempool()
    p0 = Transaction(poor.address, bob.address, 10 * COIN, 0, 0).sign(poor)
    p1 = Transaction(poor.address, bob.address, 10 * COIN, 0, 1).sign(poor)  # unaffordable
    mp3.add(p0, chain)
    mp3.add(p1, chain)
    assert [t.txid for t in mp3.select(chain)] == [p0.txid]  # only the affordable prefix

    # _cost honours unstake semantics (fee leaves balance, stake amount returns).
    utx = Transaction(alice.address, UNSTAKE_SENTINEL, 7 * COIN, 1 * COIN, 99)
    assert _cost(utx) == (1 * COIN, 7 * COIN - 1 * COIN)

    # DoS bound: a full pool only keeps the highest-fee txs; a low-fee tx is refused,
    # a higher-fee one evicts the cheapest. (Patch the cap small for the test; add()
    # reads this module's global, so patch it via globals() to work under __main__.)
    saved_cap = MAX_POOL_SIZE
    globals()["MAX_POOL_SIZE"] = 3
    try:
        mp4 = Mempool()
        for i in range(3):
            mp4.add(Transaction(alice.address, bob.address, 1 * COIN, 5 + i, 100 + i).sign(alice), chain)
        assert len(mp4) == 3
        # fee below the cheapest (5) -> refused, pool unchanged
        try:
            mp4.add(Transaction(alice.address, bob.address, 1 * COIN, 1, 200).sign(alice), chain)
            raise AssertionError("low-fee tx should be refused by a full pool")
        except ValidationError:
            pass
        assert len(mp4) == 3
        # fee above the cheapest -> admitted, cheapest (fee 5) evicted
        mp4.add(Transaction(alice.address, bob.address, 1 * COIN, 99, 201).sign(alice), chain)
        assert len(mp4) == 3
        assert min(t.fee for t in mp4.pool.values()) == 6  # the fee-5 tx was evicted
    finally:
        globals()["MAX_POOL_SIZE"] = saved_cap

    print("ALL CHECKS PASSED")
    print("  mempool/fee-market: fee-ordered selection, nonce-safe, affordable-only")
    print(f"  block capacity = {MAX_TXS_PER_BLOCK} txs, fee floor = {MIN_FEE}")


def _sender_nonces_ordered(txs: List[Transaction]) -> bool:
    last: Dict[str, int] = {}
    for t in txs:
        if t.sender in last and t.nonce <= last[t.sender]:
            return False
        last[t.sender] = t.nonce
    return True


if __name__ == "__main__":
    _demo()
