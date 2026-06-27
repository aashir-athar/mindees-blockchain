"""
Mindees core ledger primitives  --  Phase 1.

A from-scratch cryptocurrency designed to beat Bitcoin on the axes that matter:
  * Fixed supply of exactly 1,000,000 MINDEES, minted once at genesis. No mining
    inflation, no tail emission, no mint function anywhere -> the cap is a property
    of the code, not a policy that can drift.
  * Account model (like Ethereum) instead of UTXO: less data, fewer edge cases,
    cheaper to verify.
  * secp256k1 ECDSA signatures via the audited `cryptography` library -- we never
    hand-roll crypto.

This module is self-contained and self-testing. Run it directly to execute the
full correctness suite:  python core.py

Phase 1 scope = keys/addresses, signed transactions, blocks (Merkle), and a
fully-validating chain with hard-cap conservation. Consensus (PoS), P2P, mempool,
persistence and RPC are later phases and deliberately NOT here yet.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Dict, List

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

# secp256k1 group order. ECDSA signatures are malleable: (r, s) and (r, N-s) both verify.
# We canonicalize to low-S (s <= N/2) on signing and REJECT high-S on verify, so each
# transaction has exactly one valid signature -> one stable txid (no malleable twins).
_SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_HALF_N = _SECP256K1_N // 2

# --------------------------------------------------------------------------- #
# Monetary constants. Everything is integer base units -- never floats for money.
# --------------------------------------------------------------------------- #
NAME = "Mindees"
SYMBOL = "MIND"
DECIMALS = 8
COIN = 10 ** DECIMALS              # base units per 1 MINDEES
MAX_SUPPLY = 1_000_000             # whole coins, hard cap
MAX_SUPPLY_UNITS = MAX_SUPPLY * COIN

# Reserved transaction recipients: a tx to one of these is an operation, not a payment.
# Defined here (not in consensus) so Transaction.is_valid can recognise the slash shape.
STAKE_SENTINEL = "__MINDEES_STAKE__"
UNSTAKE_SENTINEL = "__MINDEES_UNSTAKE__"
SLASH_SENTINEL = "__MINDEES_SLASH__"
VOTE_SENTINEL = "__MINDEES_VOTE__"  # finality vote; FFG link rides in tx.evidence

_CURVE = ec.SECP256K1()
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class ValidationError(Exception):
    """Raised when a transaction or block violates a consensus rule."""


# --------------------------------------------------------------------------- #
# Small deterministic helpers
# --------------------------------------------------------------------------- #
def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def double_sha256(data: bytes) -> bytes:
    return sha256(sha256(data))


def canonical(obj) -> bytes:
    """Deterministic JSON encoding -- the single source of truth for hashing/signing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def is_units(v) -> bool:
    """A valid base-unit amount: a non-negative plain int (NOT a bool, NOT a float).

    Money is always integer base units. A float anywhere makes supply summation
    order-dependent across nodes -> a consensus fork; bool is an int subclass we must reject.
    """
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def b58check(payload: bytes) -> str:
    """Base58Check encode (version byte assumed already prepended)."""
    raw = payload + double_sha256(payload)[:4]
    n = int.from_bytes(raw, "big")
    out = ""
    while n > 0:
        n, rem = divmod(n, 58)
        out = _B58[rem] + out
    # Preserve leading zero bytes as '1'.
    for b in raw:
        if b == 0:
            out = "1" + out
        else:
            break
    return out


