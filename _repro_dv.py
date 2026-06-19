from forkchoice import BlockTree
from consensus import seal_block, vote_tx
from core import COIN, MAX_SUPPLY_UNITS, Wallet

v1, v2 = Wallet.from_secret(1), Wallet.from_secret(2)
wallets = {v1.address: v1, v2.address: v2}
allocations = {v1.address: MAX_SUPPLY_UNITS - 2000*COIN, v2.address: 2000*COIN}
initial_stakes = {v1.address: 1000*COIN, v2.address: 1000*COIN}
ts = 1_700_000_000
epoch = 1

tree = BlockTree(allocations, initial_stakes, ts, epoch=epoch)
g = tree.genesis_hash

def proposer_for(parent_hash):
    ch = tree.chain_at(parent_hash)
    return wallets[ch.next_validator()]

def build_block(parent_hash, height, txs, t):
    p = proposer_for(parent_hash)
    return seal_block(parent_hash, height, p, txs, t)

a1 = build_block(g, 1, [], ts+1)
tree.add_block(a1)
b1 = build_block(g, 1, [], ts+2)
tree.add_block(b1)
print("a1", a1.hash[:12], "b1", b1.hash[:12])

votes_A = [vote_tx(v1, g, 0, a1.hash, 1, nonce=0),
           vote_tx(v2, g, 0, a1.hash, 1, nonce=0)]
votes_B = [vote_tx(v1, g, 0, b1.hash, 1, nonce=0),
           vote_tx(v2, g, 0, b1.hash, 1, nonce=0)]

# Block A2/B2 carry the first votes (g->checkpoint@1), justifying a1/b1.
a2 = build_block(a1.hash, 2, votes_A, ts+3)
tree.add_block(a2)
b2 = build_block(b1.hash, 2, votes_B, ts+4)
try:
    tree.add_block(b2)
    print("b2 accepted")
except Exception as e:
    print("b2 rejected:", type(e).__name__, e)

# Now a1 (height1) is justified. Vote a1(h1) -> a2(h2) to finalize a1 (direct child).
votes_A2 = [vote_tx(v1, a1.hash, 1, a2.hash, 2, nonce=1),
            vote_tx(v2, a1.hash, 1, a2.hash, 2, nonce=1)]
votes_B2 = [vote_tx(v1, b1.hash, 1, b2.hash, 2, nonce=1),
            vote_tx(v2, b1.hash, 1, b2.hash, 2, nonce=1)]
a3 = build_block(a2.hash, 3, votes_A2, ts+5)
tree.add_block(a3)
b3 = build_block(b2.hash, 3, votes_B2, ts+6)
try:
    tree.add_block(b3)
    print("b3 accepted")
except Exception as e:
    print("b3 rejected:", type(e).__name__, e)

ca = tree.chain_at(a3.hash)
print("branch A finalized:", ca.finalized_checkpoint()[0][:12], ca.finalized_checkpoint()[1],
      "justified a1?", ca.is_justified(a1.hash))
try:
    cb = tree.chain_at(b3.hash)
    print("branch B finalized:", cb.finalized_checkpoint()[0][:12], cb.finalized_checkpoint()[1],
          "justified b1?", cb.is_justified(b1.hash))
except Exception as e:
    print("branch B replay failed:", type(e).__name__, e)

print("tree finalized_height:", tree.finalized_height, "hash", tree.finalized_hash[:12])
fa = ca.finalized_checkpoint()
fb = cb.finalized_checkpoint()
print("A finalized hash:", fa[0][:12], "height", fa[1])
print("B finalized hash:", fb[0][:12], "height", fb[1])
print("CONFLICTING FINALIZED CHECKPOINTS AT SAME HEIGHT:",
      fa[1] == fb[1] and fa[1] >= 1 and fa[0] != fb[0])
