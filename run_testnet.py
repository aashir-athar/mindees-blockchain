"""
Mindees local testnet launcher.

Spins up a multi-node Mindees network as local subprocesses that share one genesis: a few
staked validators self-produce on a slot tick, follower nodes sync. Use it to watch a live
network on one machine, or as the template for a real multi-host deploy (one `p2p.py serve`
per host, same genesis, --peer pointing at the others).

  python run_testnet.py                          # 1 validator + 2 followers, until Ctrl-C
  python run_testnet.py --validators 3 --followers 0 --slot 2
  python run_testnet.py --check                  # launch briefly, assert convergence, exit 0/1
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def build_genesis(validators, followers, data_root, stake_coins=100_000, epoch=8, unbonding=200):
    """Write a byte-identical genesis into each node's data dir (so they all agree)."""
    from core import COIN, MAX_SUPPLY_UNITS, Wallet
    from storage import BlockStore

    treasury = Wallet.from_secret(10_000)
    stake = stake_coins * COIN
    allocations = {treasury.address: MAX_SUPPLY_UNITS - len(validators) * stake}
    for v in validators:
        allocations[v.address] = stake
    stakes = {v.address: stake for v in validators}

    dirs = []
    for i in range(len(validators) + followers):
        d = os.path.join(data_root, f"node{i}")
        os.makedirs(d, exist_ok=True)
        BlockStore(d).write_genesis(
            dict(allocations), dict(stakes), 1_700_000_000, None, unbonding, treasury.address, epoch
        )
        dirs.append(d)
    return dirs


def launch(n_validators, dirs, base_port, slot):
    """Start one `p2p.py serve` subprocess per node, fully meshed."""
    total = len(dirs)
    procs = []
    for i, d in enumerate(dirs):
        cmd = [sys.executable, os.path.join(HERE, "p2p.py"), "serve",
               "--data", d, "--port", str(base_port + i), "--slot", str(slot)]
        if i < n_validators:
            cmd += ["--validator-secret", format(i + 1, "x")]  # validators use secrets 1..N
        for j in range(total):
            if j != i:
                cmd += ["--peer", f"127.0.0.1:{base_port + j}"]
        procs.append(subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    return procs


def _stop(procs):
    for p in procs:
        p.terminate()
    for p in procs:
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()


def _check(dirs, procs, seconds):
    from core import MAX_SUPPLY_UNITS
    from node import NodeService
    from storage import BlockStore

    time.sleep(seconds)        # let validators produce and gossip
    _stop(procs)               # freeze the network, then inspect each node's persisted chain
    time.sleep(0.3)

    chains = [NodeService(BlockStore(d)).chain for d in dirs]
    heights = [c.head.index for c in chains]
    assert min(heights) > 0, f"no blocks were produced/propagated: heights={heights}"
    for c in chains:
        assert c.total_supply() == MAX_SUPPLY_UNITS, "supply drifted on a node"
    # Consensus = agreement on the common prefix; tips may differ by in-flight blocks.
    common = min(heights)
    prefix_hashes = {c.chain[common].hash for c in chains}
    assert len(prefix_hashes) == 1, f"nodes disagree at height {common}: {prefix_hashes}"

    print("ALL CHECKS PASSED")
    print(f"  testnet: {len(dirs)} live subprocess nodes converged on a common chain")
    print(f"  heights={heights}, agreed prefix at height {common}, supply intact")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="run_testnet", description="launch a local Mindees testnet")
    parser.add_argument("--validators", type=int, default=1)
    parser.add_argument("--followers", type=int, default=2)
    parser.add_argument("--base-port", type=int, default=9300)
    parser.add_argument("--slot", type=float, default=1.0)
    parser.add_argument("--check", action="store_true", help="run briefly, assert convergence, exit")
    parser.add_argument("--seconds", type=float, default=5.0, help="run time for --check")
    args = parser.parse_args(argv)

    from core import Wallet

    validators = [Wallet.from_secret(i + 1) for i in range(args.validators)]
    root = tempfile.mkdtemp(prefix="mindees_testnet_") if args.check \
        else os.path.join(HERE, "testnet_data")
    dirs = build_genesis(validators, args.followers, root)
    procs = launch(args.validators, dirs, args.base_port, args.slot)
    try:
        if args.check:
            _check(dirs, procs, args.seconds)
        else:
            ports = f"{args.base_port}..{args.base_port + len(dirs) - 1}"
            print(f"Mindees testnet up: {len(dirs)} nodes on 127.0.0.1:{ports} "
                  f"({args.validators} validator(s)). Data in {root}. Ctrl-C to stop.")
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopping testnet")
    finally:
        _stop(procs)
        if args.check:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
