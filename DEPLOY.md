# Deploying a Mindees network

Three ways to run a network, smallest to largest. All are **testnet-grade** — see the
mainnet warning at the bottom.

## 1. Local testnet (one machine, one command)

```bash
pip install -r requirements.txt
python run_testnet.py                       # 1 validator + 2 followers, until Ctrl-C
python run_testnet.py --validators 3 --followers 0 --slot 2
python run_testnet.py --check               # launch briefly, assert convergence, exit
```

`run_testnet.py` writes one shared genesis, launches each node as its own `p2p.py serve`
process, fully meshes them, and the validators self-produce on the slot tick.

## 2. Docker Compose (containerised 3-node testnet)

```bash
docker compose up --build
```

`node1` is the validator; `node2`/`node3` follow and relay. They share a genesis derived
from `MINDEES_GENESIS_TO` and gossip over the Docker network. Data persists in named
volumes. **The compose file uses a trivial TEST key — never do that in production.**

## 3. Multi-host (the real thing)

On every host, the genesis must be byte-identical. The simplest way is to commit/distribute
one `genesis.json`, or run the same `node.py init` parameters everywhere:

```bash
# once, distribute the result to every host's data dir:
python node.py init --data ./chain --to <VALIDATOR_ADDRESS> --stake 100000

# validator host:
MINDEES_PASSPHRASE=... \
python p2p.py serve --data ./chain --port 9000 \
  --validator-secret <HEX> --peer seed1.example:9000 --peer seed2.example:9000 --slot 2

# follower host (no --validator-secret):
python p2p.py serve --data ./chain --port 9000 --peer validator.example:9000
```

Publish the latest **finalized block hash** with each release as a weak-subjectivity
checkpoint, so new or long-offline nodes can't be fooled by a long-range fork.

### Key handling
Never pass a raw validator secret on a production command line. Create an encrypted keystore
(`python wallet.py keygen --out validator.json`, unlocked via `$MINDEES_PASSPHRASE`) and feed
the secret from a real secrets manager.

## ⚠️ Mainnet / real value — not yet
This is a correct, finality-having chain, but it has **no security audit and no track record**.
Do not launch a network that holds real value until it has been independently audited and the
deferred hardening (RFC-6979 signatures, BLS aggregation, state pruning, inactivity-leak
recovery) is done. Run testnets with valueless coins until then.
