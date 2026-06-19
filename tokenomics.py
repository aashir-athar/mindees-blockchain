"""
Mindees genesis distribution (tokenomics)  --  policy as tested code.

The fixed 1,000,000 MIND supply is split at genesis:

    Market / treasury .....  750,000 MIND   (75%)  -> liquidity, sale, distribution
    Founder ...............  250,000 MIND   (25%)  -> the project wallet

Both are minted ONCE at genesis (the only place coins ever come into existence) and the
split is enforced to sum to exactly the cap. Staking does not change the split: when the
founder stakes part of their 250,000 to run the genesis validator, those coins still
belong to the founder -- they are just locked as stake, and the total stays 250,000.

HONEST NOTE (kept in the open, per the project's no-overclaim rule): a 25% founder
allocation minted *liquid* at genesis is a centralisation / "insider premine" signal --
exactly the kind of thing that hurts credible neutrality, the axis we already conceded to
BTC. The production-grade mitigation is a vesting / time-lock on the founder share so it
releases gradually instead of being spendable on day one. That is not built here (the
chain has no time-locks yet); it is a one-call follow-up if wanted. This module ships the
split as instructed.

Self-testing: run with no args ->  python tokenomics.py
CLI:                              python tokenomics.py init --data ./chaindata [...]
"""
from __future__ import annotations

from core import COIN, MAX_SUPPLY_UNITS, SYMBOL, Wallet
from storage import BlockStore

MARKET_SUPPLY = 750_000   # whole MIND to market / treasury
FOUNDER_SUPPLY = 250_000  # whole MIND to the founder wallet (25%)
FOUNDER_PERCENT = 25

# Fail loudly at import time if the policy constants ever stop summing to the cap.
assert (MARKET_SUPPLY + FOUNDER_SUPPLY) * COIN == MAX_SUPPLY_UNITS
assert FOUNDER_SUPPLY * 100 == FOUNDER_PERCENT * (MARKET_SUPPLY + FOUNDER_SUPPLY)


def build_genesis(
    market_addr: str,
    founder_addr: str,
    founder_stake_coins: int = 0,
    vest_cliff: int = 0,
    vest_duration: int = 0,
):
    """Return (allocations, initial_stakes, vesting) for the 750k/250k split.

    founder_stake_coins of the founder's 250,000 are locked as validator stake so the
    chain has an electable proposer from block 1. The split is unaffected.

    If vest_duration > 0 the founder's whole 250,000 vests over `vest_duration` blocks
    with a `vest_cliff`-block cliff (start = genesis, height 0): the founder can validate
    throughout but cannot send those coins away until they vest. This is the mitigation
    for a liquid 25% premine. Leave vest_duration = 0 for an immediately-liquid founder.
    """
    if market_addr == founder_addr:
        raise ValueError("market and founder addresses must differ")
    if not 0 <= founder_stake_coins <= FOUNDER_SUPPLY:
        raise ValueError(f"founder stake must be within the {FOUNDER_SUPPLY:,} MIND founder allocation")

    allocations = {
        market_addr: MARKET_SUPPLY * COIN,
        founder_addr: FOUNDER_SUPPLY * COIN,
    }
    if sum(allocations.values()) != MAX_SUPPLY_UNITS:
        raise ValueError(f"distribution must sum to {MAX_SUPPLY_UNITS} units")

    initial_stakes = {founder_addr: founder_stake_coins * COIN} if founder_stake_coins else {}
    vesting = {}
    if vest_duration > 0:
        # (total, start, cliff, duration) in base units / block heights
        vesting = {founder_addr: (FOUNDER_SUPPLY * COIN, 0, vest_cliff, vest_duration)}
    return allocations, initial_stakes, vesting


def _cli(argv=None) -> None:
    import argparse

    from wallet import new_secret

    parser = argparse.ArgumentParser(prog="tokenomics", description="write the Mindees genesis split")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="write genesis with the 750k market / 250k founder split")
    p.add_argument("--data", required=True)
    p.add_argument("--market", default=None, help="market address (generated if omitted)")
    p.add_argument("--founder", default=None, help="founder address (generated if omitted)")
    p.add_argument("--founder-stake", type=int, default=50_000, help="founder coins staked to validate")
    p.add_argument("--vest-cliff", type=int, default=0, help="blocks before founder coins start vesting")
    p.add_argument("--vest-duration", type=int, default=0, help="blocks to fully vest founder coins (0=off)")
    p.add_argument("--treasury", default=None, help="treasury address for slash receipts (generated if omitted)")
    p.add_argument("--unbonding-blocks", type=int, default=100,
                   help="delay before unstaked coins go liquid; must exceed equivocation-detection latency")
    p.add_argument("--timestamp", type=int, default=1_700_000_000)
    args = parser.parse_args(argv)

    generated = {}
    market_addr = args.market
    founder_addr = args.founder
    treasury_addr = args.treasury
    if market_addr is None:
        s = new_secret()
        market_addr = Wallet.from_secret(int(s, 16)).address
        generated["market"] = s
    if founder_addr is None:
        s = new_secret()
        founder_addr = Wallet.from_secret(int(s, 16)).address
        generated["founder"] = s
    if treasury_addr is None:
        s = new_secret()
        treasury_addr = Wallet.from_secret(int(s, 16)).address
        generated["treasury"] = s

    allocations, initial_stakes, vesting = build_genesis(
        market_addr, founder_addr, args.founder_stake, args.vest_cliff, args.vest_duration
    )
    BlockStore(args.data).write_genesis(
        allocations, initial_stakes, args.timestamp, vesting,
        args.unbonding_blocks, treasury_addr,
    )

    print(f"genesis written to {args.data}")
    print(f"  market   : {MARKET_SUPPLY:,} {SYMBOL} (75%) -> {market_addr}")
    vest_note = f", vesting over {args.vest_duration} blocks (cliff {args.vest_cliff})" if vesting else ""
    print(f"  founder  : {FOUNDER_SUPPLY:,} {SYMBOL} (25%) -> {founder_addr}"
          f"  [{args.founder_stake:,} staked{vest_note}]")
    print(f"  treasury : slash receipts -> {treasury_addr}  (unbonding {args.unbonding_blocks} blocks)")
    for role, secret in generated.items():
        print(f"  !! generated {role} secret (save it, it is the only key): {secret}")


