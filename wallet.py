"""
Mindees CLI wallet  --  Phase 6.

A thin command-line client for a Mindees node. It holds keys, builds and signs
transactions locally (the secret never leaves your machine), and submits the signed
bytes to a node over JSON-RPC.

  python wallet.py new                              # print a fresh secret + address
  python wallet.py keygen --out wallet.json         # encrypted keystore ($MINDEES_PASSPHRASE)
  python wallet.py address <SECRET_HEX>
  python wallet.py balance <ADDRESS>                [--rpc URL]
  python wallet.py send <TO> <AMOUNT> [SECRET_HEX]  [--keystore F] [--fee F] [--rpc URL]
  python wallet.py stake <AMOUNT> [SECRET_HEX]      [--keystore F] [--fee F] [--rpc URL]
  python wallet.py unstake <AMOUNT> [SECRET_HEX]    [--keystore F] [--fee F] [--rpc URL]

The secret may be passed inline or, better, via an encrypted --keystore (unlocked with
$MINDEES_PASSPHRASE) so it never lands in shell history. Amounts are in whole MIND and may
be decimal (e.g. 1.5); they convert to base units with Decimal so money never rounds wrong.
Blocks are produced by the node (run it with --autoproduce to mine each submitted tx).

Self-testing: run directly with no args ->  python wallet.py
"""
from __future__ import annotations

import argparse
import os
import secrets
from decimal import Decimal

from consensus import STAKE_SENTINEL, UNSTAKE_SENTINEL, stake_tx, unstake_tx
from core import COIN, DECIMALS, SYMBOL, Transaction, Wallet
from keystore import decrypt_secret, encrypt_secret, load_keystore, save_keystore
from network import encode_tx
from node import rpc_call

DEFAULT_RPC = "http://127.0.0.1:8645/"


def _resolve_secret(args) -> str:
    """Secret hex from a positional secret, or from an encrypted --keystore.

    A keystore passphrase is read from $MINDEES_PASSPHRASE so it never lands in shell
    history; the secret is decrypted only in memory for this one command.
    """
    keystore_path = getattr(args, "keystore", None)
    if keystore_path:
        passphrase = os.environ.get("MINDEES_PASSPHRASE")
        if not passphrase:
            raise SystemExit("set $MINDEES_PASSPHRASE to use a keystore")
        return decrypt_secret(load_keystore(keystore_path), passphrase)
    if getattr(args, "secret", None):
        return args.secret
    raise SystemExit("provide a secret or --keystore PATH")


def new_secret() -> str:
    """A fresh 256-bit secret, hex-encoded. Retry on the ~2^-128 chance it exceeds n."""
    while True:
        s = secrets.randbits(256)
        try:
            Wallet.from_secret(s)
            return format(s, "064x")
        except Exception:
            continue


def parse_amount(value) -> int:
    """Whole-MIND string/number -> integer base units, exact (no float)."""
    units = Decimal(str(value)) * COIN
    if units != units.to_integral_value():
        raise ValueError(f"{value} {SYMBOL} is finer than the smallest unit (10^-{DECIMALS})")
    if units < 0:
        raise ValueError("amount must be non-negative")
    return int(units)