# --------------------------------------------------------------------------- #
# Wallet: keypair + address + signing
# --------------------------------------------------------------------------- #
class Wallet:
    def __init__(self, private_key: ec.EllipticCurvePrivateKey):
        self._priv = private_key
        self._pub = private_key.public_key()

    @classmethod
    def generate(cls) -> "Wallet":
        return cls(ec.generate_private_key(_CURVE))

    @classmethod
    def from_secret(cls, secret: int) -> "Wallet":
        """Deterministic wallet from an integer secret -- for tests/vectors only."""
        if not 1 <= secret < 2 ** 256:
            raise ValueError("secret out of range")
        return cls(ec.derive_private_key(secret, _CURVE))

    @property
    def public_key_bytes(self) -> bytes:
        return self._pub.public_bytes(Encoding.X962, PublicFormat.CompressedPoint)

    @property
    def public_key_hex(self) -> str:
        return self.public_key_bytes.hex()

    @property
    def address(self) -> str:
        return address_from_public_key(self.public_key_bytes)

    def sign(self, message: bytes) -> bytes:
        sig = self._priv.sign(message, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(sig)
        if s > _HALF_N:               # canonicalize to low-S so the signature is unique
            s = _SECP256K1_N - s
            sig = encode_dss_signature(r, s)
        return sig


def address_from_public_key(public_key_bytes: bytes) -> str:
    # ponytail: SHA-256[:20] as the pubkey hash, not RIPEMD160 -- ripemd160 is
    # disabled in OpenSSL 3 builds of hashlib, and 20 bytes of SHA-256 is just as
    # collision-safe for an address. Version byte 0x00.
    h = sha256(public_key_bytes)[:20]
    return b58check(b"\x00" + h)


def verify_signature(public_key_bytes: bytes, message: bytes, signature: bytes) -> bool:
    try:
        r, s = decode_dss_signature(signature)
        if not (1 <= r < _SECP256K1_N and 1 <= s <= _HALF_N):
            return False  # reject high-S (malleable) and out-of-range signatures
        pub = ec.EllipticCurvePublicKey.from_encoded_point(_CURVE, public_key_bytes)
        pub.verify(signature, message, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError):
        return False


# --------------------------------------------------------------------------- #
# Transaction
# --------------------------------------------------------------------------- #
@dataclass
class Transaction:
    sender: str          # base58 address
    recipient: str       # base58 address
    amount: int          # base units, > 0
    fee: int             # base units, >= 0
    nonce: int           # per-sender sequence, starts at 0
    public_key: str = "" # sender pubkey hex (binds signature to sender address)
    signature: str = ""  # DER signature hex
    evidence: str = ""   # canonical JSON of equivocation proof for a slash tx (else "")

    def _signing_payload(self) -> bytes:
        return canonical(
            {
                "sender": self.sender,
                "recipient": self.recipient,
                "amount": self.amount,
                "fee": self.fee,
                "nonce": self.nonce,
                "public_key": self.public_key,
                "evidence": self.evidence,
            }
        )

    def sign(self, wallet: Wallet) -> "Transaction":
        if wallet.address != self.sender:
            raise ValidationError("wallet does not own sender address")
        self.public_key = wallet.public_key_hex
        self.signature = wallet.sign(self._signing_payload()).hex()
        return self

    @property
    def txid(self) -> str:
        return sha256(
            canonical(
                {
                    "sender": self.sender,
                    "recipient": self.recipient,
                    "amount": self.amount,
                    "fee": self.fee,
                    "nonce": self.nonce,
                    "public_key": self.public_key,
                    "signature": self.signature,
                    "evidence": self.evidence,
                }
            )
        ).hex()

    def is_valid(self) -> bool:
        """Stateless validity: shape + signature + pubkey/address binding."""
        # amount/fee/nonce must be non-negative plain ints (no float, no bool) -- a float
        # amount would make ledger sums order-dependent across nodes (consensus fork).
        if not (is_units(self.amount) and is_units(self.fee) and is_units(self.nonce)):
            return False
        # Slash and finality-vote txs carry amount 0 + evidence; everything else is amount > 0.
        if self.recipient in (SLASH_SENTINEL, VOTE_SENTINEL):
            if self.amount != 0 or not self.evidence:
                return False
        elif self.amount <= 0:
            return False
        if not isinstance(self.evidence, str) or not self.public_key or not self.signature:
            return False
        try:
            pub = bytes.fromhex(self.public_key)
            sig = bytes.fromhex(self.signature)
        except ValueError:
            return False
        if address_from_public_key(pub) != self.sender:
            return False
        return verify_signature(pub, self._signing_payload(), sig)


# --------------------------------------------------------------------------- #
# Block
# --------------------------------------------------------------------------- #
def merkle_root(txids: List[str]) -> str:
    if not txids:
        return sha256(b"").hex()
    layer = [bytes.fromhex(t) for t in txids]
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])  # duplicate last (BTC convention)
        layer = [sha256(layer[i] + layer[i + 1]) for i in range(0, len(layer), 2)]
    return layer[0].hex()


