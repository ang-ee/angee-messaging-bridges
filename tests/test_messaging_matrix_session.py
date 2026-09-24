"""Matrix worker tests using the optional matrix-nio and crypto SDKs."""

from __future__ import annotations

# The worker imports follow the explicit optional-SDK checks.
# ruff: noqa: E402
import asyncio
import importlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import pytest

pytest.importorskip("nio", reason="Matrix sessions require the optional matrix-nio SDK")
pytest.importorskip("Crypto", reason="Matrix sessions require optional pycryptodome")
pytest.importorskip("unpaddedbase64", reason="Matrix sessions require optional unpaddedbase64")

import nio
from Crypto.Hash import HMAC, SHA256
from Crypto.Signature import eddsa
from rebac import system_context
from unpaddedbase64 import decode_base64, encode_base64

from angee.integrate.live import PairingState, session_store_path
from angee.integrate.locks import bridge_advisory_lock
from angee.integrate.sync import BridgeProgressReporter
from angee.messaging.connect import skip_channel_password, submit_channel_password
from angee.messaging_integrate_matrix import recovery
from angee.messaging_integrate_matrix.identity import MatrixMediaFact
from tests.conftest import Credential
from tests.messaging_fixtures import Message
from tests.messaging_graphql_fixtures import (
    Channel,
    _platform_admin,
)
from tests.test_messaging_matrix import _event, _matrix_channel
from tests.test_messaging_matrix import matrix_tables as matrix_tables


def test_matrix_real_crypto_store_round_trips_the_vodozemac_account(tmp_path: Path) -> None:
    """nio's peewee ``DefaultStore`` persists and reloads a vodozemac Olm account."""

    from nio.crypto import OlmAccount
    from nio.store import DefaultStore

    store = DefaultStore("@ada:example.com", "ANGEEDEVICE", str(tmp_path), "pickle-key", "crypto.db")
    account = OlmAccount()
    store.save_account(account)
    assert (tmp_path / "crypto.db").exists()

    reopened = DefaultStore("@ada:example.com", "ANGEEDEVICE", str(tmp_path), "pickle-key", "crypto.db")
    loaded = reopened.load_account()
    assert loaded is not None
    assert loaded.identity_keys == account.identity_keys


def _b58encode(data: bytes) -> str:
    """Encode bytes with the same base58 alphabet the recovery module decodes."""

    alphabet = recovery._BASE58_ALPHABET
    number = int.from_bytes(data, "big")
    encoded = ""
    while number > 0:
        number, remainder = divmod(number, 58)
        encoded = alphabet[remainder] + encoded
    leading_zeros = len(data) - len(data.lstrip(b"\x00"))
    return alphabet[0] * leading_zeros + encoded


def _encode_recovery_key(key: bytes) -> str:
    """Encode a 32-byte SSSS key as a spaced base58 Matrix recovery key."""

    payload = bytes([0x8B, 0x01]) + key
    parity = 0
    for byte in payload:
        parity ^= byte
    raw = _b58encode(payload + bytes([parity]))
    return " ".join(raw[index : index + 4] for index in range(0, len(raw), 4))


def _random_iv() -> bytes:
    """Return a 16-byte SSSS IV with the top counter bit cleared."""

    iv = bytearray(os.urandom(16))
    iv[8] &= 0x7F
    return bytes(iv)


def _encrypt_secret(ssss_key: bytes, name: str, seed: bytes) -> dict[str, str]:
    """Encrypt one 32-byte cross-signing seed exactly as Secret Storage would."""

    aes_key, hmac_key = recovery.derive_keys(ssss_key, name)
    iv = _random_iv()
    plaintext = encode_base64(seed).encode("ascii")
    ciphertext = recovery._aes_ctr(aes_key, iv).encrypt(plaintext)
    mac = HMAC.new(hmac_key, ciphertext, SHA256).digest()
    return {"ciphertext": encode_base64(ciphertext), "iv": encode_base64(iv), "mac": encode_base64(mac)}


