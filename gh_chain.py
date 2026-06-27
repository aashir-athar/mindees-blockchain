"""
GitHub-Actions chain driver (one-shot).

GitHub Actions runners are ephemeral, so they can't host a 24/7 node. Instead a scheduled
(cron) workflow calls this script to ADVANCE the chain a few blocks each cycle and commit the
new state back into the repo. The result is a free, always-advancing, publicly-visible
single-producer testnet -- NOT a decentralised, value-bearing mainnet (that needs real hosts
+ an audit). The validator key comes from the MINDEES_VALIDATOR_SECRET Actions secret.

  python gh_chain.py init --data chain-state     # once: build + persist genesis
  python gh_chain.py tick --data chain-state --blocks 6   # each cron cycle: produce + vote
  python gh_chain.py status --data chain-state    # rewrite status.json
"""
from __future__ import annotations

import argparse
import json
import os
import time

from consensus import vote_tx
from core import COIN, MAX_SUPPLY_UNITS, SYMBOL, ValidationError, Wallet
from node import NodeService
from storage import BlockStore

EPOCH = 4              # small epoch -> finality advances within a few cron cycles
UNBONDING = 50
STAKE_COINS = 100_000


def _validator() -> Wallet:
    secret = os.environ.get("MINDEES_VALIDATOR_SECRET")
    if not secret:
        raise SystemExit("MINDEES_VALIDATOR_SECRET is not set (add it as a repo Actions secret)")
    return Wallet.from_secret(int(secret, 16))


def _write_status(data: str) -> None:
    svc = NodeService(BlockStore(data))
    chain = svc.chain
    status = {
        "network": "mindees",
        "ticker": SYMBOL,
        "height": chain.head.index,
        "head": chain.head.hash,
        "finalized_height": svc.tree.finalized_height,
        "finalized_hash": svc.tree.finalized_hash,  # weak-subjectivity checkpoint
        "supply": chain.total_supply(),
        "max_supply": MAX_SUPPLY_UNITS,
        "supply_ok": chain.total_supply() == MAX_SUPPLY_UNITS,
        "validators": sorted(chain.stakes),
        "updated_unix": int(time.time()),
        "note": "Free GitHub-Actions-hosted single-producer testnet. Experimental, unaudited, "
                "valueless. Not a decentralised or value-bearing mainnet.",
    }
    with open(os.path.join(data, "status.json"), "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2)


def cmd_init(data: str) -> None:
    store = BlockStore(data)
    if os.path.exists(store.genesis_path):
        print("genesis already exists; nothing to do")
    else:
        v = _validator()
        store.write_genesis(
            allocations={v.address: MAX_SUPPLY_UNITS},
            initial_stakes={v.address: STAKE_COINS * COIN},
            timestamp=1_700_000_000,
            unbonding_blocks=UNBONDING,
            treasury_address=v.address,
            epoch=EPOCH,
        )
        print(f"genesis written: {MAX_SUPPLY_UNITS // COIN:,} {SYMBOL} -> validator {v.address}")
    _write_status(data)


def _auto_vote(svc: NodeService) -> None:
    """Queue a finality vote (latest justified -> newest checkpoint); fault-free by construction."""
    v = svc.validator
    chain = svc.chain
    if v is None or v.address not in chain.stakes:
        return
    epoch = chain.epoch
    target = next((b for b in reversed(chain.chain) if b.index > 0 and b.index % epoch == 0), None)
    if target is None or (v.address, target.index) in chain.ffg_seen_target:
        return
    src_hash, src_height = chain.finalized
    for b in chain.chain:
        if (b.index < target.index and b.index % epoch == 0
                and b.hash in chain.justified and b.index >= src_height):
            src_hash, src_height = b.hash, b.index
    if src_height >= target.index:
        return
    pending = sum(1 for t in svc.mempool.pool.values() if t.sender == v.address)
    nonce = chain.nonces.get(v.address, 0) + pending
    try:
        svc.mempool.add(vote_tx(v, src_hash, src_height, target.hash, target.index, nonce), chain)
    except ValidationError:
        pass


def cmd_tick(data: str, blocks: int) -> None:
    svc = NodeService(BlockStore(data), validator_wallet=_validator())
    base = int(time.time())
    produced = 0
    for i in range(blocks):
        _auto_vote(svc)                       # queue a vote so the next block carries it
        if svc._is_our_turn():
            svc.produce_block({"timestamp": base + i})
            produced += 1
    _write_status(data)
    print(f"advanced {produced} blocks -> height {svc.chain.head.index}, "
          f"finalized {svc.tree.finalized_height}, supply_ok="
          f"{svc.chain.total_supply() == MAX_SUPPLY_UNITS}")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="gh_chain", description="GitHub Actions chain driver")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("init", "tick", "status"):
        p = sub.add_parser(name)
        p.add_argument("--data", default="chain-state")
        if name == "tick":
            p.add_argument("--blocks", type=int, default=6)
    args = parser.parse_args(argv)

    if args.cmd == "init":
        cmd_init(args.data)
    elif args.cmd == "tick":
        cmd_tick(args.data, args.blocks)
    else:
        _write_status(args.data)


if __name__ == "__main__":
    main()
