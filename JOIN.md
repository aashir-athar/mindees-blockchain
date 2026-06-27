# Join the Mindees network

Anyone can run a Mindees node, verify the chain independently, and help decentralize it.
Mindees is **experimental, unaudited** software and MIND has **no guaranteed value** — run it to
learn, test, and verify, not to store wealth.

## 1. Get the code and the canonical genesis

```bash
git clone https://github.com/aashir-athar/mindees-blockchain.git
cd mindees-blockchain
pip install -r requirements.txt
```

The canonical genesis and the chain so far live in [`chain-state/`](chain-state/)
(`genesis.json` + `blocks.jsonl`). Verify the genesis hash matches what the network publishes:

```bash
python -c "import hashlib;print(hashlib.sha256(open('chain-state/genesis.json','rb').read()).hexdigest())"
# compare against chain-state/status.json -> finalized_hash lineage / a release-published hash
```

## 2. Run a node (follower)

A follower validates and relays everything but does not produce blocks. Run it on your own copy
of the chain so you never share a data directory with another process:

```bash
cp -r chain-state mynode            # your own working copy of genesis + blocks
python p2p.py serve --data mynode --port 9000 --peer <PEER_HOST>:9000
```

Your node replays and re-validates every block from genesis (so it trusts *no one*), then gossips
new blocks with the peers you list. Point `--peer` at other operators' public `host:port`.

## 3. Verify the chain yourself

```bash
python -c "import sys;sys.path.insert(0,'.');from storage import BlockStore;from node import NodeService;\
s=NodeService(BlockStore('mynode'));print('height',s.chain.head.index,'finalized',s.tree.finalized_height,\
'supply_ok',s.chain.total_supply()==10**14)"
python fuzz.py            # property/invariant fuzzer
python -m ruff check .    # static analysis
```

Every node independently enforces the rules: the 1,000,000 fixed supply, Casper-FFG finality,
slashing, and the finalized-prefix that can never be reorged.

## 4. Becoming a validator

Producing blocks requires **stake**. The genesis validator set is fixed at launch; new validators
join only when stake is delegated/distributed to them — a governance decision by the network's
stakeholders, not something a node can grant itself. If you receive stake:

```bash
python wallet.py keygen --out validator.json          # encrypted key ($MINDEES_PASSPHRASE)
export MINDEES_PASSPHRASE='...'
python p2p.py serve --data mynode --port 9000 \
  --validator-keystore validator.json --advertise <YOUR_PUBLIC_HOST>:9000 --peer <PEER>:9000
```

## Honest status
The mainnet is currently in **bootstrap phase**: a single GitHub-Actions producer, unaudited.
Decentralization (independent validators like you) and an **independent security audit** are what
turn a live chain into one anyone should trust with value. See [MAINNET.md](MAINNET.md).