def _next_nonce(url: str, address: str) -> int:
    return rpc_call(url, "nonce", address=address)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="wallet", description="Mindees CLI wallet")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("new", help="generate a new secret + address")

    p_keygen = sub.add_parser("keygen", help="create an encrypted keystore ($MINDEES_PASSPHRASE)")
    p_keygen.add_argument("--out", required=True)

    p_addr = sub.add_parser("address", help="derive the address for a secret")
    p_addr.add_argument("secret")

    p_info = sub.add_parser("info", help="show node info")
    p_info.add_argument("--rpc", default=DEFAULT_RPC)

    p_bal = sub.add_parser("balance", help="show an address balance")
    p_bal.add_argument("address")
    p_bal.add_argument("--rpc", default=DEFAULT_RPC)

    for name in ("send", "stake", "unstake"):
        p = sub.add_parser(name, help=f"{name} MIND")
        if name == "send":
            p.add_argument("to")
        p.add_argument("amount")
        p.add_argument("secret", nargs="?", help="secret hex (omit and use --keystore)")
        p.add_argument("--fee", default="0")
        p.add_argument("--keystore", default=None, help="encrypted keystore ($MINDEES_PASSPHRASE)")
        p.add_argument("--rpc", default=DEFAULT_RPC)

    args = parser.parse_args(argv)

    if args.cmd == "new":
        secret = new_secret()
        print(f"secret : {secret}")
        print(f"address: {Wallet.from_secret(int(secret, 16)).address}")
        print("keep the secret safe -- it is the only way to spend these coins")
        return

    if args.cmd == "keygen":
        passphrase = os.environ.get("MINDEES_PASSPHRASE")
        if not passphrase:
            raise SystemExit("set $MINDEES_PASSPHRASE to encrypt the keystore")
        secret = new_secret()
        save_keystore(encrypt_secret(secret, passphrase), args.out)
        print(f"keystore written to {args.out}")
        print(f"address: {Wallet.from_secret(int(secret, 16)).address}")
        print("the secret is encrypted at rest; unlock it with your passphrase")
        return

    if args.cmd == "address":
        print(Wallet.from_secret(int(args.secret, 16)).address)
        return

    if args.cmd == "info":
        info = rpc_call(args.rpc, "info")
        print(f"{info['name']} ({info['symbol']})  height={info['height']}  "
              f"supply={info['supply'] // COIN:,}/{info['max_supply'] // COIN:,}  "
              f"mempool={info['mempool']}")
        return

    if args.cmd == "balance":
        units = rpc_call(args.rpc, "balance", address=args.address)
        print(f"{units // COIN:,}.{units % COIN:0{DECIMALS}d} {SYMBOL}  ({units} base units)")
        return

    # send / stake / unstake all build a signed tx and submit it.
    wallet = Wallet.from_secret(int(_resolve_secret(args), 16))
    amount = parse_amount(args.amount)
    fee = parse_amount(args.fee)
    nonce = _next_nonce(args.rpc, wallet.address)
    if args.cmd == "send":
        tx = Transaction(wallet.address, args.to, amount, fee, nonce).sign(wallet)
    elif args.cmd == "stake":
        tx = stake_tx(wallet, amount, fee, nonce)
    else:
        tx = unstake_tx(wallet, amount, fee, nonce)
    res = rpc_call(args.rpc, "submit_tx", tx=encode_tx(tx))
    print(f"submitted {args.cmd}: txid={res['txid']}")
    if res.get("mined"):
        print(f"  included in block #{res['mined']['index']}")


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #
def _demo() -> None:
    # Amount parsing is exact and rejects sub-unit dust.
    assert parse_amount("1.5") == 150_000_000
    assert parse_amount("0.00000001") == 1          # one base unit
    assert parse_amount(7) == 7 * COIN
    try:
        parse_amount("0.000000001")                  # finer than 10^-8
        raise AssertionError("sub-unit amount should be rejected")
    except ValueError:
        pass

    # Generated secrets are valid and round-trip to an address.
    secret = new_secret()
    assert len(secret) == 64
    w = Wallet.from_secret(int(secret, 16))
    assert w.address == Wallet.from_secret(int(secret, 16)).address

    # Locally built transactions are signed and valid before they ever hit the wire.
    alice = Wallet.from_secret(1)
    transfer = Transaction(alice.address, w.address, parse_amount("2.5"), parse_amount("0.1"), 0).sign(alice)
    assert transfer.is_valid()
    assert encode_tx(transfer)["amount"] == 250_000_000

    st = stake_tx(alice, parse_amount("10"), 0, 0)
    assert st.is_valid() and st.recipient == STAKE_SENTINEL
    ust = unstake_tx(alice, parse_amount("10"), 0, 0)
    assert ust.is_valid() and ust.recipient == UNSTAKE_SENTINEL

    # Secret resolution: inline secret, and an encrypted keystore unlocked via env.
    import shutil
    import tempfile

    class _Args:
        pass

    inline = _Args()
    inline.secret, inline.keystore = secret, None
    assert _resolve_secret(inline) == secret

    tmp = tempfile.mkdtemp(prefix="mindees_wks_")
    try:
        path = os.path.join(tmp, "k.json")
        save_keystore(encrypt_secret(secret, "pw"), path)
        os.environ["MINDEES_PASSPHRASE"] = "pw"
        ks_args = _Args()
        ks_args.secret, ks_args.keystore = None, path
        assert _resolve_secret(ks_args) == secret
    finally:
        os.environ.pop("MINDEES_PASSPHRASE", None)
        shutil.rmtree(tmp, ignore_errors=True)

    print("ALL CHECKS PASSED")
    print("  wallet: exact decimal amounts, local signing, send/stake/unstake builders")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        main()
    else:
        _demo()
