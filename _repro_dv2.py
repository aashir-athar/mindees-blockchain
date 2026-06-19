from forkchoice import BlockTree
from consensus import seal_block, vote_tx
from core import COIN, MAX_SUPPLY_UNITS, Wallet

v1, v2 = Wallet.from_secret(1), Wallet.from_secret(2)
wallets = {v1.address: v1, v2.address: v2}
allocations = {v1.address: MAX_SUPPLY_UNITS - 2000*COIN, v2.address: 2000*COIN}
initial_stakes = {v1.address: 1000*COIN, v2.address: 1000*COIN}
ts = 1_700_000_000
epoch = 1

def new_tree():
    return BlockTree(allocations, initial_stakes, ts, epoch=epoch)

# Use one tree just to BUILD the blocks of each branch (proposer election only).
builder = new_tree()
g = builder.genesis_hash

def proposer_for(tree, parent_hash):
    return wallets[tree.chain_at(parent_hash).next_validator()]

def build(tree, parent_hash, height, txs, t):
    return seal_block(parent_hash, height, proposer_for(tree, parent_hash), txs, t)

# ---- Build branch A blocks ----
a1 = build(builder, g, 1, [], ts+1)
builder.add_block(a1)
votesA1 = [vote_tx(v1, g,0, a1.hash,1, 0), vote_tx(v2, g,0, a1.hash,1, 0)]
a2 = build(builder, a1.hash, 2, votesA1, ts+3)
builder.add_block(a2)
votesA2 = [vote_tx(v1, a1.hash,1, a2.hash,2, 1), vote_tx(v2, a1.hash,1, a2.hash,2, 1)]
a3 = build(builder, a2.hash, 3, votesA2, ts+5)
builder.add_block(a3)

# ---- Build branch B blocks on a SEPARATE builder (so genesis identical) ----
builderB = new_tree()
b1 = build(builderB, g, 1, [], ts+2)
builderB.add_block(b1)
votesB1 = [vote_tx(v1, g,0, b1.hash,1, 0), vote_tx(v2, g,0, b1.hash,1, 0)]
b2 = build(builderB, b1.hash, 2, votesB1, ts+4)
builderB.add_block(b2)
votesB2 = [vote_tx(v1, b1.hash,1, b2.hash,2, 1), vote_tx(v2, b1.hash,1, b2.hash,2, 1)]
b3 = build(builderB, b2.hash, 3, votesB2, ts+6)
builderB.add_block(b3)

print("a1", a1.hash[:12], "b1", b1.hash[:12], "distinct:", a1.hash != b1.hash)

# ---- NODE A: a partitioned node that only saw branch A ----
nodeA = new_tree()
for blk in (a1, a2, a3):
    nodeA.add_block(blk)
print("NodeA finalized:", nodeA.finalized_hash[:12], "h", nodeA.finalized_height)

# ---- NODE B: a partitioned node that only saw branch B ----
nodeB = new_tree()
for blk in (b1, b2, b3):
    nodeB.add_block(blk)
print("NodeB finalized:", nodeB.finalized_hash[:12], "h", nodeB.finalized_height)

print("PERMANENT SAFETY FAULT (two nodes finalized conflicting checkpoints @ same height):",
      nodeA.finalized_height == nodeB.finalized_height == 1 and
      nodeA.finalized_hash != nodeB.finalized_hash)

# ---- Partition heals: NodeA receives branch B blocks. Can it ever accept them? ----
print("\n-- partition heals: NodeA receives branch B --")
for blk, name in ((b1,'b1'), (b2,'b2'), (b3,'b3')):
    try:
        r = nodeA.add_block(blk)
        print(f"NodeA accepts {name}:", r)
    except Exception as e:
        print(f"NodeA rejects {name}:", type(e).__name__, str(e))
print("NodeA head still:", nodeA.head_hash[:12], "finalized:", nodeA.finalized_hash[:12])
