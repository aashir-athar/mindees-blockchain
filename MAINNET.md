# Mindees mainnet launch runbook

> ## ⚠️ Read this first
> Mindees is **experimental, unaudited software**. Launching a mainnet that holds **real value**
> before the steps below are done puts other people's money at risk. Technically, a "mainnet" is
> just a public network everyone agrees is canonical — the software can do it today. **Doing it
> responsibly is the hard part.** Nothing here is financial or legal advice.

## Do these BEFORE a value-bearing mainnet (non-negotiable)

1. **Run a public testnet for weeks.** Real hosts, real validators, real load, valueless coins.
   Watch for forks, finality stalls, crashes, and reorgs. Fix what breaks.
2. **Get an independent security audit.** A from-scratch consensus + crypto codebase must be
   reviewed by people who break chains for a living. Budget for it.
3. **Finish the deferred hardening:** RFC-6979 deterministic signatures, FFG state pruning,
   peer-discovery hardening/auth, and inactivity-leak recovery. (See README → Security model.)
4. **Check your legal/regulatory position.** Issuing a token can be a regulated activity depending
   on jurisdiction and how it is distributed. Get qualified legal advice. Do not solicit
   investment based on this README.

Until all four are done, **run a testnet, not a value-bearing mainnet.**

---

## The technical launch (once the prerequisites above are met)

### 1. Key ceremony (do this on an offline machine)
Generate encrypted keystores for the founder, treasury, and each genesis validator. Never put a
raw secret on a command line or in a repo.

```bash
export MINDEES_PASSPHRASE='a long, unique passphrase per key'
python wallet.py keygen --out founder.json     # 250,000 MIND (25%)
python wallet.py keygen --out treasury.json    # slash receipts sink
python wallet.py keygen --out validator1.json  # a genesis validator
# ... one keystore per validator; record each printed address
```

Back up every keystore + passphrase offline (hardware/paper). Losing a key loses those coins
forever — there is no recovery.

### 2. Freeze the canonical genesis
Pick parameters and generate **one** `genesis.json`. Every node on the network must use the
byte-identical file, so publish it and its hash.

```bash
python tokenomics.py init --data ./mainnet \
  --market   <TREASURY_OR_DISTRIBUTION_ADDRESS> \
  --founder  <FOUNDER_ADDRESS> \
  --founder-stake 50000 \
  --vest-cliff <BLOCKS> --vest-duration <BLOCKS> \
  --treasury <TREASURY_ADDRESS> \
  --unbonding-blocks <BLOCKS_>_DETECTION_LATENCY> \
  --timestamp <FIXED_GENESIS_UNIX_TIME>
sha256sum ./mainnet/genesis.json   # publish this hash
```

Choose `--unbonding-blocks` strictly greater than worst-case equivocation/vote-fault detection +
inclusion latency, or an exiting validator can dodge slashing.

### 3. Bring up persistent seed nodes
On always-on hosts (cloud VMs, dedicated servers). Distribute the **same** `genesis.json` to each.
Use a real reachable address with `--advertise`, and unlock the validator key from a secrets
manager — not a literal secret.

```bash
# validator/seed node
MINDEES_PASSPHRASE=... python p2p.py serve \
  --data ./mainnet --port 9000 --advertise <PUBLIC_HOST>:9000 \
  --validator-secret <FROM_SECRETS_MANAGER> \
  --peer seed1.example:9000 --peer seed2.example:9000 --slot <SECONDS>
```

Run several geographically separate seed nodes so the network can bootstrap and survive a host
loss. (Note: `--validator-secret` currently takes a hex secret; wire it from your secrets manager,
and prefer adding keystore-unlock to `p2p.py serve` before mainnet.)

### 4. Publish the genesis bundle
So anyone can join and verify:
- `genesis.json` + its SHA-256 hash,
- the list of seed node `host:port` addresses,
- the **latest finalized block hash** as a *weak-subjectivity checkpoint* (refresh it with each
  release) so new or long-offline nodes can't be fooled by a long-range fork.

### 5. Operate
- Monitor height, finalized height, and peer count on every node.
- Keep validator keys hot only on the validator host; everything else cold.
- Watch for slashing events; a slashed validator's stake is redistributed (5% reporter / 95%
  treasury), never burned, so total supply stays exactly 1,000,000.

---

## What this runbook deliberately does NOT do
It does not market MIND as an investment, promise any value, or list it for sale. Mindees is a
fixed-supply coin with honest, documented limitations. Whether it ever holds value is a market and
regulatory matter — and an ethical one: don't sell people something unaudited and call it safe.
