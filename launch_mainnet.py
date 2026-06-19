"""
Mindees mainnet launcher.

Builds the canonical genesis (750,000 market / 250,000 founder, founder vesting, treasury,
unbonding), writes ENCRYPTED keystores for the genesis keys, and launches a live network — a
genesis validator that produces and finalizes blocks, plus follower nodes that sync — then emits
a publishable genesis bundle (genesis.json + its SHA-256 + the latest finalized checkpoint).

  MINDEES_PASSPHRASE='a strong passphrase' python launch_mainnet.py --seconds 12

This brings up a LIVE network locally — a real launch rehearsal that proves the genesis and
multi-node launch work end to end. For a PUBLIC, value-bearing mainnet you run these same nodes on
always-on hosts AFTER an independent security audit and a public testnet period (see MAINNET.md).
Mindees is experimental, unaudited software.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _gen_keystore(path, passphrase):
    """Create an encrypted keystore at `path`; return its address."""
    from keystore import encrypt_secret, save_keystore
    from wallet import new_secret
    from core import Wallet

    secret = new_secret()
    save_keystore(encrypt_secret(secret, passphrase), path)
    return Wallet.from_secret(int(secret, 16)).address


def _sha256_file(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="launch_mainnet", description="launch a live Mindees network")
    parser.add_argument("--data", default=os.path.join(HERE, "mainnet"))
    parser.add_argument("--keys", default=os.path.join(HERE, "mainnet-keys"))
    parser.add_argument("--followers", type=int, default=2)
    parser.add_argument("--base-port", type=int, default=9500)
    parser.add_argument("--slot", type=float, default=1.0)
    parser.add_argument("--epoch", type=int, default=4)
    parser.add_argument("--founder-stake", type=int, default=50_000)
    parser.add_argument("--vest-cliff", type=int, default=50)
    parser.add_argument("--vest-duration", type=int, default=500)
    parser.add_argument("--unbonding-blocks", type=int, default=100)
    parser.add_argument("--seconds", type=float, default=12.0)
    args = parser.parse_args(argv)

    passphrase = os.environ.get("MINDEES_PASSPHRASE")
    if not passphrase:
        raise SystemExit("set $MINDEES_PASSPHRASE to encrypt the genesis keystores")

    from storage import BlockStore
    from tokenomics import build_genesis

    os.makedirs(args.keys, exist_ok=True)
    founder_ks = os.path.join(args.keys, "founder.json")
    market = _gen_keystore(os.path.join(args.keys, "market.json"), passphrase)
    treasury = _gen_keystore(os.path.join(args.keys, "treasury.json"), passphrase)
    founder = _gen_keystore(founder_ks, passphrase)

    allocations, initial_stakes, vesting = build_genesis(
        market, founder, args.founder_stake, args.vest_cliff, args.vest_duration
    )

    # The validator (node0) plus follower nodes, each with the byte-identical canonical genesis.
    total = 1 + args.followers
    dirs = []
    for i in range(total):
        d = os.path.join(args.data, f"node{i}")
        os.makedirs(d, exist_ok=True)
        BlockStore(d).write_genesis(
            dict(allocations), dict(initial_stakes), 1_700_000_000, dict(vesting),
            args.unbonding_blocks, treasury, args.epoch,
        )
        dirs.append(d)
    genesis_path = os.path.join(dirs[0], "genesis.json")
    genesis_hash = _sha256_file(genesis_path)

    env = dict(os.environ)  # carries MINDEES_PASSPHRASE to the validator subprocess
    procs = []
    for i, d in enumerate(dirs):
        port = args.base_port + i
        cmd = [sys.executable, os.path.join(HERE, "p2p.py"), "serve",
               "--data", d, "--port", str(port), "--slot", str(args.slot),
               "--advertise", f"127.0.0.1:{port}"]
        if i == 0:
            cmd += ["--validator-keystore", founder_ks]
        for j in range(total):
            if j != i:
                cmd += ["--peer", f"127.0.0.1:{args.base_port + j}"]
        procs.append(subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

    print("=== Mindees network LAUNCHED ===")
    print(f"  validator   : {founder}  (stake {args.founder_stake:,} MIND, vesting)")
    print(f"  market (75%): {market}")
    print(f"  treasury    : {treasury}")
    print(f"  genesis sha256: {genesis_hash}")
    print(f"  nodes on 127.0.0.1:{args.base_port}..{args.base_port + total - 1}, epoch={args.epoch}")
    print(f"  running for {args.seconds:.0f}s ...")

    try:
        deadline = time.time() + args.seconds
        from node import NodeService
        while time.time() < deadline:
            time.sleep(2)
            svc = NodeService(BlockStore(dirs[0]))
            print(f"    height={svc.chain.head.index}  finalized={svc.tree.finalized_height}  "
                  f"supply_ok={svc.chain.total_supply() == _max_supply()}")
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()

    from node import NodeService
    final = NodeService(BlockStore(dirs[0]))
    bundle = {
        "network": "mindees",
        "genesis_sha256": genesis_hash,
        "epoch": args.epoch,
        "validator_address": founder,
        "treasury_address": treasury,
        "height": final.chain.head.index,
        "finalized_height": final.tree.finalized_height,
        "finalized_hash": final.tree.finalized_hash,   # weak-subjectivity checkpoint
        "seed_example": f"<PUBLIC_HOST>:{args.base_port}",
        "disclaimer": "Experimental, unaudited. Not financial advice. Testnet-first; audit before real value.",
    }
    bundle_path = os.path.join(args.data, "genesis_bundle.json")
    with open(bundle_path, "w", encoding="utf-8") as f:
        json.dump(bundle, f, indent=2)

    print("=== launch complete ===")
    print(f"  reached height {final.chain.head.index}, finalized through {final.tree.finalized_height}")
    print(f"  genesis bundle written to {bundle_path}")
    print("  keystores (KEEP SECRET, back up offline) are in:", args.keys)


def _max_supply():
    from core import MAX_SUPPLY_UNITS
    return MAX_SUPPLY_UNITS


if __name__ == "__main__":
    main()
