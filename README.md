# Mindees (MIND)

A from-scratch Proof-of-Stake Layer-1 cryptocurrency, built in pure Python with one
dependency (`cryptography`). Fixed supply of **exactly 1,000,000 MIND**, minted once at
genesis — zero inflation, ever.

## Is it "10x better than BTC"? Honestly:

| Axis | Verdict |
|------|---------|
| **Supply / inflation** | **Genuinely 10x.** Exactly 1,000,000, 0% inflation from block 0, cap true *by construction* (no mint path exists). |
| **Energy** | **Far lower.** Proof-of-Stake — a validator signs a block, it doesn't burn electricity. |
| **Throughput / fees** | Higher than BTC (~hundreds of tx/s locally vs 3–7), cheap fee market. |
| **Confirmation latency** | Seconds (PoS), not ~10-minute blocks. |
| **Security / Lindy / decentralization** | **Behind BTC, honestly.** A new chain has no track record; this is the axis BTC exists to win and we don't pretend otherwise. We are *closing the gap* (fork choice, slashing, finality), not claiming it's closed. |

The win is real where it's real (supply, energy, speed) and we concede the rest. No inflated multipliers.

## Quickstart

```bash
# 1. Create genesis: 750k market / 250k founder (25%), a treasury, and an unbonding period.
python tokenomics.py init --data ./chaindata --founder-stake 50000 --unbonding-blocks 200
#    -> prints market / founder / treasury addresses + their generated secrets (save them!)

# 2. Run a validator node (JSON-RPC API on http://127.0.0.1:8645/)
python node.py serve --data ./chaindata --validator-secret <FOUNDER_SECRET> --autoproduce

# 3. Talk to it with the wallet
python wallet.py info
python wallet.py balance <ADDRESS>
python wallet.py send <TO_ADDRESS> 100 <SENDER_SECRET> --mine

# Safer key handling: an encrypted keystore instead of a raw secret on the command line
export MINDEES_PASSPHRASE='correct horse battery staple'
python wallet.py keygen --out founder.json
python wallet.py send <TO_ADDRESS> 100 --keystore founder.json --mine
```

## Architecture

Each module is self-contained and self-testing — run it directly to execute its checks.

| Module | Responsibility |
|--------|----------------|
| [core.py](core.py) | Keys (secp256k1), signed account-model transactions, Merkle blocks, the hard-capped ledger |
| [consensus.py](consensus.py) | Proof-of-Stake election, staking, **slashing + unbonding** |
| [vesting.py](vesting.py) | Linear-with-cliff token vesting (locks the founder premine) |
| [mempool.py](mempool.py) | Fee market + DoS-bounded pending-tx pool |
| [network.py](network.py) | P2P gossip (relay/dedup), block propagation, sync, TCP ingest, wire codec |
| [forkchoice.py](forkchoice.py) | Block tree, heaviest-branch fork choice, deterministic reorg, **finality-respecting** |
| [finality.py](finality.py) | Casper-FFG finality gadget gates (justify → finalize, vote-fault slashing) |
| [storage.py](storage.py) | Tamper-evident, crash-safe, replayable block store |
| [node.py](node.py) | Runnable node daemon (fork choice + finality) + JSON-RPC API |
| [p2p.py](p2p.py) | TCP peer mesh: tx + block gossip across a real network |
| [wallet.py](wallet.py) | CLI wallet (local signing, encrypted keystore) |
| [keystore.py](keystore.py) | scrypt + AES-256-GCM key encryption at rest |
| [tokenomics.py](tokenomics.py) | The 750k/250k genesis distribution as tested policy |
| [fuzz.py](fuzz.py) | Property/invariant fuzzer + honest benchmark |

## Tokenomics

- **Total supply:** exactly 1,000,000 MIND (8 decimals, like BTC).
- **Distribution:** 750,000 (75%) to market/treasury, 250,000 (25%) to the founder.
- **Founder vesting:** the 25% can be vested (cliff + linear) so it isn't a liquid day-one premine.
- **Validators** stake to produce blocks and earn fees; there is no block reward (fee-only, fixed supply).

## Security model (current)

- **Fork choice:** heaviest-stake-weight branch; equivocation forks resolve deterministically, and
  the canonical head can never reorg at or below a finalized checkpoint.
- **Slashing:** proven equivocation (two blocks, same parent/height, by the same validator) confiscates
  100% of the offender's bonded stake, **redistributed** (5% reporter bounty, 95% treasury) — never
  burned, so supply stays exactly 1,000,000. Honest validators are unslashable (shared-parent rule).
- **Unbonding:** unstaking is delayed so an exiting validator's stake stays slashable in the window.
- **Finality:** Casper-FFG two-phase gadget. Votes are signed txs; a checkpoint justifies at ≥2/3 of
  the source's active stake and finalizes on its direct epoch child. **Accountable safety under <1/3
  Byzantine stake** — two conflicting checkpoints can only finalize if ≥1/3 of stake casts a slashable
  double/surround vote. Finalized state is a pure function of the chain, identical on every node and on
  disk reload. Double/surround vote-faults are slashed through the same engine.
- **Networking:** a TCP peer mesh gossips txs and blocks; every node independently validates and relays.

### Honest limitations
- **Liveness under ≥1/3 offline/censoring stake:** finality *stalls* (it never finalizes wrongly), and
  recovery is social/governance — no inactivity-leak in v1.
- **Weak subjectivity:** a finalized checkpoint is economically irreversible only while offending stake
  is bonded; new/long-offline nodes should sync from a recent finalized checkpoint.
- **Deferred:** RFC-6979 deterministic signatures, BLS vote aggregation, state pruning, and peer
  discovery/outbound-retry on the mesh.

This is a *correct* chain with accountable finality. It is not a battle-tested *secure* L1 — no audit,
no track record — and we don't claim it is. The honest wins are fixed supply, energy, and speed.

## Testing

```bash
# Every module self-tests:
for m in core consensus vesting mempool network forkchoice finality storage node p2p wallet keystore tokenomics; do python $m.py; done
# Property/invariant fuzzer (supply invariant across thousands of random + adversarial blocks):
python fuzz.py
# Static analysis gate:
python -m ruff check .
```

Everything is free and open-source: Python standard library plus the `cryptography` package.