@dataclass
class Block:
    index: int
    previous_hash: str
    timestamp: int
    transactions: List[Transaction]
    validator: str            # address that produced the block and earns fees
    nonce: int = 0            # placeholder until PoS lands in a later phase
    merkle_root: str = ""
    proposer_pubkey: str = ""  # set by the PoS layer: pubkey of the elected validator
    validator_sig: str = ""    # set by the PoS layer: validator's signature over block hash

    def __post_init__(self):
        if not self.merkle_root:
            self.merkle_root = merkle_root([tx.txid for tx in self.transactions])

    def header(self) -> dict:
        return {
            "index": self.index,
            "previous_hash": self.previous_hash,
            "timestamp": self.timestamp,
            "merkle_root": self.merkle_root,
            "validator": self.validator,
            "nonce": self.nonce,
        }

    @property
    def hash(self) -> str:
        return sha256(canonical(self.header())).hex()


# --------------------------------------------------------------------------- #
# Blockchain
# --------------------------------------------------------------------------- #
class Blockchain:
    GENESIS_PREV = "0" * 64

    def __init__(self, allocations: Dict[str, int], timestamp: int = 0):
        # ponytail: fixed supply minted once at genesis, fee-only incentives --
        # no block reward, so the cap can never be exceeded by construction.
        # Every amount MUST be a plain int: a float allocation makes the ledger float-typed,
        # and order-dependent float summation in _check_supply would give different
        # total_supply() on different nodes for the same chain -> a consensus fork.
        if not all(is_units(v) for v in allocations.values()):
            raise ValidationError("genesis allocations must be non-negative integers")
        total = sum(allocations.values())
        if total != MAX_SUPPLY_UNITS:
            raise ValidationError(
                f"genesis must allocate exactly {MAX_SUPPLY_UNITS} units, got {total}"
            )
        if any(v < 0 for v in allocations.values()):
            raise ValidationError("negative genesis allocation")

        self.balances: Dict[str, int] = dict(allocations)
        self.nonces: Dict[str, int] = {addr: 0 for addr in allocations}
        genesis = Block(
            index=0,
            previous_hash=self.GENESIS_PREV,
            timestamp=timestamp,
            transactions=[],
            validator="genesis",
        )
        self.chain: List[Block] = [genesis]

    # -- queries ----------------------------------------------------------- #
    def total_supply(self) -> int:
        return sum(self.balances.values())

    def balance_of(self, address: str) -> int:
        return self.balances.get(address, 0)

    @property
    def head(self) -> Block:
        return self.chain[-1]

    # -- mutation ---------------------------------------------------------- #
    def add_block(
        self,
        transactions: List[Transaction],
        validator: str,
        timestamp: int,
        nonce: int = 0,
    ) -> Block:
        block = Block(
            index=self.head.index + 1,
            previous_hash=self.head.hash,
            timestamp=timestamp,
            transactions=transactions,
            validator=validator,
            nonce=nonce,
        )
        return self.submit_block(block)

    def submit_block(self, block: Block) -> Block:
        """Validate and append an already-built block (local or received from a peer)."""
        self._apply(block)  # raises ValidationError on any rule violation
        self.chain.append(block)
        return block

    def _apply(self, block: Block) -> None:
        if block.previous_hash != self.head.hash:
            raise ValidationError("previous_hash mismatch")
        if block.index != self.head.index + 1:
            raise ValidationError("non-sequential block index")
        if block.merkle_root != merkle_root([tx.txid for tx in block.transactions]):
            raise ValidationError("merkle root mismatch")
        self._check_proposer(block)

        # Work on copies so a single bad tx can't half-apply a block (atomicity).
        balances = dict(self.balances)
        nonces = dict(self.nonces)
        aux = self._snapshot_aux()
        self._apply_block_pre(block, balances, nonces, aux)  # e.g. mature unbonding stake
        seen = set()

        for tx in block.transactions:
            if tx.txid in seen:
                raise ValidationError("duplicate transaction in block")
            seen.add(tx.txid)
            if not tx.is_valid():
                raise ValidationError(f"invalid signature/shape for tx {tx.txid}")
            if nonces.get(tx.sender, 0) != tx.nonce:
                raise ValidationError(
                    f"bad nonce for {tx.sender}: expected {nonces.get(tx.sender, 0)}, got {tx.nonce}"
                )
            self._apply_tx(tx, block, balances, nonces, aux)

        self._check_supply(balances, aux)
        self.balances = balances
        self.nonces = nonces
        self._commit_aux(aux)

    # --- consensus hooks: base chain is permissionless; subclasses (PoS) override --- #
    def _check_proposer(self, block: Block) -> None:
        """Base chain places no restriction on who may produce a block."""

    def _snapshot_aux(self) -> dict:
        """Extra mutable state a subclass needs to apply atomically (e.g. stakes)."""
        return {}

    def _apply_block_pre(self, block: Block, balances, nonces, aux) -> None:
        """Hook run once before the tx loop (height-keyed effects, e.g. maturing stake)."""

    def _apply_tx(self, tx: Transaction, block: Block, balances, nonces, aux) -> None:
        cost = tx.amount + tx.fee
        if balances.get(tx.sender, 0) < cost:
            raise ValidationError(f"insufficient funds for {tx.sender}")
        balances[tx.sender] = balances.get(tx.sender, 0) - cost
        balances[tx.recipient] = balances.get(tx.recipient, 0) + tx.amount
        balances[block.validator] = balances.get(block.validator, 0) + tx.fee
        nonces[tx.sender] = tx.nonce + 1
        nonces.setdefault(tx.recipient, nonces.get(tx.recipient, 0))

    def _check_supply(self, balances, aux) -> None:
        # Hard invariant: a block moves money, it never creates or destroys it.
        if sum(balances.values()) != MAX_SUPPLY_UNITS:
            raise ValidationError("supply conservation violated")

    def _commit_aux(self, aux) -> None:
        """Persist subclass aux state after a block validates."""

    def is_valid_chain(self) -> bool:
        for i in range(1, len(self.chain)):
            if self.chain[i].previous_hash != self.chain[i - 1].hash:
                return False
            if self.chain[i].index != self.chain[i - 1].index + 1:
                return False
        return True


