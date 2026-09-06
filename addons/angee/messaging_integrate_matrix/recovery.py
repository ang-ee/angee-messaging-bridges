"""Matrix recovery-key self-verification (SSSS + cross-signing), worker-only.

matrix-nio has no cross-signing or Secret Storage (SSSS): it cannot turn a pasted
recovery key into a device the user's other clients will trust. This module ports
that one round from mautrix-python's ``crypto/ssss`` and ``crypto/cross_signing.py``
(MPL-2.0) onto :mod:`pycryptodome` primitives plus :func:`nio.Api.to_canonical_json`,
so the operator-facing behaviour matches the libolm bridge it replaces: paste the
recovery key, decrypt the three ``m.cross_signing.*`` account-data secrets from
Secret Storage, prove they match the account's published cross-signing keys, then
self-sign this device with the self-signing key and upload the signature.

The module is pure crypto plus one async entry point; every network call is owned
by the injected :class:`SecretStorageApi`, so this stays SDK/aiohttp-free and
independently testable.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from typing import Any, Protocol

import nio
from Crypto.Cipher import AES
from Crypto.Hash import HMAC, SHA256
from Crypto.Protocol.KDF import HKDF
from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa
from Crypto.Util import Counter
from unpaddedbase64 import decode_base64, encode_base64

SSSS_ALGORITHM = "m.secret_storage.v1.aes-hmac-sha2"
DEFAULT_KEY_EVENT = "m.secret_storage.default_key"
KEY_EVENT_PREFIX = "m.secret_storage.key."
MASTER_EVENT = "m.cross_signing.master"
SELF_SIGNING_EVENT = "m.cross_signing.self_signing"
USER_SIGNING_EVENT = "m.cross_signing.user_signing"

_CROSS_SIGNING_EVENTS = (MASTER_EVENT, SELF_SIGNING_EVENT, USER_SIGNING_EVENT)
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_KEY_LENGTH = 35
_UNSIGNABLE = ("signatures", "unsigned")


class RecoveryError(ValueError):
    """A recovery-key round failed on syntax, authentication, or key mismatch."""


class SecretStorageApi(Protocol):
    """The three Matrix client-server calls a recovery round needs."""

    async def get_account_data(self, event_type: str) -> Mapping[str, Any]:
        """Return one ``m.*`` account-data event's content for the account."""

    async def query_keys(self, user_id: str) -> Mapping[str, Any]:
        """Return the account's published device and cross-signing keys."""

    async def upload_signatures(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Upload cross-signing signatures and return the server response body."""


def decode_recovery_key(text: str) -> bytes:
    """Decode a base58 Matrix recovery key into its 32-byte Secret Storage key."""

    stripped = "".join(text.split())
    number = 0
    for character in stripped:
        index = _BASE58_ALPHABET.find(character)
        if index < 0:
            raise RecoveryError("The recovery key contains a non-base58 character.")
        number = number * 58 + index
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeros = len(stripped) - len(stripped.lstrip("1"))
    decoded = b"\x00" * leading_zeros + body
    if len(decoded) != _KEY_LENGTH:
        raise RecoveryError("The recovery key is not the expected length.")
    if decoded[0] != 0x8B or decoded[1] != 0x01:
        raise RecoveryError("The recovery key has an unexpected prefix.")
    parity = 0
    for byte in decoded[:34]:
        parity ^= byte
    if parity != decoded[34]:
        raise RecoveryError("The recovery key failed its parity check.")
    return decoded[2:34]


def derive_keys(key: bytes, name: str = "") -> tuple[bytes, bytes]:
    """Derive the (AES, HMAC) key pair for one Secret Storage secret name."""

    aes_key, hmac_key = HKDF(
        master=key,
        key_len=32,
        salt=b"\x00" * 32,
        hashmod=SHA256,
        num_keys=2,
        context=name.encode("utf-8"),
    )
    return aes_key, hmac_key


def _aes_ctr(aes_key: bytes, iv: bytes) -> Any:
    """Return an AES-256-CTR cipher seeded from a 16-byte SSSS initial vector."""

    counter = Counter.new(128, initial_value=int.from_bytes(iv, "big"))
    return AES.new(aes_key, AES.MODE_CTR, counter=counter)


def key_check_mac(key: bytes, iv_b64: str) -> str:
    """Return the SSSS key-check MAC (unpadded base64) for a key and its IV."""

    aes_key, hmac_key = derive_keys(key)
    encrypted_zeros = _aes_ctr(aes_key, decode_base64(iv_b64)).encrypt(b"\x00" * 32)
    return encode_base64(HMAC.new(hmac_key, encrypted_zeros, SHA256).digest())


def verify_secret_storage_key(key: bytes, metadata: Mapping[str, Any]) -> None:
    """Raise unless the recovery key matches the default key's algorithm and MAC."""

    if metadata.get("algorithm") != SSSS_ALGORITHM:
        raise RecoveryError("The Secret Storage key uses an unsupported algorithm.")
    expected = key_check_mac(key, str(metadata.get("iv") or ""))
    if not hmac.compare_digest(str(metadata.get("mac") or "").rstrip("="), expected):
        raise RecoveryError("The recovery key does not match this account's Secret Storage key.")


def decrypt_secret(key: bytes, name: str, encrypted: Mapping[str, Any]) -> bytes:
    """Authenticate and CTR-decrypt one Secret Storage secret into its 32-byte seed."""

    aes_key, hmac_key = derive_keys(key, name)
    ciphertext = decode_base64(str(encrypted.get("ciphertext") or ""))
    mac = decode_base64(str(encrypted.get("mac") or ""))
    if not hmac.compare_digest(mac, HMAC.new(hmac_key, ciphertext, SHA256).digest()):
        raise RecoveryError(f"The {name} secret failed its authentication check.")
    plaintext = _aes_ctr(aes_key, decode_base64(str(encrypted.get("iv") or ""))).decrypt(ciphertext)
    return decode_base64(plaintext.decode("ascii"))


class Ed25519Signer:
    """One Ed25519 signing key reconstructed from a 32-byte cross-signing seed."""

    def __init__(self, seed: bytes) -> None:
        self._key = ECC.construct(curve="Ed25519", seed=seed)
        self.public_key = encode_base64(self._key.public_key().export_key(format="raw"))

    def sign(self, obj: Mapping[str, Any]) -> str:
        """Return the unpadded-base64 Ed25519 signature over canonical JSON."""

        signable = {key: value for key, value in obj.items() if key not in _UNSIGNABLE}
        message = nio.Api.to_canonical_json(signable).encode("utf-8")
        return encode_base64(eddsa.new(self._key, "rfc8032").sign(message))


def _require_published_public_key(
    published: Mapping[str, Any],
    field: str,
    user_id: str,
    public_key: str,
) -> None:
    """Raise unless a decrypted seed's public key is the account's published one."""

    keys = ((published.get(field) or {}).get(user_id) or {}).get("keys") or {}
    if public_key not in keys.values():
        raise RecoveryError(f"The recovered {field} key is not this account's published key.")


async def verify_with_recovery_key(
    api: SecretStorageApi,
    *,
    user_id: str,
    device_id: str,
    recovery_key: str,
) -> str:
    """Self-verify this device from a recovery key; return the self-signing public key.

    Decodes the recovery key, proves it against the account's default Secret Storage
    key, decrypts the three cross-signing seeds, confirms the master and self-signing
    seeds reproduce the account's published cross-signing keys, then signs this
    device's published keys with the self-signing key and uploads the signature.
    """

    key = decode_recovery_key(recovery_key)
    default = await api.get_account_data(DEFAULT_KEY_EVENT)
    key_id = str(default.get("key") or "")
    if not key_id:
        raise RecoveryError("This account has no default Secret Storage key.")
    metadata = await api.get_account_data(KEY_EVENT_PREFIX + key_id)
    verify_secret_storage_key(key, metadata)

    signers: dict[str, Ed25519Signer] = {}
    for event_type in _CROSS_SIGNING_EVENTS:
        content = await api.get_account_data(event_type)
        encrypted = (content.get("encrypted") or {}).get(key_id)
        if not isinstance(encrypted, Mapping):
            raise RecoveryError(f"The {event_type} secret is not encrypted for the default key.")
        signers[event_type] = Ed25519Signer(decrypt_secret(key, event_type, encrypted))

    published = await api.query_keys(user_id)
    _require_published_public_key(published, "master_keys", user_id, signers[MASTER_EVENT].public_key)
    _require_published_public_key(published, "self_signing_keys", user_id, signers[SELF_SIGNING_EVENT].public_key)

    device_keys = ((published.get("device_keys") or {}).get(user_id) or {}).get(device_id)
    if not isinstance(device_keys, Mapping):
        raise RecoveryError("This device's keys are not published; upload device keys first.")

    self_signer = signers[SELF_SIGNING_EVENT]
    signable = {key_name: value for key_name, value in device_keys.items() if key_name not in _UNSIGNABLE}
    signature = self_signer.sign(signable)
    signable["signatures"] = {user_id: {f"ed25519:{self_signer.public_key}": signature}}
    result = await api.upload_signatures({user_id: {device_id: signable}})
    failures = result.get("failures") or {}
    if failures:
        raise RecoveryError(f"The cross-signing signature upload failed: {failures}.")
    return self_signer.public_key