def _demo() -> None:
    import shutil
    import tempfile

    from consensus import ProofOfStakeChain
    from core import Transaction, ValidationError
    from storage import BlockStore

    founder = Wallet.from_secret(1)
    market = Wallet.from_secret(2)

    allocations, initial_stakes, vesting = build_genesis(
        market.address, founder.address, founder_stake_coins=100_000
    )
    assert vesting == {}  # no vesting requested
    chain = ProofOfStakeChain(allocations, initial_stakes, 1_700_000_000)

    # The split is exact and the cap is intact.
    assert chain.total_supply() == MAX_SUPPLY_UNITS
    assert chain.balance_of(market.address) == 750_000 * COIN
    assert chain.balance_of(founder.address) == 150_000 * COIN          # 250k - 100k staked
    assert chain.stake_of(founder.address) == 100_000 * COIN
    # Founder still owns exactly 250k (liquid + staked); staking did not change the split.
    assert chain.balance_of(founder.address) + chain.stake_of(founder.address) == FOUNDER_SUPPLY * COIN
    # 25% really is a quarter of the cap.
    assert FOUNDER_SUPPLY == (MARKET_SUPPLY + FOUNDER_SUPPLY) // 4

    # The founder is the genesis validator and can produce a block.
    assert chain.next_validator() == founder.address
    chain.add_block([], founder, 1_700_000_010)
    assert chain.head.index == 1
    assert chain.total_supply() == MAX_SUPPLY_UNITS

    # Guards: addresses must differ, and stake can't exceed the founder allocation.
    try:
        build_genesis(market.address, market.address)
        raise AssertionError("identical market/founder should be rejected")
    except ValueError:
        pass
    try:
        build_genesis(market.address, founder.address, founder_stake_coins=300_000)
        raise AssertionError("over-allocated founder stake should be rejected")
    except ValueError:
        pass

    # --- vesting end-to-end: founder cannot dump the premine on day one --------------- #
    alloc, stakes, vest = build_genesis(
        market.address, founder.address, founder_stake_coins=50_000,
        vest_cliff=2, vest_duration=4,
    )
    vchain = ProofOfStakeChain(alloc, stakes, 1_700_000_000, vest)
    assert vchain.locked_of(founder.address) == FOUNDER_SUPPLY * COIN   # fully locked at genesis
    assert vchain.spendable_of(founder.address) == 0                    # cannot move a coin yet

    # Founder tries to send 1 MIND at height 1 -> rejected by the vesting lock.
    try:
        vchain.add_block(
            [Transaction(founder.address, market.address, 1 * COIN, 0, 0).sign(founder)],
            founder, 1_700_000_001,
        )
        raise AssertionError("locked founder coins should not be spendable")
    except ValidationError:
        pass
    assert vchain.head.index == 0  # rejected, no block

    # Founder validates empty blocks (allowed while locked) to advance past the schedule.
    for h in range(1, 5):
        vchain.add_block([], founder, 1_700_000_000 + h)
    assert vchain.head.index == 4
    assert vchain.locked_of(founder.address) == 0  # fully vested at height 4

    # Now the founder CAN spend, and supply is still exactly the cap.
    vchain.add_block(
        [Transaction(founder.address, market.address, 1 * COIN, 0, 0).sign(founder)],
        founder, 1_700_000_010,
    )
    assert vchain.balance_of(market.address) == 750_001 * COIN
    assert vchain.total_supply() == MAX_SUPPLY_UNITS

    # The vesting rule survives a restart from disk (it is part of genesis).
    tmp = tempfile.mkdtemp(prefix="mindees_vest_")
    try:
        store = BlockStore(tmp)
        store.write_genesis(alloc, stakes, 1_700_000_000, vest)
        reloaded = store.load()
        assert reloaded.locked_of(founder.address) == FOUNDER_SUPPLY * COIN
        assert reloaded.spendable_of(founder.address) == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("ALL CHECKS PASSED")
    print(f"  distribution: {MARKET_SUPPLY:,} market (75%) + {FOUNDER_SUPPLY:,} founder (25%) "
          f"= {MAX_SUPPLY_UNITS // COIN:,} {SYMBOL}")
    print("  vesting: founder premine locked at genesis, releases on schedule, persists")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        _cli()
    else:
        _demo()
