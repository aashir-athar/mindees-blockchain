# Mindees (MIND) — Open-Source Proof-of-Stake Blockchain & Cryptocurrency in Python

[![CI](https://github.com/aashir-athar/mindees-blockchain/actions/workflows/ci.yml/badge.svg)](https://github.com/aashir-athar/mindees-blockchain/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

**Live mainnet (bootstrap):**
[![height](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Faashir-athar%2Fmindees-blockchain%2Fmain%2Fchain-state%2Fstatus.json&query=%24.height&label=height&color=brightgreen)](chain-state/status.json)
[![finalized](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Faashir-athar%2Fmindees-blockchain%2Fmain%2Fchain-state%2Fstatus.json&query=%24.finalized_height&label=finalized&color=blue)](chain-state/status.json)
[![supply](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Faashir-athar%2Fmindees-blockchain%2Fmain%2Fchain-state%2Fstatus.json&query=%24.supply&label=supply&color=informational)](chain-state/status.json)
[![supply exact](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Faashir-athar%2Fmindees-blockchain%2Fmain%2Fchain-state%2Fstatus.json&query=%24.supply_ok&label=supply%20exact&color=success)](chain-state/status.json)

**Mindees** is a from-scratch **Proof-of-Stake (PoS) Layer-1 blockchain** and **cryptocurrency**,
written in pure **Python** with a single dependency. It has a **fixed supply of exactly
1,000,000 MIND** — minted once at genesis, with **zero inflation, ever**. Mindees implements the
full stack of a modern crypto network: stake-weighted block production, a fee-market mempool,
**peer-to-peer (P2P) networking** with discovery and sync, **Casper-FFG accountable finality**,
**slashing**, **staking**, **vesting**, an encrypted **wallet/keystore**, and a JSON-RPC node.

> **Keywords:** blockchain · cryptocurrency · proof of stake · PoS · Layer 1 · L1 · Python blockchain ·
> build a blockchain from scratch · fixed supply coin · Bitcoin alternative · crypto · staking ·
> validator · finality · Casper FFG · slashing · fork choice · P2P · decentralized · digital currency ·
> open-source crypto · blockchain node · crypto wallet · tokenomics · web3 · distributed systems.

---

## Table of contents
- [What is Mindees?](#what-is-mindees)
- [Is it "10x better than Bitcoin"? An honest comparison](#is-it-10x-better-than-bitcoin-an-honest-comparison)
- [Features](#features)
- [Quickstart](#quickstart)
- [Run a network](#run-a-network)
- [Architecture](#architecture)
- [Tokenomics](#tokenomics)
- [Security model](#security-model)
- [FAQ](#faq)
- [Disclaimer](#disclaimer)

## What is Mindees?

Mindees (ticker **MIND**) is an educational-yet-complete **Proof-of-Stake cryptocurrency**: a
working example of **how to build a blockchain from scratch in Python**. Unlike Bitcoin's
energy-hungry Proof-of-Work, Mindees uses **Proof-of-Stake** — validators stake coins to produce
blocks and earn fees — so it is fast and energy-light. The **supply is hard-capped at 1,000,000
coins** by the code itself: there is no mint function, no block reward, and no inflation path.

## Is it "10x better than Bitcoin"? An honest comparison

| Axis | Mindees (MIND) vs Bitcoin (BTC) |
|---|---|
| **Supply / inflation** | **Genuinely better:** exactly 1,000,000 coins, 0% inflation from block 0, cap true by construction. |
| **Energy** | **Far lower:** Proof-of-Stake — signing a block, not burning electricity. |
| **Throughput & fees** | Higher throughput, cheap fee market (hundreds of tx/s locally vs ~3–7 for BTC). |
| **Finality** | Seconds-to-final via Casper-FFG, vs Bitcoin's ~1-hour probabilistic settlement. |
| **Security / track record** | **Behind, honestly:** new chain, no audit, no Lindy effect. Bitcoin wins this axis. |

The real, defensible wins are **fixed supply, energy efficiency, and speed**. We do **not** claim
to beat Bitcoin on security or decentralization maturity — that takes years and audits.

## Features

- ✅ **Fixed supply** — exactly 1,000,000 MIND, enforced as immutable accounting (no inflation).
- ✅ **Proof-of-Stake consensus** — deterministic, stake-weighted validator election.
- ✅ **Casper-FFG finality** — accountable, irreversible finality under <1/3 Byzantine stake.
- ✅ **Slashing & unbonding** — equivocation and double/surround vote-faults are punished.
- ✅ **Fork choice** — heaviest-branch selection that never reorgs a finalized checkpoint.
- ✅ **P2P networking** — TCP gossip, peer discovery, and late-joiner block sync.
- ✅ **Fee-market mempool** — DoS-bounded, highest-fee-first selection.
- ✅ **Vesting** — lock the founder allocation (cliff + linear release).
- ✅ **Wallet & encrypted keystore** — local signing, scrypt + AES-256-GCM key storage.
- ✅ **JSON-RPC node** + **CLI wallet** + **one-command local testnet** + **Docker** deploy.
- ✅ **Free & open-source (MIT)** — Python standard library plus `cryptography`.

## Quickstart

```bash
pip install -r requirements.txt

# 1. Create a genesis (750k market / 250k founder, treasury, unbonding period)
python tokenomics.py init --data ./chaindata --founder-stake 50000 --unbonding-blocks 200

# 2. Run a validator node (JSON-RPC API on http://127.0.0.1:8645/)
python node.py serve --data ./chaindata --validator-secret <FOUNDER_SECRET> --autoproduce

# 3. Use the CLI wallet
python wallet.py info
python wallet.py balance <ADDRESS>
python wallet.py send <TO_ADDRESS> 100 <SENDER_SECRET> --mine

# Safer keys: an encrypted keystore instead of a raw secret on the command line
export MINDEES_PASSPHRASE='correct horse battery staple'
python wallet.py keygen --out founder.json
python wallet.py send <TO_ADDRESS> 100 --keystore founder.json --mine
```

## Run a network

```bash
python run_testnet.py            # local: 1 validator + 2 followers, one command
python run_testnet.py --check    # launch briefly, assert the nodes converge, exit
docker compose up --build        # containerised 3-node testnet
```

See [DEPLOY.md](DEPLOY.md) for multi-host deployment and key handling, and
[MAINNET.md](MAINNET.md) for the mainnet launch runbook (and its prerequisites).

## Live mainnet (bootstrap phase) on GitHub

The canonical Mindees network runs **live, for free** on a scheduled GitHub Actions workflow:
every 15 minutes a runner produces blocks, casts finality votes, and commits the new chain state
to [`chain-state/`](chain-state/) — see [`chain-state/status.json`](chain-state/status.json) for
the live height, finalized checkpoint, and supply. Workflows:
[`launch.yml`](.github/workflows/launch.yml) (one-time genesis) and
[`chain.yml`](.github/workflows/chain.yml) (the cron). The validator key is a repo Actions secret.

**What "bootstrap phase" honestly means:** the mainnet is live, but right now it is produced by a
**single GitHub-Actions validator** (centralized) and is **unaudited**, so **MIND has no
guaranteed value** and must not be treated as one. Reaching a mainnet people can safely transact
value on requires two things this repo cannot provide by itself: **independent 24/7 validators**
(decentralization) and an **independent security audit**. The turnkey path for real operators is
[`launch_mainnet.py`](launch_mainnet.py) + [MAINNET.md](MAINNET.md).

**Run a node / help decentralize it:** anyone can sync, verify, and gossip — see
[JOIN.md](JOIN.md).

## Architecture

Pure Python, one dependency (`cryptography`). Each module is self-contained and self-testing —
run it directly to execute its checks.

| Module | Responsibility |
|--------|----------------|
| [core.py](core.py) | Keys (secp256k1), signed account-model transactions, Merkle blocks, the hard-capped ledger |
| [consensus.py](consensus.py) | Proof-of-Stake election, staking, slashing + unbonding, finality votes |
| [vesting.py](vesting.py) | Linear-with-cliff token vesting (locks the founder allocation) |
| [mempool.py](mempool.py) | Fee market + DoS-bounded pending-transaction pool |
| [network.py](network.py) | Gossip codec, relay/dedup, block propagation, sync, TCP ingest |
| [forkchoice.py](forkchoice.py) | Block tree, heaviest-branch fork choice, finality-respecting reorg |
| [finality.py](finality.py) | Casper-FFG finality gadget gates (justify → finalize, vote-fault slashing) |
| [storage.py](storage.py) | Tamper-evident, crash-safe, replayable block store |
| [node.py](node.py) | Runnable node daemon (fork choice + finality) + JSON-RPC API |
| [p2p.py](p2p.py) | TCP peer mesh: gossip, peer discovery, late-joiner block sync, slot production |
| [wallet.py](wallet.py) | CLI cryptocurrency wallet (local signing, encrypted keystore) |
| [keystore.py](keystore.py) | scrypt + AES-256-GCM key encryption at rest |
| [tokenomics.py](tokenomics.py) | The 750k/250k genesis distribution as tested policy |
| [fuzz.py](fuzz.py) | Property/invariant fuzzer + honest benchmark |

## Tokenomics

- **Total supply:** exactly **1,000,000 MIND** (8 decimals, like Bitcoin).
- **Distribution:** 750,000 (75%) to market/treasury, 250,000 (25%) to the founder.
- **Founder vesting:** the 25% can vest (cliff + linear) so it is not a liquid day-one premine.
- **No block reward:** validators earn transaction fees only — fixed supply, zero inflation.

## Security model

- **Fork choice:** heaviest-stake-weight branch; never reorgs at or below a finalized checkpoint.
- **Slashing:** proven equivocation confiscates 100% of bonded stake, **redistributed** (5% reporter
  bounty, 95% treasury) — never burned, so supply stays exactly 1,000,000. Honest validators are
  unslashable (shared-parent rule).
- **Finality:** Casper-FFG two-phase gadget; **accountable safety under <1/3 Byzantine stake**.
- **Networking:** every node independently validates, relays, discovers peers, and syncs.

**Honest limitations:** no security audit and no track record; liveness stalls (never finalizes
wrongly) under ≥1/3 offline stake; deferred hardening includes RFC-6979 signatures, BLS aggregation,
and state pruning. This is a **correct** chain with accountable finality — **not** a battle-tested
secure Layer-1.

## FAQ

**How do I build a blockchain from scratch in Python?**
Read the modules in order — `core` (ledger) → `consensus` (PoS) → `mempool` → `network`/`p2p` →
`forkchoice` → `finality`. Each is small and self-testing.

**Is Mindees a Bitcoin alternative?**
It's a Proof-of-Stake cryptocurrency with a fixed 1,000,000 supply. It beats Bitcoin on supply
policy, energy, and speed, and concedes security maturity. It is experimental software.

**What makes the 1,000,000 supply trustworthy?**
There is no mint function and no block reward anywhere in the code; the cap is enforced as an
invariant checked after every block, and a property fuzzer hammers it across thousands of
random + adversarial scenarios.

**Is it Proof-of-Work or Proof-of-Stake?**
Proof-of-Stake. Validators stake MIND to produce blocks and earn fees — no mining, low energy.

**Can I run a node / validator?**
Yes — `python p2p.py serve …`. See [DEPLOY.md](DEPLOY.md).

**Is there a mainnet?**
See [MAINNET.md](MAINNET.md). The software can launch a network today; running one that holds real
value responsibly requires a public testnet period and an independent security audit first.

## Disclaimer

Mindees is **experimental, unaudited open-source software**, provided "as is" under the MIT license
with **no warranty**. Nothing here is financial, investment, or legal advice. Cryptocurrencies are
high-risk; you can lose everything. Do not put real value on this chain until it has been
independently audited. You are solely responsible for compliance with the laws and regulations of
your jurisdiction.

---

*Mindees: an open-source Python Proof-of-Stake Layer-1 blockchain and fixed-supply cryptocurrency
with Casper-FFG finality, staking, slashing, and P2P networking.*