def _device_keys(client: Any, user_id: str) -> dict[str, Any]:
    """Build the account's real, self-signed device keys from its vodozemac account."""

    identity = client.olm.account.identity_keys
    device_id = client.device_id
    keys: dict[str, Any] = {
        "user_id": user_id,
        "device_id": device_id,
        "algorithms": ["m.olm.v1.curve25519-aes-sha2", "m.megolm.v1.aes-sha2"],
        "keys": {
            f"curve25519:{device_id}": identity["curve25519"],
            f"ed25519:{device_id}": identity["ed25519"],
        },
    }
    keys["signatures"] = {user_id: {f"ed25519:{device_id}": client.olm.sign_json(dict(keys))}}
    return keys


class _RecoveryFixture:
    """One account's Secret Storage + cross-signing state for a recovery round."""

    def __init__(self) -> None:
        self.ssss_key = os.urandom(32)
        self.recovery_key = _encode_recovery_key(self.ssss_key)
        self.key_id = "angee_ssss_key"
        iv = _random_iv()
        self.metadata = {
            "algorithm": recovery.SSSS_ALGORITHM,
            "iv": encode_base64(iv),
            "mac": recovery.key_check_mac(self.ssss_key, encode_base64(iv)),
        }
        self.seeds = {event: os.urandom(32) for event in recovery._CROSS_SIGNING_EVENTS}
        self.signers = {event: recovery.Ed25519Signer(seed) for event, seed in self.seeds.items()}
        self.encrypted = {event: _encrypt_secret(self.ssss_key, event, seed) for event, seed in self.seeds.items()}

    def account_data(self, event_type: str) -> dict[str, Any]:
        """Serve default-key, key-metadata, and encrypted cross-signing account data."""

        if event_type == recovery.DEFAULT_KEY_EVENT:
            return {"key": self.key_id}
        if event_type == recovery.KEY_EVENT_PREFIX + self.key_id:
            return dict(self.metadata)
        if event_type in self.encrypted:
            return {"encrypted": {self.key_id: dict(self.encrypted[event_type])}}
        return {}

    def query_keys(self, client: Any, user_id: str) -> dict[str, Any]:
        """Serve published cross-signing keys and this device's real signed keys."""

        master = self.signers[recovery.MASTER_EVENT].public_key
        self_signing = self.signers[recovery.SELF_SIGNING_EVENT].public_key
        return {
            "master_keys": {user_id: {"keys": {f"ed25519:{master}": master}}},
            "self_signing_keys": {user_id: {"keys": {f"ed25519:{self_signing}": self_signing}}},
            "device_keys": {user_id: {client.device_id: _device_keys(client, user_id)}},
            "failures": {},
        }


class _FakeResponse:
    """A minimal aiohttp-style response for the raw ``send`` transport path."""

    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._payload = payload

    async def json(self, **_kwargs: Any) -> Any:
        return self._payload

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