# --------------------------------------------------------------------------- #
# Self-test (ponytail: one runnable check that fails loudly if the logic breaks)
# --------------------------------------------------------------------------- #
def _demo() -> None:
    alice = Wallet.from_secret(1)
    bob = Wallet.from_secret(2)
    val = Wallet.from_secret(3)  # validator / fee collector

    # Genesis: entire fixed supply to Alice.
    chain = Blockchain({alice.address: MAX_SUPPLY_UNITS}, timestamp=1_700_000_000)
    assert chain.total_supply() == MAX_SUPPLY_UNITS
    assert chain.is_valid_chain()

    # Address determinism + checksum sanity.
    assert alice.address == Wallet.from_secret(1).address
    assert alice.address != bob.address

    # Happy path: Alice -> Bob 100 MIND, fee 1 MIND, validator earns the fee.
    tx = Transaction(alice.address, bob.address, 100 * COIN, 1 * COIN, nonce=0).sign(alice)
    assert tx.is_valid()
    chain.add_block([tx], validator=val.address, timestamp=1_700_000_060)
    assert chain.balance_of(alice.address) == MAX_SUPPLY_UNITS - 101 * COIN
    assert chain.balance_of(bob.address) == 100 * COIN
    assert chain.balance_of(val.address) == 1 * COIN
    assert chain.total_supply() == MAX_SUPPLY_UNITS  # nothing minted or burned
    assert chain.is_valid_chain()

    # Tamper detection: mutate a signed tx -> signature must fail.
    bad = Transaction(alice.address, bob.address, 5 * COIN, 0, nonce=1).sign(alice)
    bad.amount = 5_000 * COIN
    assert not bad.is_valid()

    # Forgery: Bob signs a tx that claims to come from Alice.
    forged = Transaction(alice.address, bob.address, 1 * COIN, 0, nonce=1)
    forged.public_key = bob.public_key_hex
    forged.signature = bob.sign(forged._signing_payload()).hex()
    assert not forged.is_valid()  # pubkey doesn't hash to sender address

    # Overspend: Bob only has 100 MIND, tries to send 1000.
    over = Transaction(bob.address, alice.address, 1_000 * COIN, 0, nonce=0).sign(bob)
    try:
        chain.add_block([over], validator=val.address, timestamp=1_700_000_120)
        raise AssertionError("overspend should have been rejected")
    except ValidationError:
        pass

    # Bad nonce: Alice's next nonce is 1, submit 5.
    wrongnonce = Transaction(alice.address, bob.address, 1 * COIN, 0, nonce=5).sign(alice)
    try:
        chain.add_block([wrongnonce], validator=val.address, timestamp=1_700_000_180)
        raise AssertionError("bad nonce should have been rejected")
    except ValidationError:
        pass

    # Replay: re-submit the already-mined nonce-0 tx.
    try:
        chain.add_block([tx], validator=val.address, timestamp=1_700_000_240)
        raise AssertionError("replay should have been rejected")
    except ValidationError:
        pass

    # State is unchanged after every rejected block.
    assert chain.balance_of(alice.address) == MAX_SUPPLY_UNITS - 101 * COIN
    assert chain.total_supply() == MAX_SUPPLY_UNITS

    # Genesis allocation that doesn't equal the cap must be rejected.
    try:
        Blockchain({alice.address: 42})
        raise AssertionError("under-allocated genesis should have been rejected")
    except ValidationError:
        pass

    # Float / bool genesis allocations are rejected (a float ledger forks consensus).
    for bad in (float(MAX_SUPPLY_UNITS), True):
        try:
            Blockchain({alice.address: bad})
            raise AssertionError("non-integer genesis allocation should be rejected")
        except ValidationError:
            pass
    assert not is_units(1.0) and not is_units(True) and is_units(5) and not is_units(-1)

    # A float-amount transaction is invalid (never reaches the ledger as a float).
    float_tx = Transaction(alice.address, bob.address, 1.5, 0, 0).sign(alice)
    assert not float_tx.is_valid()

    # Low-S: signatures are canonical (s <= N/2) and the malleable high-S twin is rejected.
    msg = b"canonical-s-check"
    sig = alice.sign(msg)
    r, s = decode_dss_signature(sig)
    assert s <= _HALF_N
    assert verify_signature(alice.public_key_bytes, msg, sig)
    high_s_twin = encode_dss_signature(r, _SECP256K1_N - s)
    assert not verify_signature(alice.public_key_bytes, msg, high_s_twin)

    print("ALL CHECKS PASSED")
    print(f"  {NAME} ({SYMBOL})  supply={MAX_SUPPLY:,} coins = {MAX_SUPPLY_UNITS:,} units")
    print(f"  blocks={len(chain.chain)}  alice={alice.address}")


if __name__ == "__main__":
    _demo()
