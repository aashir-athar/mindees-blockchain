"""
Mindees encrypted keystore  --  at-rest key protection.

A wallet secret must never sit on disk in the clear. This stores it encrypted under a
passphrase: scrypt stretches the passphrase into a key, AES-256-GCM encrypts the secret
and authenticates it (a wrong passphrase or any tampering fails to decrypt, it does not
silently return garbage). Both primitives come from the already-required `cryptography`
library -- no new dependency, no hand-rolled crypto.

  ks = encrypt_secret(secret_hex, "correct horse battery staple")
  save_keystore(ks, "founder.json")
  secret_hex = decrypt_secret(load_keystore("founder.json"), passphrase)   # raises on wrong pass

Run directly ->  python keystore.py
"""
from __future__ import annotations

import base64
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

# scrypt work factors: ~16 MB / a few ms to derive. Raise N for more brute-force resistance.
_N, _R, _P = 2 ** 14, 8, 1


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode())


def _derive(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase.encode("utf-8"))


def encrypt_secret(secret_hex: str, passphrase: str) -> dict:
    secret = bytes.fromhex(secret_hex)  # validates it is hex
    if not passphrase:
        raise ValueError("passphrase must not be empty")
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive(passphrase, salt, _N, _R, _P)
    ciphertext = AESGCM(key).encrypt(nonce, secret, None)
    return {
        "version": 1,
        "kdf": "scrypt",
        "n": _N, "r": _R, "p": _P,
        "salt": _b64(salt),
        "nonce": _b64(nonce),
        "ciphertext": _b64(ciphertext),
    }


def decrypt_secret(keystore: dict, passphrase: str) -> str:
    if keystore.get("kdf") != "scrypt":
        raise ValueError("unsupported keystore format")
    key = _derive(passphrase, _unb64(keystore["salt"]), keystore["n"], keystore["r"], keystore["p"])
    try:
        secret = AESGCM(key).decrypt(_unb64(keystore["nonce"]), _unb64(keystore["ciphertext"]), None)
    except InvalidTag as exc:
        raise ValueError("wrong passphrase or corrupt keystore") from exc
    return secret.hex()


def save_keystore(keystore: dict, path: str) -> None:
    # write+replace so a crash can't leave a half-written keystore
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(keystore, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_keystore(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _demo() -> None:
    import shutil
    import tempfile

    secret = "a3f1" * 16  # 64 hex chars = a 256-bit secret
    passphrase = "correct horse battery staple"

    ks = encrypt_secret(secret, passphrase)
    # The plaintext secret never appears in the keystore bytes.
    assert secret not in json.dumps(ks)
    # Round-trips with the right passphrase.
    assert decrypt_secret(ks, passphrase) == secret

    # Wrong passphrase is rejected (authenticated encryption), not silently wrong.
    try:
        decrypt_secret(ks, "wrong passphrase")
        raise AssertionError("wrong passphrase should fail")
    except ValueError:
        pass

    # Tampering with the ciphertext is detected.
    tampered = dict(ks)
    raw = bytearray(_unb64(ks["ciphertext"]))
    raw[0] ^= 0x01
    tampered["ciphertext"] = _b64(bytes(raw))
    try:
        decrypt_secret(tampered, passphrase)
        raise AssertionError("tampered keystore should fail")
    except ValueError:
        pass

    # Two encryptions of the same secret differ (random salt + nonce).
    assert encrypt_secret(secret, passphrase)["ciphertext"] != ks["ciphertext"]

    # Survives a save/load round-trip to disk.
    tmp = tempfile.mkdtemp(prefix="mindees_ks_")
    try:
        path = os.path.join(tmp, "wallet.json")
        save_keystore(ks, path)
        assert decrypt_secret(load_keystore(path), passphrase) == secret
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("ALL CHECKS PASSED")
    print("  keystore: scrypt + AES-256-GCM, wrong-pass/tamper rejected, secret never in cleartext")


if __name__ == "__main__":
    _demo()
