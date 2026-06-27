# Mindees -- Go Live (always-on, worldwide)

> **Reality check (from the repo's own docs).** Mindees is experimental, unaudited software
> (MAINNET.md:3, DEPLOY.md:54-58). This runbook makes it *always-on and reachable worldwide*. It
> does **not** make it safe to hold real value -- do the four prerequisites in `MAINNET.md`
> (public testnet for weeks, independent audit, finish deferred hardening, legal review) before any
> value-bearing launch. Run this as a **public testnet** first.

This replaces the centralized GitHub-Actions producer (`chain.yml` cron, every 15 min, single key)
with **3 always-on validators + 1 public RPC node** behind TLS.

## 0. What you need
- 4 always-on Linux hosts (Ubuntu/Debian). 3 validators + 1 public RPC. (Free option: Oracle Cloud
  Always-Free ARM -- see README/cost notes.)
- DNS: `seed1`, `seed2`, `seed3.mindees.example` -> the 3 validators; `rpc.mindees.example` -> the
  RPC host.
- An offline machine for the key ceremony.

## 1. Key ceremony (offline machine, ONCE)
```bash
export MINDEES_PASSPHRASE='a long, unique passphrase'   # use a different one per key ideally
python wallet.py keygen --out validator1.json   # genesis validator + founder
python wallet.py keygen --out validator2.json
python wallet.py keygen --out validator3.json
python wallet.py keygen --out market.json       # holds the distributable supply
python wallet.py keygen --out treasury.json     # slash-receipt sink
```
Record every printed address. **Back up all keystores + passphrases offline.** A lost key is
unrecoverable.

## 2. Freeze the canonical genesis (offline, ONCE)
Every node must use the **byte-identical** `genesis.json`.
```bash
python tokenomics.py init --data ./mainnet \
  --market   <MARKET_ADDR> \
  --founder  <VALIDATOR1_ADDR> \
  --founder-stake 50000 \
  --vest-cliff 50 --vest-duration 500 \
  --treasury <TREASURY_ADDR> \
  --unbonding-blocks 200 \
  --timestamp 1700000000
sha256sum ./mainnet/genesis.json     # <-- PUBLISH this hash
```
`--unbonding-blocks` must exceed worst-case fault-detection latency (MAINNET.md:58).

## 3. Publish genesis + hash
Commit `genesis.json`, its SHA-256, and (after first finality) the latest finalized hash into the
repo and into `deploy/seeds.txt`. Anyone joining verifies their copy against the hash.

## 4. Provision each host (idempotent; safe to re-run)
Validator 1 (repeat for 2/3 with their own advertise host):
```bash
sudo MINDEES_ROLE=validator \
     MINDEES_REPO_URL=https://github.com/aashir-athar/mindees-blockchain.git \
     MINDEES_ADVERTISE=seed1.mindees.example:9000 \
     MINDEES_PEERS='seed2.mindees.example:9000 seed3.mindees.example:9000' \
     bash deploy/setup.sh
```
Public RPC host:
```bash
sudo MINDEES_ROLE=rpc \
     MINDEES_REPO_URL=https://github.com/aashir-athar/mindees-blockchain.git \
     MINDEES_ADVERTISE=rpc.mindees.example:9000 \
     MINDEES_PEERS='seed1.mindees.example:9000 seed2.mindees.example:9000 seed3.mindees.example:9000' \
     MINDEES_RPC_DOMAIN=rpc.mindees.example \
     bash deploy/setup.sh
```
> The script can also be pasted as **cloud-init user-data** (prefix with `#!/usr/bin/env bash` and
> the same env exports).

## 5. Drop secrets + genesis + keystore on each host
```bash
sudoedit /etc/mindees/mindees.env          # set MINDEES_PASSPHRASE; on rpc host also MINDEES_RPC_TOKEN
# generate the token once:  openssl rand -hex 32
scp ./mainnet/genesis.json host:/tmp/ && sudo install -o mindees -g mindees -m640 /tmp/genesis.json /var/lib/mindees/data/genesis.json
sha256sum /var/lib/mindees/data/genesis.json   # MUST equal the published hash
# validators only:
scp validatorN.json host:/tmp/ && sudo install -o mindees -g mindees -m600 /tmp/validatorN.json /var/lib/mindees/keys/validator.json
```
Also add the peer flags to the env file (setup.sh references `${MINDEES_PEER_FLAGS}` /
`${MINDEES_ADVERTISE}` if you use the standalone unit):
```bash
echo 'MINDEES_ADVERTISE=seed1.mindees.example:9000' | sudo tee -a /etc/mindees/mindees.env
echo 'MINDEES_PEER_FLAGS=--peer seed2.mindees.example:9000 --peer seed3.mindees.example:9000' | sudo tee -a /etc/mindees/mindees.env
```

## 6. Start the validators
```bash
sudo systemctl enable --now mindees-node@validator
journalctl -u mindees-node@validator -f      # expect: 'validator=yes height=...'
sudo ufw allow 9000/tcp                        # p2p gossip inbound
```

## 7. Start the public RPC node + TLS
```bash
sudo systemctl enable --now mindees-node@rpc   # its own gossip (no validator key)
sudo systemctl enable --now mindees-rpc        # node.py JSON-RPC on 127.0.0.1:8645
sudo systemctl reload caddy                     # auto-TLS for rpc.mindees.example
sudo ufw allow 443/tcp && sudo ufw allow 9000/tcp
# DO NOT open 8645 to the world -- it stays loopback-only.
```
**Why this is safe to expose:** the node binds `127.0.0.1` but we still set `MINDEES_RPC_TOKEN`,
because the loopback guard is evaluated from `--host` at startup and the handler ignores proxy
headers/peer IP (node.py:278, 153-171). Caddy injects the `Bearer` token on proxied requests, so
public users can read freely AND `submit_tx` works -- but nobody on the public side ever sees the
token.

Test:
```bash
curl -s -X POST https://rpc.mindees.example/ -d '{"method":"info","id":1}'
# {"id":1,"result":{...,"height":N,...}}
```

## 8. Distribute stake (decentralize block production)
From the market holder, fund the other two validators, then each stakes:
```bash
export MINDEES_PASSPHRASE='...'
python wallet.py send <VALIDATOR2_ADDR> 50000 --keystore market.json --rpc https://rpc.mindees.example/
python wallet.py send <VALIDATOR3_ADDR> 50000 --keystore market.json --rpc https://rpc.mindees.example/
python wallet.py stake 50000 --keystore validator2.json --rpc https://rpc.mindees.example/
python wallet.py stake 50000 --keystore validator3.json --rpc https://rpc.mindees.example/
python wallet.py balance <VALIDATOR2_ADDR> --rpc https://rpc.mindees.example/   # staked > 0
```
Now `info.next_validator` rotates across all three.

## 9. Publish seeds + bundle
Fill in `deploy/seeds.txt` with the real `seedN` hosts, the RPC URL, the genesis SHA-256, and the
current finalized hash (weak-subjectivity checkpoint, refresh each release). Commit it.

## 10. Operate
- Uptime check: `POST {"method":"info"}` to the RPC URL every minute; alert if height stops rising.
- `journalctl -u mindees-node@validator -f` per host.
- Back up `/var/lib/mindees/data` (genesis.json + blocks.jsonl) regularly.
- Restart cost grows with chain length (full replay from genesis, node.py:49-51 / storage:17-19);
  fine for now, revisit with snapshots at millions of blocks.
- **Retire `chain.yml`** (disable the cron) so the mesh is the sole producer.

## Limits this does NOT fix (documented, deferred)
- p2p gossip is plaintext + unauthenticated (p2p.py:17-18) -- consensus still validates everything,
  but the transport trusts no credentials. No TLS on gossip.
- No per-IP rate limiting on RPC or p2p (only memory/body caps). Add Caddy rate-limit / fail2ban if
  abused.
- Crypto/consensus hardening (RFC-6979, BLS, state pruning, inactivity-leak) still open
  (DEPLOY.md:57). Do not hold real value until audited.
