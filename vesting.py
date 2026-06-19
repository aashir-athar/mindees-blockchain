"""
Mindees vesting math  --  Phase 8.

Linear-with-cliff token release. Pure functions of (schedule, height) -- no state, no I/O,
so it is trivial to reason about and reuse on-chain and in tooling.

A grant is (total, start, cliff, duration) in base units and block heights:
  * nothing releases before height (start + cliff),
  * then it releases linearly, reaching `total` at height (start + duration).

The chain ENFORCES a grant by requiring the holder's (balance + stake) never falls below
`locked(...)` at the current block height. So locked coins may be staked (the founder can
still validate) but can never be sent away until they vest -- which is exactly what stops a
25% premine from being dumped on day one.

Run directly ->  python vesting.py
"""
from __future__ import annotations


def vested(total: int, start: int, cliff: int, duration: int, height: int) -> int:
    """Base units released by `height`."""
    if duration <= 0:
        return total  # no schedule -> nothing is locked
    if height < start + cliff:
        return 0
    if height >= start + duration:
        return total
    return total * (height - start) // duration


def locked(total: int, start: int, cliff: int, duration: int, height: int) -> int:
    """Base units still locked at `height` (the complement of `vested`)."""
    return total - vested(total, start, cliff, duration, height)


def _demo() -> None:
    T = 250_000

    # Cliff at height 2, fully vested at height 4, starting at 0.
    assert locked(T, 0, 2, 4, 0) == T            # genesis: fully locked
    assert locked(T, 0, 2, 4, 1) == T            # still before the cliff
    assert locked(T, 0, 2, 4, 2) == T - (T * 2 // 4)  # cliff releases the linear point
    assert 0 < locked(T, 0, 2, 4, 3) < T         # mid-vest
    assert locked(T, 0, 2, 4, 4) == 0            # fully vested
    assert locked(T, 0, 2, 4, 99) == 0           # stays vested

    # Release is monotonic non-increasing in locked amount.
    prev = T + 1
    for h in range(0, 12):
        cur = locked(T, 0, 2, 4, h)
        assert cur <= prev, "lock must never increase"
        prev = cur

    # No schedule (duration 0) means nothing is ever locked.
    assert locked(T, 0, 0, 0, 0) == 0

    print("ALL CHECKS PASSED")
    print("  vesting: cliff + linear release, monotonic, pure function of height")


if __name__ == "__main__":
    _demo()