class _FakeMatrixClient(nio.AsyncClient):
    """The real nio client with its two transport choke points canned.

    ``_send`` routes nio's parsed client-server calls to fixture dicts and runs the
    real ``receive_response`` (real vodozemac account, real peewee store, real event
    parsing). ``send`` answers the raw account-data / key-query / signature-upload
    calls the recovery module makes.
    """

    instances: list[_FakeMatrixClient] = []
    login_error: str | None = None
    whoami_error: str | None = None
    sync_error: str | None = None
    sync_events: list[dict[str, Any]] = []
    history_events: list[dict[str, Any]] = []
    downloads: dict[str, bytes] = {}
    recovery_fixture: _RecoveryFixture | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.login_calls: list[dict[str, Any]] = []
        self.sync_calls: list[dict[str, Any]] = []
        self.history_calls: list[dict[str, Any]] = []
        self.download_calls: list[str] = []
        self.signature_uploads: list[dict[str, Any]] = []
        type(self).instances.append(self)

    @staticmethod
    def _error(errcode: str) -> dict[str, str]:
        return {"errcode": errcode, "error": errcode}

    async def _send(  # type: ignore[override]
        self,
        response_class: type,
        method: str,
        path: str,
        data: Any = None,
        response_data: tuple[Any, ...] | None = None,
        content_type: str | None = None,
        trace_context: Any = None,
        data_provider: Any = None,
        timeout: float | None = None,
        content_length: int | None = None,
        save_to: Any = None,
    ) -> Any:
        parsed = urlparse(path)
        query = {key: value[0] for key, value in parse_qs(parsed.query).items()}
        body = json.loads(data) if isinstance(data, str) and data else {}
        if "/login" in parsed.path:
            self.login_calls.append(body)
            payload = (
                self._error(self.login_error)
                if self.login_error
                else {
                    "user_id": "@ada:example.com",
                    "device_id": "ANGEEDEVICE",
                    "access_token": "access-token",
                }
            )
        elif "/account/whoami" in parsed.path:
            payload = (
                self._error(self.whoami_error)
                if self.whoami_error
                else {
                    "user_id": self.user_id or "@ada:example.com",
                    "device_id": self.device_id or "ANGEEDEVICE",
                }
            )
        elif "/keys/upload" in parsed.path:
            payload = {"one_time_key_counts": {"curve25519": 0, "signed_curve25519": 50}}
        elif "/keys/query" in parsed.path:
            payload = {"device_keys": {}, "failures": {}}
        elif "/sync" in parsed.path:
            index = len(self.sync_calls)
            self.sync_calls.append(query)
            if self.sync_error:
                payload = self._error(self.sync_error)
            elif index == 0:
                payload = {
                    "next_batch": "s1",
                    "rooms": {
                        "join": {
                            "!room:example.com": {
                                "state": {
                                    "events": [
                                        {
                                            "type": "m.room.name",
                                            "state_key": "",
                                            "sender": "@ada:example.com",
                                            "event_id": "$name",
                                            "origin_server_ts": 1,
                                            "content": {"name": "Matrix Room"},
                                        }
                                    ]
                                },
                                "timeline": {
                                    "prev_batch": "t0",
                                    "events": list(type(self).sync_events),
                                },
                            }
                        }
                    },
                }
            else:
                await asyncio.sleep(0.02)
                payload = {"next_batch": "s1", "rooms": {"join": {}}}
        elif "/messages" in parsed.path:
            room_id = unquote(parsed.path.split("/rooms/", 1)[1].split("/", 1)[0])
            self.history_calls.append({"room_id": room_id, "limit": int(query.get("limit", "0"))})
            payload = {"chunk": list(type(self).history_events), "start": "t0", "end": "t1"}
        elif "/media/download/" in parsed.path:
            server, media_id = parsed.path.split("/media/download/", 1)[1].split("/", 1)
            mxc = f"mxc://{server}/{media_id}"
            self.download_calls.append(mxc)
            resp = nio.MemoryDownloadResponse.from_data(type(self).downloads[mxc], "application/octet-stream", None)
            await self.receive_response(resp)
            return resp
        else:
            raise AssertionError(f"Unrouted Matrix path: {path}")
        response = response_class.from_dict(payload, *(response_data or ()))
        await self.receive_response(response)
        return response

    async def send(  # type: ignore[override]
        self,
        method: str,
        path: str,
        data: Any = None,
        headers: dict[str, str] | None = None,
        trace_context: Any = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        body = json.loads(data) if isinstance(data, str) and data else {}
        fixture = type(self).recovery_fixture
        assert fixture is not None
        if "/account_data/" in path:
            return _FakeResponse(200, fixture.account_data(unquote(path.rsplit("/account_data/", 1)[1])))
        if "/keys/query" in path:
            return _FakeResponse(200, fixture.query_keys(self, self.user_id))
        if "/keys/signatures/upload" in path:
            self.signature_uploads.append(body)
            return _FakeResponse(200, {"failures": {}})
        raise AssertionError(f"Unrouted raw Matrix path: {path}")


@pytest.fixture
def matrix_session_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Reset the transport stub and bind it to the real worker session module."""

    _FakeMatrixClient.instances.clear()
    _FakeMatrixClient.login_error = None
    _FakeMatrixClient.whoami_error = None
    _FakeMatrixClient.sync_error = None
    _FakeMatrixClient.sync_events = []
    _FakeMatrixClient.history_events = []
    _FakeMatrixClient.downloads = {}
    _FakeMatrixClient.recovery_fixture = None
    module = importlib.import_module("angee.messaging_integrate_matrix.session")
    monkeypatch.setattr(module.MatrixSession, "client_class", _FakeMatrixClient)
    return module


def _session_facts(channel: Any) -> dict[str, Any]:
    """Read the persisted worker session facts, or an empty envelope."""

    path = session_store_path(channel) / "session.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _wait_until(predicate: Any, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


@pytest.mark.django_db(transaction=True)
def test_matrix_password_login_recovery_secret_sync_and_bounded_backfill(
    matrix_tables: Any,
    matrix_session_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full advisory-locked run logs in, self-verifies, syncs, backfills, and ingests."""

    from angee.integrate import session as live_session_module

    monkeypatch.setattr(live_session_module, "AWAITING_PASSWORD_WAKE_SECONDS", 0.01)
    admin = _platform_admin("msg-matrix-session-admin")
    channel = _matrix_channel(admin)
    _FakeMatrixClient.sync_events = [_event("$live")]
    _FakeMatrixClient.history_events = [_event("$history")]
    fixture = _RecoveryFixture()
    _FakeMatrixClient.recovery_fixture = fixture
    stop_event = threading.Event()
    session = matrix_session_module.MatrixSession(
        channel,
        reporter=BridgeProgressReporter(channel),
        stop_event=stop_event,
    )
    failures: list[BaseException] = []

    def operate() -> None:
        try:
            _wait_until(lambda: session.pairing is PairingState.AWAITING_PASSWORD)
            with system_context(reason="test.messaging.matrix.recovery.submit"):
                operator_channel = Channel.objects.get(pk=channel.pk)
                submit_channel_password(operator_channel, fixture.recovery_key)
            _wait_until(
                lambda: (
                    Message._base_manager.filter(channel_id=channel.pk).count() == 2
                    and bool(Channel._base_manager.get(pk=channel.pk).subscription_state.get("history_seeded"))
                )
            )
            _wait_until(lambda: _session_facts(channel).get("next_batch") == "s1")
            stop_event.set()
        except BaseException as error:  # noqa: BLE001 — surface operator-thread failures.
            failures.append(error)
            stop_event.set()

    operator = threading.Thread(target=operate, daemon=True)
    with system_context(reason="test.messaging.matrix.session.run"), bridge_advisory_lock(channel) as acquired:
        assert acquired
        operator.start()
        outcome = session.run()
        operator.join(timeout=3)
        assert not operator.is_alive()

    assert failures == []
    assert outcome is PairingState.PAIRED
    client = _FakeMatrixClient.instances[-1]
    assert client.login_calls and client.login_calls[0]["password"] == "durable-login-password"
    assert len(client.history_calls) == 1
    assert client.history_calls[0]["limit"] == matrix_session_module.INITIAL_CONVERSATION_LIMIT
    assert len(_FakeMatrixClient.history_events) <= matrix_session_module.INITIAL_HISTORY_LIMIT
    assert {message.external_id for message in Message._base_manager.filter(channel_id=channel.pk)} == {
        "!room:example.com/$live",
        "!room:example.com/$history",
    }

    # The recovery round self-signed our own device with the self-signing key.
    assert client.signature_uploads
    upload = client.signature_uploads[-1]
    signable = upload["@ada:example.com"]["ANGEEDEVICE"]
    self_signing_public = fixture.signers[recovery.SELF_SIGNING_EVENT].public_key
    signatures = signable["signatures"]["@ada:example.com"]
    assert list(signatures) == [f"ed25519:{self_signing_public}"]
    signed = {key: value for key, value in signable.items() if key not in ("signatures", "unsigned")}
    verifier = eddsa.new(eddsa.import_public_key(decode_base64(self_signing_public)), "rfc8032")
    verifier.verify(
        nio.Api.to_canonical_json(signed).encode("utf-8"), decode_base64(signatures[f"ed25519:{self_signing_public}"])
    )

    facts = _session_facts(channel)
    assert facts["recovery"] == "verified"
    assert facts["next_batch"] == "s1"
    assert (session_store_path(channel) / "crypto.db").exists()
    with system_context(reason="test.messaging.matrix.session.material.verify"):
        material = Credential.objects.get(pk=channel.credential_id).reveal()
        assert material["password"] == "durable-login-password"
        assert "recovery_key" not in material


@pytest.mark.django_db(transaction=True)
def test_matrix_recovery_skip_continues_forward_only_and_history_gate_holds(
    matrix_tables: Any,
    matrix_session_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skipping recovery still pairs and a seeded channel performs no backfill."""

    from angee.integrate import session as live_session_module

    monkeypatch.setattr(live_session_module, "AWAITING_PASSWORD_WAKE_SECONDS", 0.01)
    admin = _platform_admin("msg-matrix-skip-admin")
    channel = _matrix_channel(admin, history_seeded=True)
    stop_event = threading.Event()
    session = matrix_session_module.MatrixSession(
        channel,
        reporter=BridgeProgressReporter(channel),
        stop_event=stop_event,
    )
    failures: list[BaseException] = []

    def operate() -> None:
        try:
            _wait_until(lambda: session.pairing is PairingState.AWAITING_PASSWORD)
            with system_context(reason="test.messaging.matrix.recovery.skip"):
                operator_channel = Channel.objects.get(pk=channel.pk)
                assert operator_channel.live_impl.pairing().can_skip is True
                skip_channel_password(operator_channel)
            _wait_until(lambda: session.pairing is PairingState.PAIRED)
            stop_event.set()
        except BaseException as error:  # noqa: BLE001 — surface operator-thread failures.
            failures.append(error)
            stop_event.set()

    operator = threading.Thread(target=operate, daemon=True)
    with system_context(reason="test.messaging.matrix.skip.run"), bridge_advisory_lock(channel) as acquired:
        assert acquired
        operator.start()
        outcome = session.run()
        operator.join(timeout=3)

    assert failures == []
    assert outcome is PairingState.PAIRED
    assert _FakeMatrixClient.instances[-1].history_calls == []
    assert _session_facts(channel)["recovery"] == "skipped"
    with system_context(reason="test.messaging.matrix.skip.material.verify"):
        material = Credential.objects.get(pk=channel.credential_id).reveal()
        assert material["password"] == "durable-login-password"
        assert "recovery_key" not in material


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("session_facts", "error_attr", "errcode"),
    [
        ({}, "login_error", "M_FORBIDDEN"),
        (
            {
                "user_id": "@ada:example.com",
                "device_id": "ANGEEDEVICE",
                "access_token": "expired-token",
            },
            "whoami_error",
            "M_UNKNOWN_TOKEN",
        ),
    ],
)
def test_matrix_auth_failures_report_logged_out(
    matrix_tables: Any,
    matrix_session_module: Any,
    session_facts: dict[str, str],
    error_attr: str,
    errcode: str,
) -> None:
    """Password rejection and revoked access tokens use generic logged-out state."""

    admin = _platform_admin(f"msg-matrix-{errcode.lower()}-admin")
    channel = _matrix_channel(admin)
    path = session_store_path(channel) / "session.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if session_facts:
        matrix_session_module._write_session_facts(path, session_facts)
    setattr(_FakeMatrixClient, error_attr, errcode)
    session = matrix_session_module.MatrixSession(
        channel,
        reporter=BridgeProgressReporter(channel),
        stop_event=threading.Event(),
    )
    session.client = session._build_client(path)

    session._connect()

    kinds = [session.events.get_nowait()[0] for _ in range(session.events.qsize())]
    assert kinds == ["logged_out", "disconnected"]


@pytest.mark.django_db(transaction=True)
def test_matrix_mid_session_forbidden_is_retriable_not_logged_out(
    matrix_tables: Any,
    matrix_session_module: Any,
) -> None:
    """A non-auth M_FORBIDDEN retains the crypto store and surfaces a session error."""

    admin = _platform_admin("msg-matrix-mid-forbidden-admin")
    channel = _matrix_channel(admin)
    path = session_store_path(channel) / "session.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    matrix_session_module._write_session_facts(
        path,
        {
            "user_id": "@ada:example.com",
            "device_id": "ANGEEDEVICE",
            "access_token": "access-token",
            "pickle_key": "pickle-key",
            "recovery": "skipped",
        },
    )
    _FakeMatrixClient.sync_error = "M_FORBIDDEN"
    session = matrix_session_module.MatrixSession(
        channel,
        reporter=BridgeProgressReporter(channel),
        stop_event=threading.Event(),
    )
    session.client = session._build_client(path)

    session._connect()

    kinds = [session.events.get_nowait()[0] for _ in range(session.events.qsize())]
    assert kinds == ["paired", "disconnected"]
    assert isinstance(session.outcome_error, matrix_session_module.MatrixApiError)
    assert session.outcome_error.errcode == "M_FORBIDDEN"


@pytest.mark.django_db(transaction=True)
def test_matrix_undecryptable_event_is_counted_and_skipped(
    matrix_tables: Any,
    matrix_session_module: Any,
) -> None:
    """A missing Megolm session leaves a MegolmEvent that is counted and skipped."""

    admin = _platform_admin("msg-matrix-undecryptable-admin")
    channel = _matrix_channel(admin)
    session = matrix_session_module.MatrixSession(
        channel,
        reporter=BridgeProgressReporter(channel),
        stop_event=threading.Event(),
    )
    megolm = nio.Event.parse_event(
        {
            "type": "m.room.encrypted",
            "event_id": "$encrypted",
            "sender": "@grace:example.com",
            "origin_server_ts": 1_768_800_000_000,
            "content": {
                "algorithm": "m.megolm.v1.aes-sha2",
                "sender_key": "sender-curve25519-key",
                "session_id": "megolm-session-id",
                "ciphertext": "AwgAEnB2x+undecryptable",
                "device_id": "GRACEDEVICE",
            },
        }
    )
    assert isinstance(megolm, nio.MegolmEvent)

    assert session._queued_event(megolm, room_id="!room:example.com") is None
    assert session._undecryptable_events == 1


@pytest.mark.django_db(transaction=True)
def test_matrix_download_authenticates_and_decrypts_encrypted_attachment(
    matrix_tables: Any,
    matrix_session_module: Any,
) -> None:
    """Media resolution stays on the owning Matrix loop and decrypts after download."""

    admin = _platform_admin("msg-matrix-media-admin")
    channel = _matrix_channel(admin)
    session = matrix_session_module.MatrixSession(
        channel,
        reporter=BridgeProgressReporter(channel),
        stop_event=threading.Event(),
    )
    path = session_store_path(channel) / "session.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    matrix_session_module._write_session_facts(
        path,
        {
            "user_id": "@ada:example.com",
            "device_id": "ANGEEDEVICE",
            "access_token": "media-token",
            "pickle_key": "pickle-key",
        },
    )
    session.client = session._build_client(path)
    ciphertext, keys = nio.crypto.attachments.encrypt_attachment(b"secret-plaintext")
    _FakeMatrixClient.downloads = {"mxc://example.com/encrypted": bytes(ciphertext)}
    fact = MatrixMediaFact(
        url="mxc://example.com/encrypted",
        key=keys["key"]["k"],
        hash=keys["hashes"]["sha256"],
        iv=keys["iv"],
    )
    loop = asyncio.new_event_loop()
    session._loop = loop
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        content = session._download(None, fact)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=1)
        loop.close()

    assert content == b"secret-plaintext"
    assert session.client.download_calls == ["mxc://example.com/encrypted"]
