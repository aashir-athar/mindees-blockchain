"""
Mindees Proof-of-Stake consensus  --  Phase 2.

Why PoS is where the "10x better than BTC" actually comes from:
  * Finality in one block (seconds), not ~6 confirmations / ~60 minutes.
  * ~99.9% less energy: a validator signs a block, it does not burn electricity.
  * Security is bought with staked capital that is on-chain and slashable, not
    with hardware nobody can audit.

This layer sits on top of core.Blockchain via the consensus hooks it exposes.
It adds three things and nothing more (ponytail: minimum that is actually secure):

  1. Staking: coins move from a sender's liquid balance into an on-chain stake.
     Modelled as ordinary *signed* transactions whose recipient is a reserved
     sentinel -- so the entire signature/nonce/fee machinery is reused unchanged.
  2. Deterministic, stake-weighted validator election per block height. The seed
     is the previous block hash + height, so every node elects the same proposer
     without communication and the result is verifiable after the fact.
  3. Block proposer authentication: the elected validator signs the block hash.
     Without this, anyone could forge a block claiming to be the elected proposer.

Slashing of equivocating validators is a later phase and intentionally not here.

Self-testing: run directly ->  python consensus.py
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

from core import (
    COIN,
    MAX_SUPPLY_UNITS,
    NAME,
    SLASH_SENTINEL,
    STAKE_SENTINEL,
    SYMBOL,
    UNSTAKE_SENTINEL,
    VOTE_SENTINEL,
    Block,
    Blockchain,
    Transaction,
    ValidationError,
    Wallet,
    address_from_public_key,
    canonical,
    is_units,
    sha256,
    verify_signature,
)
from network import decode_block, decode_tx, encode_block, encode_tx
from vesting import locked

# Finality (Casper-FFG). EPOCH is genesis-anchored (a block at height % EPOCH == 0 is a
# checkpoint). A link justifies/finalizes at >= 2/3 of the source checkpoint's active stake.
DEFAULT_EPOCH = 32
FFG_NUM, FFG_DEN = 2, 3  # supermajority is FFG_DEN*voted >= FFG_NUM*total, i.e. >= 2/3

# Slashing economics. 100% of bonded is confiscated for proven equivocation; of that,
# REPORTER_BPS goes to the whistleblower and the rest to the treasury -- a redistribution,
# never a burn, so the fixed 1,000,000 supply is preserved exactly.
SLASH_BPS = 10000     # 100% of the offender's bonded stake
REPORTER_BPS = 500    # 5% of the slashed amount to the evidence author; 95% to treasury


def _hexbytes(s) -> bytes:
    """Parse attacker-controlled hex, raising ValidationError (not a raw ValueError)."""
    try:
        return bytes.fromhex(s)
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"malformed hex field: {exc}") from exc


def _load_json(s) -> dict:
    """Parse attacker-controlled evidence JSON into a dict, raising ValidationError."""
    try:
        obj = json.loads(s)
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"malformed evidence json: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValidationError("evidence must be a JSON object")
    return obj


def elect_validator(stakes: Dict[str, int], seed: bytes) -> str:
    """Deterministic stake-weighted proposer selection. Pure function of (stakes, seed)."""
    validators = sorted(a for a, amt in stakes.items() if amt > 0)
    total = sum(stakes[a] for a in validators)
    if total == 0:
        raise ValidationError("no active validators")
    pick = int.from_bytes(seed, "big") % total
    cumulative = 0
    for addr in validators:
        cumulative += stakes[addr]
        if pick < cumulative:
            return addr
    return validators[-1]  # defensive; unreachable given pick < total


class ProofOfStakeChain(Blockchain):
    def __init__(
        self,
        allocations: Dict[str, int],
        initial_stakes: Optional[Dict[str, int]] = None,
        timestamp: int = 0,
        vesting: Optional[Dict[str, tuple]] = None,
        unbonding_blocks: int = 0,
        treasury_address: Optional[str] = None,
        epoch: int = DEFAULT_EPOCH,
    ):
        super().__init__(allocations, timestamp)
        # Bootstrap validators: move some genesis balance into stake so block 1
        # has an electable set. Supply is unchanged -- it just shifts liquid->staked.
        self.stakes: Dict[str, int] = {}
        for addr, amt in (initial_stakes or {}).items():
            if not is_units(amt) or amt == 0:
                raise ValidationError("initial stake must be a positive integer")
            if self.balances.get(addr, 0) < amt:
                raise ValidationError("initial stake exceeds genesis balance")
            self.balances[addr] -= amt
            self.stakes[addr] = self.stakes.get(addr, 0) + amt

        # Vesting grants: addr -> (total, start, cliff, duration). Fixed at genesis,
        # read-only thereafter, so they need no per-block snapshot/commit.
        self.vesting: Dict[str, tuple] = {a: tuple(g) for a, g in (vesting or {}).items()}
        for addr, grant in self.vesting.items():
            if len(grant) != 4 or not all(is_units(x) for x in grant):
                raise ValidationError("vesting grant must be 4 non-negative integers")
            held = self.balances.get(addr, 0) + self.stakes.get(addr, 0)
            if grant[0] > held:
                raise ValidationError(f"vesting grant for {addr} exceeds its genesis holding")

        # Slashing/unbonding genesis config (anchored here so replay is deterministic).
        # unbonding_blocks == 0 keeps the legacy instant-unstake behaviour; a real
        # deployment sets it > worst-case equivocation-detection latency. treasury_address
        # receives the non-reporter share of slashes and is a first-class balance holder.
        self.unbonding_blocks = unbonding_blocks
        self.treasury_address = treasury_address
        if treasury_address is not None:
            self.balances.setdefault(treasury_address, 0)  # adds 0 -> supply sum unchanged
        # addr -> list of (amount, bond_height, release_height); addr -> set of offenses
        self.unbonding: Dict[str, list] = {}
        self.slashed: set = set()

        # Finality (FFG) state. All reproducible by replaying blocks, so a disk-booted node
        # and a live node derive identical finalized state. Genesis is a checkpoint that is
        # justified and finalized by definition.
        if epoch <= 0:
            raise ValidationError("epoch must be positive")
        self.epoch = epoch
        genesis_hash = self.head.hash
        self.ffg_stake: Dict[str, Dict[str, int]] = {genesis_hash: dict(self.stakes)}
        self.ffg_height: Dict[str, int] = {genesis_hash: 0}
        self.ffg_votes: Dict[tuple, set] = {}            # (source_hash, target_hash) -> {voter}
        self.ffg_seen_target: Dict[tuple, tuple] = {}    # (voter, target_height) -> (s, t)
        self.justified: set = {genesis_hash}
        self.finalized: tuple = (genesis_hash, 0)        # (hash, height), monotonic

    # -- queries ----------------------------------------------------------- #
    def total_supply(self) -> int:
        return (sum(self.balances.values()) + sum(self.stakes.values())
                + self._unbonding_grand_total())

    def _unbonding_grand_total(self) -> int:
        return sum(amt for q in self.unbonding.values() for (amt, _, _) in q)

    def _unbonding_total(self, address: str) -> int:
        return sum(amt for (amt, _, _) in self.unbonding.get(address, []))

    def stake_of(self, address: str) -> int:
        return self.stakes.get(address, 0)

    def unbonding_of(self, address: str) -> int:
        return self._unbonding_total(address)

    def finalized_checkpoint(self) -> tuple:
        """(hash, height) of the highest finalized checkpoint (genesis until finality runs)."""
        return self.finalized

    def is_justified(self, checkpoint_hash: str) -> bool:
        return checkpoint_hash in self.justified

    def locked_of(self, address: str, height: Optional[int] = None) -> int:
        """Base units still vesting-locked for an address at the given height (default: head)."""
        grant = self.vesting.get(address)
        if not grant:
            return 0
        h = self.head.index if height is None else height
        return locked(grant[0], grant[1], grant[2], grant[3], h)

    def spendable_of(self, address: str) -> int:
        """Liquid balance the address may actually move right now (locked coins excluded)."""
        bal = self.balances.get(address, 0)
        lock = self.locked_of(address)
        if lock <= 0:
            return bal
        holding = bal + self.stakes.get(address, 0) + self._unbonding_total(address)
        return max(0, min(bal, holding - lock))

    def _seed(self, prev_hash: str, index: int) -> bytes:
        return sha256(bytes.fromhex(prev_hash) + index.to_bytes(8, "big"))

    def _elect(self, prev_hash: str, index: int) -> str:
        return elect_validator(self.stakes, self._seed(prev_hash, index))

    def next_validator(self) -> str:
        """The address that is allowed to produce the next block."""
        return self._elect(self.head.hash, self.head.index + 1)

    # -- block production -------------------------------------------------- #
    def add_block(
        self,
        transactions: List[Transaction],
        validator_wallet: Wallet,
        timestamp: int,
        nonce: int = 0,
    ) -> Block:
        block = seal_block(
            self.head.hash, self.head.index + 1, validator_wallet, transactions, timestamp, nonce
        )
        return self.submit_block(block)  # _apply (with PoS hooks) + append

    # -- consensus hooks (override core.Blockchain) ------------------------ #
    def _check_proposer(self, block: Block) -> None:
        expected = self._elect(block.previous_hash, block.index)
        if block.validator != expected:
            raise ValidationError(f"wrong proposer: expected {expected}, got {block.validator}")
        if not block.proposer_pubkey or not block.validator_sig:
            raise ValidationError("missing validator block signature")
        pub = _hexbytes(block.proposer_pubkey)
        if address_from_public_key(pub) != block.validator:
            raise ValidationError("proposer pubkey does not match validator address")
        if not verify_signature(pub, bytes.fromhex(block.hash), _hexbytes(block.validator_sig)):
            raise ValidationError("invalid validator block signature")

    def _snapshot_aux(self) -> dict:
        # DEEP copies everywhere: a shallow copy would alias committed state across
        # fork-choice replays and corrupt supply or finality accounting.
        return {
            "stakes": dict(self.stakes),
            "unbonding": {a: [tuple(e) for e in q] for a, q in self.unbonding.items()},
            "slashed": set(self.slashed),
            "ffg_stake": {h: dict(m) for h, m in self.ffg_stake.items()},
            "ffg_height": dict(self.ffg_height),
            "ffg_votes": {k: set(v) for k, v in self.ffg_votes.items()},
            "ffg_seen_target": dict(self.ffg_seen_target),
            "justified": set(self.justified),
            "finalized": self.finalized,
        }

    def _commit_aux(self, aux) -> None:
        self.stakes = aux["stakes"]
        self.unbonding = aux["unbonding"]
        self.slashed = aux["slashed"]
        self.ffg_stake = aux["ffg_stake"]
        self.ffg_height = aux["ffg_height"]
        self.ffg_votes = aux["ffg_votes"]
        self.ffg_seen_target = aux["ffg_seen_target"]
        self.justified = aux["justified"]
        self.finalized = aux["finalized"]

    def _check_supply(self, balances, aux) -> None:
        total = sum(balances.values()) + sum(aux["stakes"].values())
        total += sum(amt for q in aux["unbonding"].values() for (amt, _, _) in q)
        if total != MAX_SUPPLY_UNITS:
            raise ValidationError("supply conservation violated")

    def _apply_block_pre(self, block: Block, balances, nonces, aux) -> None:
        # Mature unbonding stake whose release height has arrived (keyed on block.index,
        # never wall-clock, so every replaying node agrees). unbonding -> balance.
        unbonding = aux["unbonding"]
        for addr in list(unbonding.keys()):
            remaining = []
            for entry in unbonding[addr]:
                amount, _bond_h, release_h = entry
                if release_h <= block.index:
                    balances[addr] = balances.get(addr, 0) + amount
                else:
                    remaining.append(entry)
            if remaining:
                unbonding[addr] = remaining
            else:
                del unbonding[addr]

        # FFG: freeze the parent's justified set (a vote's source must be justified BEFORE
        # this block, never by a vote in the same block), then snapshot active stake at
        # epoch-boundary checkpoints. Both are pure functions of the block sequence, so
        # finality replays identically on every node and from disk.
        aux["justified_parent"] = set(aux["justified"])
        if block.index % self.epoch == 0:
            aux["ffg_stake"][block.hash] = dict(aux["stakes"])
            aux["ffg_height"][block.hash] = block.index

    def _apply_tx(self, tx: Transaction, block: Block, balances, nonces, aux) -> None:
        stakes = aux["stakes"]
        unbonding = aux["unbonding"]
        if tx.recipient == STAKE_SENTINEL:
            cost = tx.amount + tx.fee
            if balances.get(tx.sender, 0) < cost:
                raise ValidationError(f"insufficient funds to stake for {tx.sender}")
            balances[tx.sender] -= cost
            stakes[tx.sender] = stakes.get(tx.sender, 0) + tx.amount
            balances[block.validator] = balances.get(block.validator, 0) + tx.fee
            nonces[tx.sender] = tx.nonce + 1
        elif tx.recipient == UNSTAKE_SENTINEL:
            if balances.get(tx.sender, 0) < tx.fee:
                raise ValidationError(f"insufficient funds for unstake fee for {tx.sender}")
            if stakes.get(tx.sender, 0) < tx.amount:
                raise ValidationError(f"insufficient stake to withdraw for {tx.sender}")
            balances[tx.sender] -= tx.fee
            stakes[tx.sender] -= tx.amount
            if self.unbonding_blocks > 0:
                # Delayed withdrawal: coins stay bonded (and slashable) until maturity.
                unbonding.setdefault(tx.sender, []).append(
                    (tx.amount, block.index, block.index + self.unbonding_blocks)
                )
            else:
                balances[tx.sender] += tx.amount  # legacy instant unstake
            balances[block.validator] = balances.get(block.validator, 0) + tx.fee
            if stakes[tx.sender] == 0:
                del stakes[tx.sender]
            nonces[tx.sender] = tx.nonce + 1
        elif tx.recipient == SLASH_SENTINEL:
            self._apply_slash(tx, block, balances, nonces, aux)
            return  # the slash sender is only charged a fee; no vesting check on it
        elif tx.recipient == VOTE_SENTINEL:
            self._apply_vote(tx, block, balances, nonces, aux)
            return  # finality vote: only a fee moves; no vesting check on it
        else:
            super()._apply_tx(tx, block, balances, nonces, aux)

        # Vesting: after the tx, the sender's holding (liquid + staked + unbonding) must
        # not drop below what is still locked. Rearranging within your own holding is
        # fine; sending locked coins away is not.
        self._enforce_vesting(tx.sender, block.index, balances, stakes, unbonding)

    def _apply_vote(self, tx: Transaction, block: Block, balances, nonces, aux) -> None:
        link = _load_json(tx.evidence)
        try:
            s, sh, t, th = link["s"], link["sh"], link["t"], link["th"]
        except KeyError as exc:
            raise ValidationError(f"vote link missing field {exc}") from exc
        if not (isinstance(s, str) and isinstance(t, str) and is_units(sh) and is_units(th)):
            raise ValidationError("vote link fields have wrong types")
        ffg_stake = aux["ffg_stake"]
        ffg_height = aux["ffg_height"]

        # Shape: both endpoints are epoch boundaries and the target is above the source.
        if sh % self.epoch != 0 or th % self.epoch != 0 or th <= sh:
            raise ValidationError("vote: source/target are not ordered epoch boundaries")
        # Both checkpoints exist on THIS branch (snapshotted in _apply_block_pre), and the
        # claimed heights match reality -- so the source is the unique ancestor at sh.
        if s not in ffg_stake or t not in ffg_stake:
            raise ValidationError("vote: source/target is not a checkpoint on this branch")
        if ffg_height[s] != sh or ffg_height[t] != th:
            raise ValidationError("vote: link heights do not match the checkpoints")
        # The source must already be justified BEFORE this block (no same-block chaining).
        if s not in aux["justified_parent"]:
            raise ValidationError("vote: source checkpoint is not justified")

        voter = tx.sender
        if ffg_stake[s].get(voter, 0) <= 0:
            raise ValidationError("vote: voter has no stake at the source checkpoint")
        # One vote per (voter, target_height): a second, different vote is a slashable fault,
        # rejected here; re-including the identical vote is idempotent (set semantics below).
        seen = aux["ffg_seen_target"]
        if seen.get((voter, th), (s, t)) != (s, t):
            raise ValidationError("vote: conflicting vote already cast for this target height")
        if balances.get(voter, 0) < tx.fee:
            raise ValidationError(f"vote: insufficient funds for fee for {voter}")

        balances[voter] -= tx.fee
        balances[block.validator] = balances.get(block.validator, 0) + tx.fee
        nonces[voter] = tx.nonce + 1

        voters = aux["ffg_votes"].setdefault((s, t), set())
        voters.add(voter)
        seen[(voter, th)] = (s, t)

        # Justify the target at >= 2/3 of the SOURCE snapshot's stake (integer-exact).
        snapshot = ffg_stake[s]
        voted = sum(snapshot.get(v, 0) for v in voters)
        total = sum(snapshot.values())
        if FFG_DEN * voted >= FFG_NUM * total:
            aux["justified"].add(t)
            # Finalize the source iff the target is its DIRECT epoch child (gap-free).
            if th == sh + self.epoch and sh > aux["finalized"][1]:
                aux["finalized"] = (s, sh)

    def _apply_slash(self, tx: Transaction, block: Block, balances, nonces, aux) -> None:
        if not tx.evidence:
            raise ValidationError("slash tx carries no evidence")
        if self.treasury_address is None:
            raise ValidationError("chain has no treasury address for slashing")
        data = _load_json(tx.evidence)
        # Two fault kinds, one confiscation path. Dedup keys are type-disjoint so a block
        # fault and a vote fault by the same validator at one height never silence each other.
        if data.get("kind") == "vote":
            offender, offense_key = verify_vote_fault(decode_tx(data["a"]), decode_tx(data["b"]))
        else:
            offender, height = verify_equivocation(decode_block(data["a"]), decode_block(data["b"]))
            offense_key = (offender, height)

        slashed = aux["slashed"]
        if offense_key in slashed:
            raise ValidationError("offense already slashed")
        if balances.get(tx.sender, 0) < tx.fee:
            raise ValidationError(f"insufficient funds for slash fee for {tx.sender}")

        stakes = aux["stakes"]
        unbonding = aux["unbonding"]
        # Reporter pays the fee to the includer (normal fee path).
        balances[tx.sender] -= tx.fee
        balances[block.validator] = balances.get(block.validator, 0) + tx.fee

        # Confiscate 100% of the offender's bonded amount (active stake + still-unbonding).
        bonded = stakes.get(offender, 0) + sum(a for (a, _, _) in unbonding.get(offender, []))
        stakes.pop(offender, None)
        unbonding.pop(offender, None)

        reporter_share = bonded * REPORTER_BPS // 10000
        treasury_share = bonded - reporter_share
        # The offender can never profit from their own slash: if they report it themselves,
        # the reporter share is redirected to the treasury too.
        beneficiary = tx.sender if tx.sender != offender else self.treasury_address
        balances[beneficiary] = balances.get(beneficiary, 0) + reporter_share
        balances[self.treasury_address] = balances.get(self.treasury_address, 0) + treasury_share

        slashed.add(offense_key)  # one offense -> one slash per history
        nonces[tx.sender] = tx.nonce + 1

    def _enforce_vesting(self, address: str, height: int, balances, stakes, unbonding) -> None:
        grant = self.vesting.get(address)
        if not grant:
            return
        lock = locked(grant[0], grant[1], grant[2], grant[3], height)
        if lock <= 0:
            return
        holding = (balances.get(address, 0) + stakes.get(address, 0)
                   + sum(a for (a, _, _) in unbonding.get(address, [])))
        if holding < lock:
            raise ValidationError(
                f"vesting: {address} must retain >= {lock} units until vested (would leave {holding})"
            )


def seal_block(
    prev_hash: str,
    index: int,
    validator_wallet: Wallet,
    transactions: List[Transaction],
    timestamp: int,
    nonce: int = 0,
) -> Block:
    """Build a block and have the proposer authenticate it by signing its hash.

    Shared by normal block production and the fork-choice engine so there is exactly
    one way a valid block is sealed.
    """
    block = Block(
        index=index,
        previous_hash=prev_hash,
        timestamp=timestamp,
        transactions=transactions,
        validator=validator_wallet.address,
        nonce=nonce,
    )
    block.proposer_pubkey = validator_wallet.public_key_hex
    block.validator_sig = validator_wallet.sign(bytes.fromhex(block.hash)).hex()
    return block


def verify_equivocation(b1: Block, b2: Block) -> tuple:
    """Verify two blocks prove equivocation; return (offender_address, offense_height).

    Equivocation = two DISTINCT blocks on the SAME parent at the SAME height, both sealed
    by the SAME validator with a valid signature. The shared-parent requirement is the
    red-team fix that makes slashing an honest validator impossible: an honest validator
    legitimately elected on two competing post-partition histories signs one block on each
    DIFFERENT parent and is not a double-signer. Each block self-authenticates via the
    offender's own key, so no one can forge evidence against a validator.

    # ponytail: v1 omits re-running parent election here -- self-authentication + shared
    # parent already guarantees only the key-holder could produce this pair, and honest
    # software never seals two blocks on one parent. Re-electing against parent state is a
    # later hardening (needs the parent's stake snapshot).
    """
    if b1.index != b2.index:
        raise ValidationError("evidence: blocks are at different heights")
    if b1.previous_hash != b2.previous_hash:
        raise ValidationError("evidence: blocks have different parents (not equivocation)")
    if b1.validator != b2.validator:
        raise ValidationError("evidence: blocks have different validators")
    if b1.hash == b2.hash:
        raise ValidationError("evidence: the two blocks are identical")
    for b in (b1, b2):
        if not b.proposer_pubkey or not b.validator_sig:
            raise ValidationError("evidence: block is not self-authenticating")
        pub = _hexbytes(b.proposer_pubkey)
        if address_from_public_key(pub) != b.validator:
            raise ValidationError("evidence: proposer pubkey does not match validator")
        if not verify_signature(pub, bytes.fromhex(b.hash), _hexbytes(b.validator_sig)):
            raise ValidationError("evidence: invalid validator signature")
    return b1.validator, b1.index


def verify_vote_fault(tx_a: Transaction, tx_b: Transaction) -> tuple:
    """Verify two finality votes prove a slashable fault; return (offender, offense_id).

    Both faults are proven from two of the validator's OWN signed votes (so an honest
    validator who casts one vote per target can never be framed):
      * DOUBLE  vote: same target height, but a different target or source.
      * SURROUND vote: one link strictly surrounds the other (sh2 < sh1 and th1 < th2).
    The offense_id is a hex string (block-equivocation offenses stay (offender,height)
    tuples) so the two fault kinds never collide in the slashed set.
    """
    if not tx_a.is_valid() or not tx_b.is_valid():
        raise ValidationError("vote-fault: a vote tx is invalid")
    if tx_a.recipient != VOTE_SENTINEL or tx_b.recipient != VOTE_SENTINEL:
        raise ValidationError("vote-fault: not finality votes")
    if tx_a.sender != tx_b.sender:
        raise ValidationError("vote-fault: votes are from different voters")
    la, lb = _load_json(tx_a.evidence), _load_json(tx_b.evidence)
    try:
        la = {"s": la["s"], "sh": la["sh"], "t": la["t"], "th": la["th"]}
        lb = {"s": lb["s"], "sh": lb["sh"], "t": lb["t"], "th": lb["th"]}
    except KeyError as exc:
        raise ValidationError(f"vote-fault: link missing field {exc}") from exc
    double = la["th"] == lb["th"] and (la["t"] != lb["t"] or la["s"] != lb["s"])
    surround = (la["sh"] < lb["sh"] and lb["th"] < la["th"]) or \
               (lb["sh"] < la["sh"] and la["th"] < lb["th"])
    if not (double or surround):
        raise ValidationError("vote-fault: votes are neither a double nor a surround")
    offense_id = sha256(canonical(sorted([tx_a.txid, tx_b.txid]))).hex()
    return tx_a.sender, offense_id


def slash_tx(reporter: Wallet, b1: Block, b2: Block, nonce: int, fee: int = 0) -> Transaction:
    """Build a signed slash transaction carrying block-equivocation evidence for (b1, b2)."""
    evidence = canonical({"kind": "block", "a": encode_block(b1), "b": encode_block(b2)}).decode()
    tx = Transaction(reporter.address, SLASH_SENTINEL, 0, fee, nonce, evidence=evidence)
    return tx.sign(reporter)


def vote_tx(voter: Wallet, source_hash: str, source_height: int, target_hash: str,
            target_height: int, nonce: int, fee: int = 0) -> Transaction:
    """Build a signed FFG finality vote: a link (source -> target) over checkpoints."""
    link = canonical({"s": source_hash, "sh": source_height,
                      "t": target_hash, "th": target_height}).decode()
    tx = Transaction(voter.address, VOTE_SENTINEL, 0, fee, nonce, evidence=link)
    return tx.sign(voter)


def vote_slash_tx(reporter: Wallet, vote_a: Transaction, vote_b: Transaction,
                  nonce: int, fee: int = 0) -> Transaction:
    """Build a signed slash transaction carrying a finality vote-fault (double/surround)."""
    evidence = canonical({"kind": "vote", "a": encode_tx(vote_a), "b": encode_tx(vote_b)}).decode()
    tx = Transaction(reporter.address, SLASH_SENTINEL, 0, fee, nonce, evidence=evidence)
    return tx.sign(reporter)


# Convenience constructors for staking transactions (reuse the signed-tx machinery).
def stake_tx(wallet: Wallet, amount: int, fee: int, nonce: int) -> Transaction:
    return Transaction(wallet.address, STAKE_SENTINEL, amount, fee, nonce).sign(wallet)


def unstake_tx(wallet: Wallet, amount: int, fee: int, nonce: int) -> Transaction:
    return Transaction(wallet.address, UNSTAKE_SENTINEL, amount, fee, nonce).sign(wallet)


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #
def _demo() -> None:
    alice = Wallet.from_secret(1)
    bob = Wallet.from_secret(2)
    v1 = Wallet.from_secret(3)
    v2 = Wallet.from_secret(4)
    wallets = {w.address: w for w in (alice, bob, v1, v2)}

    allocations = {
        alice.address: MAX_SUPPLY_UNITS - 2000 * COIN,
        v1.address: 1000 * COIN,
        v2.address: 1000 * COIN,
    }
    chain = ProofOfStakeChain(
        allocations,
        initial_stakes={v1.address: 1000 * COIN, v2.address: 1000 * COIN},
        timestamp=1_700_000_000,
    )
    assert chain.total_supply() == MAX_SUPPLY_UNITS
    assert chain.stake_of(v1.address) == 1000 * COIN
    assert chain.balance_of(v1.address) == 0  # genesis balance moved into stake

    # Election is deterministic and stake-weighted.
    assert chain.next_validator() == chain.next_validator()

    # Happy path: only the elected validator can produce the block.
    elected = chain.next_validator()
    tx = Transaction(alice.address, bob.address, 100 * COIN, 1 * COIN, 0).sign(alice)
    chain.add_block([tx], wallets[elected], timestamp=1_700_000_010)
    assert chain.balance_of(bob.address) == 100 * COIN
    assert chain.total_supply() == MAX_SUPPLY_UNITS
    assert chain.is_valid_chain()

    # Wrong proposer (bob holds no stake -> never electable for any height) is
    # rejected and leaves state untouched. Using a zero-stake account keeps this
    # deterministic even though signatures (and thus block hashes / which staker is
    # elected next) vary run-to-run with ECDSA's random nonce.
    tx2 = Transaction(alice.address, bob.address, 1 * COIN, 0, 1).sign(alice)
    try:
        chain.add_block([tx2], bob, timestamp=1_700_000_020)
        raise AssertionError("wrong proposer should be rejected")
    except ValidationError:
        pass
    assert chain.balance_of(bob.address) == 100 * COIN

    # Forged proposer signature (right elected address, attacker's key) is rejected.
    elected2 = chain.next_validator()
    forged = Block(
        index=chain.head.index + 1,
        previous_hash=chain.head.hash,
        timestamp=1_700_000_030,
        transactions=[],
        validator=elected2,
    )
    forged.proposer_pubkey = bob.public_key_hex
    forged.validator_sig = bob.sign(bytes.fromhex(forged.hash)).hex()
    try:
        chain._apply(forged)
        raise AssertionError("forged proposer pubkey should be rejected")
    except ValidationError:
        pass

    # Staking: Alice locks 500 MIND and becomes a validator. Supply is unchanged.
    n = chain.nonces[alice.address]
    p = chain.next_validator()
    chain.add_block([stake_tx(alice, 500 * COIN, 0, n)], wallets[p], timestamp=1_700_000_040)
    assert chain.stake_of(alice.address) == 500 * COIN
    assert chain.total_supply() == MAX_SUPPLY_UNITS
    wallets[alice.address] = alice  # alice can now be elected

    # Unstaking: Alice withdraws 200 MIND back to liquid. Supply still unchanged.
    n = chain.nonces[alice.address]
    p = chain.next_validator()
    bal_before = chain.balance_of(alice.address)
    chain.add_block([unstake_tx(alice, 200 * COIN, 0, n)], wallets[p], timestamp=1_700_000_050)
    assert chain.stake_of(alice.address) == 300 * COIN
    assert chain.balance_of(alice.address) == bal_before + 200 * COIN
    assert chain.total_supply() == MAX_SUPPLY_UNITS

    # Election distribution: equal stake -> roughly equal selection across many seeds.
    counts: Dict[str, int] = {}
    pool = {v1.address: 1000, v2.address: 1000}
    for i in range(2000):
        winner = elect_validator(pool, sha256(i.to_bytes(8, "big")))
        counts[winner] = counts.get(winner, 0) + 1
    assert counts.get(v1.address, 0) > 700 and counts.get(v2.address, 0) > 700

    # ----------------------------------------------------------------- #
    # Slashing + unbonding (Phase 10)
    # ----------------------------------------------------------------- #
    treasury = Wallet.from_secret(7)
    val_wallets = {v1.address: v1, v2.address: v2}

    def slashing_chain(unbonding_blocks=10):
        allocs = {
            alice.address: MAX_SUPPLY_UNITS - 2000 * COIN,
            v1.address: 1000 * COIN,
            v2.address: 1000 * COIN,
        }
        return ProofOfStakeChain(
            allocs,
            initial_stakes={v1.address: 1000 * COIN, v2.address: 1000 * COIN},
            timestamp=1_700_000_000,
            unbonding_blocks=unbonding_blocks,
            treasury_address=treasury.address,
        )

    reporter_share = 1000 * COIN * REPORTER_BPS // 10000
    treasury_share = 1000 * COIN - reporter_share

    # GATE: supply stays EXACTLY 1,000,000 after a slash; bonded stake redistributes.
    c = slashing_chain()
    off = c.next_validator()
    ow = val_wallets[off]
    b1 = seal_block(c.head.hash, 1, ow, [], 1_700_000_001)
    b2 = seal_block(c.head.hash, 1, ow, [], 1_700_000_002)  # same parent/height -> equivocation
    assert b1.hash != b2.hash
    assert verify_equivocation(b1, b2) == (off, 1)
    c.submit_block(b1)  # one of the two becomes the canonical height-1 block
    reporter_bal_before = c.balance_of(alice.address)
    c.add_block([slash_tx(alice, b1, b2, c.nonces.get(alice.address, 0))],
                val_wallets[c.next_validator()], 1_700_000_010)
    assert c.stake_of(off) == 0
    assert c.balance_of(alice.address) == reporter_bal_before + reporter_share
    assert c.balance_of(treasury.address) == treasury_share
    assert c.total_supply() == MAX_SUPPLY_UNITS  # THE promise holds verbatim

    # GATE: a second report of the same offense is rejected (slashed exactly once).
    try:
        c.add_block([slash_tx(alice, b1, b2, c.nonces.get(alice.address, 0))],
                    val_wallets[c.next_validator()], 1_700_000_020)
        raise AssertionError("double slash should be rejected")
    except ValidationError:
        pass

    # GATE: bounty-recapture is dead -- an equivocator reporting their OWN slash gets nothing.
    c2 = slashing_chain()
    off2 = c2.next_validator()
    ow2 = val_wallets[off2]
    e1 = seal_block(c2.head.hash, 1, ow2, [], 1_700_000_001)
    e2 = seal_block(c2.head.hash, 1, ow2, [], 1_700_000_002)
    c2.submit_block(e1)
    off_bal_before = c2.balance_of(off2)
    treas_before = c2.balance_of(treasury.address)
    c2.add_block([slash_tx(ow2, e1, e2, c2.nonces.get(off2, 0))],
                 val_wallets[c2.next_validator()], 1_700_000_010)
    assert c2.stake_of(off2) == 0
    assert c2.balance_of(off2) == off_bal_before            # recovers NOTHING
    assert c2.balance_of(treasury.address) == treas_before + 1000 * COIN  # all to treasury
    assert c2.total_supply() == MAX_SUPPLY_UNITS

    # GATE: an honest validator legitimately on two DIFFERENT parents is NOT slashable.
    cross_a = seal_block("aa" * 32, 5, v1, [], 1_700_000_000)
    cross_b = seal_block("bb" * 32, 5, v1, [], 1_700_000_000)
    try:
        verify_equivocation(cross_a, cross_b)
        raise AssertionError("cross-parent blocks must not count as equivocation")
    except ValidationError:
        pass

    # Forged evidence (identical blocks / different validators) is rejected.
    one = seal_block("cc" * 32, 3, v1, [], 1)
    try:
        verify_equivocation(one, one)
        raise AssertionError("identical blocks are not equivocation")
    except ValidationError:
        pass
    try:
        verify_equivocation(seal_block("dd" * 32, 3, v1, [], 1), seal_block("dd" * 32, 3, v2, [], 2))
        raise AssertionError("different-validator blocks are not equivocation")
    except ValidationError:
        pass

    # Unstake is a DELAYED withdrawal under an unbonding period, not instant.
    val = Wallet.from_secret(8)
    cu = ProofOfStakeChain(
        {alice.address: MAX_SUPPLY_UNITS - 300 * COIN, val.address: 300 * COIN},
        initial_stakes={val.address: 300 * COIN},
        timestamp=1_700_000_000, unbonding_blocks=5, treasury_address=treasury.address,
    )
    cu.add_block([unstake_tx(val, 200 * COIN, 0, 0)], val, 1_700_000_001)  # height 1
    assert cu.stake_of(val.address) == 100 * COIN
    assert cu.balance_of(val.address) == 0           # NOT credited yet
    assert cu.unbonding_of(val.address) == 200 * COIN
    assert cu.total_supply() == MAX_SUPPLY_UNITS
    for h in range(2, 7):                            # advance to release height (1 + 5 = 6)
        cu.add_block([], val, 1_700_000_000 + h)
    assert cu.balance_of(val.address) == 200 * COIN  # matured into liquid
    assert cu.unbonding_of(val.address) == 0
    assert cu.total_supply() == MAX_SUPPLY_UNITS

    # GATE: equivocate then immediately unstake -> the unbonding dodge still fails.
    ce = slashing_chain()
    oe = ce.next_validator()
    oew = val_wallets[oe]
    g1 = seal_block(ce.head.hash, 1, oew, [], 1_700_000_001)
    g2 = seal_block(ce.head.hash, 1, oew, [], 1_700_000_002)
    ce.submit_block(g1)
    ce.add_block([unstake_tx(oew, 1000 * COIN, 0, ce.nonces.get(oe, 0))],
                 val_wallets[ce.next_validator()], 1_700_000_010)  # stake -> unbonding
    assert ce.stake_of(oe) == 0 and ce.unbonding_of(oe) == 1000 * COIN
    ce.add_block([slash_tx(alice, g1, g2, ce.nonces.get(alice.address, 0))],
                 val_wallets[ce.next_validator()], 1_700_000_020)  # within the window
    assert ce.unbonding_of(oe) == 0                  # confiscated despite the exit attempt
    assert ce.balance_of(treasury.address) == treasury_share
    assert ce.total_supply() == MAX_SUPPLY_UNITS

    print("ALL CHECKS PASSED")
    print(f"  {NAME} ({SYMBOL}) PoS: blocks={len(chain.chain)}  validators={sorted(chain.stakes)}")
    print(f"  supply conserved at {MAX_SUPPLY_UNITS:,} units across transfers + staking + slashing")
    print("  slashing: equivocation punished, supply preserved, honest validators safe")


if __name__ == "__main__":
    _demo()
