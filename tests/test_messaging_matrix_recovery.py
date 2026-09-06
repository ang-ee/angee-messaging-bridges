"""Pure unit tests for the ported Matrix SSSS + cross-signing recovery round."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any

import pytest
from Crypto.Hash import HMAC, SHA256
from Crypto.Signature import eddsa
from unpaddedbase64 import decode_base64, encode_base64

from angee.messaging_integrate_matrix import recovery

USER_ID = "@ada:example.com"
DEVICE_ID = "ANGEEDEVICE"


# --- Test-side encoders that mirror what a real homeserver / client would produce ---


def _b58encode(data: bytes) -> str:
    alphabet = recovery._BASE58_ALPHABET
    number = int.from_bytes(data, "big")
    encoded = ""
    while number > 0:
        number, remainder = divmod(number, 58)
        encoded = alphabet[remainder] + encoded
    leading_zeros = len(data) - len(data.lstrip(b"\x00"))
    return alphabet[0] * leading_zeros + encoded


def _spaced(raw: str) -> str:
    return " ".join(raw[index : index + 4] for index in range(0, len(raw), 4))


def _encode_recovery_key(key: bytes, *, prefix: bytes = b"\x8b\x01", parity_xor: int = 0) -> str:
    payload = prefix + key
    parity = 0
    for byte in payload:
        parity ^= byte
    return _spaced(_b58encode(payload + bytes([parity ^ parity_xor])))


def _random_iv() -> bytes:
    iv = bytearray(os.urandom(16))
    iv[8] &= 0x7F
    return bytes(iv)


def _encrypt_secret(ssss_key: bytes, name: str, seed: bytes) -> dict[str, str]:
    aes_key, hmac_key = recovery.derive_keys(ssss_key, name)
    iv = _random_iv()
    ciphertext = recovery._aes_ctr(aes_key, iv).encrypt(encode_base64(seed).encode("ascii"))
    mac = HMAC.new(hmac_key, ciphertext, SHA256).digest()
    return {"ciphertext": encode_base64(ciphertext), "iv": encode_base64(iv), "mac": encode_base64(mac)}


# --- decode_recovery_key ---


def test_decode_recovery_key_round_trips_a_spaced_key() -> None:
    key = os.urandom(32)
    assert recovery.decode_recovery_key(_encode_recovery_key(key)) == key


def test_decode_recovery_key_rejects_a_non_base58_character() -> None:
    # '0', 'O', 'I', 'l' are excluded from the Bitcoin base58 alphabet.
    with pytest.raises(recovery.RecoveryError, match="non-base58"):
        recovery.decode_recovery_key("EsTw 0OIl")


def test_decode_recovery_key_rejects_a_bad_prefix() -> None:
    with pytest.raises(recovery.RecoveryError, match="prefix"):
        recovery.decode_recovery_key(_encode_recovery_key(os.urandom(32), prefix=b"\x8a\x01"))


def test_decode_recovery_key_rejects_a_bad_parity() -> None:
    with pytest.raises(recovery.RecoveryError, match="parity"):
        recovery.decode_recovery_key(_encode_recovery_key(os.urandom(32), parity_xor=0xFF))


def test_decode_recovery_key_rejects_a_wrong_length() -> None:
    with pytest.raises(recovery.RecoveryError, match="length"):
        recovery.decode_recovery_key(_spaced(_b58encode(os.urandom(10))))


# --- key check MAC ---


def test_key_check_mac_matches_the_same_key_and_iv() -> None:
    key = os.urandom(32)
    iv = _random_iv()
    metadata = {
        "algorithm": recovery.SSSS_ALGORITHM,
        "iv": encode_base64(iv),
        "mac": recovery.key_check_mac(key, encode_base64(iv)),
    }
    recovery.verify_secret_storage_key(key, metadata)


def test_verify_secret_storage_key_rejects_a_wrong_key() -> None:
    iv = _random_iv()
    metadata = {
        "algorithm": recovery.SSSS_ALGORITHM,
        "iv": encode_base64(iv),
        "mac": recovery.key_check_mac(os.urandom(32), encode_base64(iv)),
    }
    with pytest.raises(recovery.RecoveryError, match="does not match"):
        recovery.verify_secret_storage_key(os.urandom(32), metadata)


def test_verify_secret_storage_key_rejects_a_wrong_algorithm() -> None:
    with pytest.raises(recovery.RecoveryError, match="algorithm"):
        recovery.verify_secret_storage_key(os.urandom(32), {"algorithm": "other", "iv": "", "mac": ""})


# --- secret decryption ---


def test_decrypt_secret_round_trips_an_encrypted_seed() -> None:
    ssss_key = os.urandom(32)
    seed = os.urandom(32)
    encrypted = _encrypt_secret(ssss_key, recovery.MASTER_EVENT, seed)
    assert recovery.decrypt_secret(ssss_key, recovery.MASTER_EVENT, encrypted) == seed


def test_decrypt_secret_detects_a_tampered_mac() -> None:
    ssss_key = os.urandom(32)
    encrypted = _encrypt_secret(ssss_key, recovery.MASTER_EVENT, os.urandom(32))
    encrypted["mac"] = encode_base64(b"\x00" * 32)
    with pytest.raises(recovery.RecoveryError, match="authentication"):
        recovery.decrypt_secret(ssss_key, recovery.MASTER_EVENT, encrypted)


# --- Ed25519 signer ---


def test_signer_public_key_and_signature_verify() -> None:
    signer = recovery.Ed25519Signer(os.urandom(32))
    assert len(decode_base64(signer.public_key)) == 32
    obj = {"device_id": DEVICE_ID, "keys": {"ed25519:D": "abc"}, "signatures": {"ignored": True}}
    signature = signer.sign(obj)
    signed = {key: value for key, value in obj.items() if key not in ("signatures", "unsigned")}
    import nio

    verifier = eddsa.new(eddsa.import_public_key(decode_base64(signer.public_key)), "rfc8032")
    verifier.verify(nio.Api.to_canonical_json(signed).encode("utf-8"), decode_base64(signature))


# --- verify_with_recovery_key end to end against a fake Secret Storage API ---


class _FakeApi:
    def __init__(self, account_data: dict[str, Any], keys: dict[str, Any], upload: dict[str, Any]) -> None:
        self._account_data = account_data
        self._keys = keys
        self._upload = upload
        self.uploaded: Any = None

    async def get_account_data(self, event_type: str) -> dict[str, Any]:
        return self._account_data.get(event_type, {})

    async def query_keys(self, user_id: str) -> dict[str, Any]:
        return self._keys

    async def upload_signatures(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.uploaded = payload
        return self._upload


def _world(*, upload: dict[str, Any] | None = None) -> SimpleNamespace:
    ssss_key = os.urandom(32)
    key_id = "ssss1"
    iv = _random_iv()
    metadata = {
        "algorithm": recovery.SSSS_ALGORITHM,
        "iv": encode_base64(iv),
        "mac": recovery.key_check_mac(ssss_key, encode_base64(iv)),
    }
    seeds = {event: os.urandom(32) for event in recovery._CROSS_SIGNING_EVENTS}
    signers = {event: recovery.Ed25519Signer(seed) for event, seed in seeds.items()}
    account_data: dict[str, Any] = {
        recovery.DEFAULT_KEY_EVENT: {"key": key_id},
        recovery.KEY_EVENT_PREFIX + key_id: metadata,
    }
    for event, seed in seeds.items():
        account_data[event] = {"encrypted": {key_id: _encrypt_secret(ssss_key, event, seed)}}
    master = signers[recovery.MASTER_EVENT].public_key
    self_signing = signers[recovery.SELF_SIGNING_EVENT].public_key
    device_keys = {
        "user_id": USER_ID,
        "device_id": DEVICE_ID,
        "algorithms": ["m.olm.v1.curve25519-aes-sha2", "m.megolm.v1.aes-sha2"],
        "keys": {f"ed25519:{DEVICE_ID}": "device-ed25519", f"curve25519:{DEVICE_ID}": "device-curve25519"},
    }
    keys = {
        "master_keys": {USER_ID: {"keys": {f"ed25519:{master}": master}}},
        "self_signing_keys": {USER_ID: {"keys": {f"ed25519:{self_signing}": self_signing}}},
        "device_keys": {USER_ID: {DEVICE_ID: device_keys}},
        "failures": {},
    }
    return SimpleNamespace(
        recovery_key=_encode_recovery_key(ssss_key),
        api=_FakeApi(account_data, keys, upload or {"failures": {}}),
        signers=signers,
        keys=keys,
        self_signing_public=self_signing,
    )


def _run(api: _FakeApi, recovery_key: str) -> str:
    return asyncio.run(
        recovery.verify_with_recovery_key(api, user_id=USER_ID, device_id=DEVICE_ID, recovery_key=recovery_key)
    )


def test_verify_with_recovery_key_signs_our_device_and_returns_the_self_signing_key() -> None:
    world = _world()
    result = _run(world.api, world.recovery_key)

    assert result == world.self_signing_public
    signable = world.api.uploaded[USER_ID][DEVICE_ID]
    signatures = signable["signatures"][USER_ID]
    assert list(signatures) == [f"ed25519:{world.self_signing_public}"]
    signed = {key: value for key, value in signable.items() if key not in ("signatures", "unsigned")}
    import nio

    verifier = eddsa.new(eddsa.import_public_key(decode_base64(world.self_signing_public)), "rfc8032")
    verifier.verify(
        nio.Api.to_canonical_json(signed).encode("utf-8"),
        decode_base64(signatures[f"ed25519:{world.self_signing_public}"]),
    )


def test_verify_with_recovery_key_rejects_seeds_that_miss_the_published_keys() -> None:
    world = _world()
    world.keys["master_keys"][USER_ID]["keys"] = {"ed25519:other": "unrelated-public-key"}
    with pytest.raises(recovery.RecoveryError, match="master_keys"):
        _run(world.api, world.recovery_key)


def test_verify_with_recovery_key_requires_published_device_keys() -> None:
    world = _world()
    world.keys["device_keys"][USER_ID] = {}
    with pytest.raises(recovery.RecoveryError, match="device"):
        _run(world.api, world.recovery_key)


def test_verify_with_recovery_key_raises_on_upload_failures() -> None:
    world = _world(upload={"failures": {DEVICE_ID: {"status": 400}}})
    with pytest.raises(recovery.RecoveryError, match="failed"):
        _run(world.api, world.recovery_key)
