"""SDK-independent Matrix identity, connection and console contracts."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
from django.apps import apps
from django.core.management import call_command
from django.db import connection
from rebac import system_context

from angee.graphql.schema import SCHEMA_PART_KEYS, GraphQLSchemas
from angee.integrate.credentials import CredentialKind
from angee.messaging_integrate_matrix.backend import MatrixChannelBackend
from angee.messaging_integrate_matrix.constants import SESSION_QUEUE
from angee.messaging_integrate_matrix.identity import MatrixMediaFact, parsed_message
from tests.conftest import (
    SchemaAddon,
    Vendor,
    _clear_model_tables,
    _create_missing_tables,
    execute_schema,
    result_data,
)
from tests.messaging_fixtures import MESSAGING_TEST_MODELS
from tests.messaging_graphql_fixtures import (
    Channel,
    _platform_admin,
    _request,
    iam_schema,
    integrate_schema,
    messaging_schema,
    parties_schema,
)

Credential = apps.get_model("integrate", "Credential")
MATRIX_TEST_MODELS = (*MESSAGING_TEST_MODELS, Channel)


def _event(
    event_id: str = "$event",
    *,
    room_id: str = "!room:example.com",
    sender: str = "@grace:example.com",
    msgtype: str = "m.text",
    body: str = "Hello from Matrix",
    relation: dict[str, Any] | None = None,
    **content: Any,
) -> dict[str, Any]:
    """Return one ordinary Matrix event-dict fixture."""

    return {
        "type": "m.room.message",
        "room_id": room_id,
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": 1_768_800_000_000,
        "content": {
            "msgtype": msgtype,
            "body": body,
            **({"m.relates_to": relation} if relation is not None else {}),
            **content,
        },
    }


def test_matrix_identity_scopes_events_and_replies_to_the_room() -> None:
    """Room, sender, message, and reply identities use stable Matrix ids."""

    wire = _event(
        "$reply",
        relation={"m.in_reply_to": {"event_id": "$parent"}},
    )
    parsed = parsed_message(wire, room_name="Angee", own_user_id="@ada:example.com")

    assert parsed is not None
    assert parsed.external_id == "!room:example.com/$reply"
    assert parsed.platform == "matrix"
    assert parsed.direction == "inbound"
    assert parsed.in_reply_to == "!room:example.com/$parent"
    assert parsed.thread is not None
    assert parsed.thread.external_id == "!room:example.com"
    assert parsed.thread.title == "Angee"
    assert parsed.sender is not None
    assert parsed.sender.platform == "matrix"
    assert parsed.sender.external_id == "@grace:example.com"
    assert parsed.body is not None and parsed.body.text == "Hello from Matrix"


@pytest.mark.parametrize("msgtype", ["m.text", "m.notice", "m.emote"])
def test_matrix_text_message_types_map_to_text(msgtype: str) -> None:
    """The three v1 text-like msgtypes share the neutral text body."""

    parsed = parsed_message(_event(msgtype=msgtype))
    assert parsed is not None
    assert parsed.body is not None and parsed.body.text == "Hello from Matrix"


@pytest.mark.parametrize("msgtype", ["m.image", "m.file", "m.audio", "m.video"])
def test_matrix_media_types_expose_download_and_encryption_facts(msgtype: str) -> None:
    """Plain and encrypted media stay as facts until the vendor loop downloads them."""

    parsed = parsed_message(
        _event(
            msgtype=msgtype,
            body="asset.bin",
            file={
                "url": "mxc://example.com/encrypted",
                "key": {"k": "key"},
                "hashes": {"sha256": "hash"},
                "iv": "iv",
            },
            info={"mimetype": "application/octet-stream"},
        )
    )
    assert parsed is not None
    assert parsed.metadata["_media_facts"] == (
        MatrixMediaFact(
            url="mxc://example.com/encrypted",
            mime="application/octet-stream",
            name="asset.bin",
            key="key",
            hash="hash",
            iv="iv",
        ),
    )


@pytest.mark.parametrize(
    "wire",
    [
        {**_event(), "state_key": ""},
        {**_event(), "type": "m.reaction"},
        {**_event(), "type": "m.room.redaction"},
        _event(msgtype="m.location"),
        _event(relation={"rel_type": "m.replace", "event_id": "$old"}),
    ],
)
def test_matrix_v1_skip_list_ignores_non_messages_and_edits(wire: dict[str, Any]) -> None:
    """Unsupported Matrix event shapes never reach the neutral ingest seam."""

    assert parsed_message(wire) is None


def test_matrix_backend_declares_worker_and_transient_material_contracts() -> None:
    """Console imports see only the dotted worker boundary and recovery-key reset policy."""

    assert SESSION_QUEUE == "matrix"
    assert MatrixChannelBackend.key == "matrix"
    assert MatrixChannelBackend.session_class == ("angee.messaging_integrate_matrix.session.MatrixSession")
    assert MatrixChannelBackend.transient_material_keys == ("recovery_key",)


@pytest.fixture
def matrix_tables(tmp_path: Path, settings: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Create concrete messaging tables and isolate Matrix session storage."""

    settings.ANGEE_DATA_DIR = str(tmp_path / "data")
    monkeypatch.setattr("angee.integrate.impl.enqueue_task", lambda *args, **kwargs: None)
    created_models = _create_missing_tables(MATRIX_TEST_MODELS)
    call_command("rebac", "sync", verbosity=0)
    try:
        yield
    finally:
        _clear_model_tables(MATRIX_TEST_MODELS)
        if created_models:
            with connection.schema_editor() as schema_editor:
                for model in reversed(created_models):
                    schema_editor.delete_model(model)


def _matrix_channel(user: Any, *, history_seeded: bool = False) -> Any:
    connect = importlib.import_module("angee.messaging_integrate_matrix.connect")
    with system_context(reason="test.messaging.matrix.vendor.seed"):
        Vendor.objects.get_or_create(slug="matrix", defaults={"display_name": "Matrix"})
    channel = connect.create_matrix_channel(
        user,
        "https://8.8.8.8/",
        "@ada:example.com",
        "durable-login-password",
    )
    if history_seeded:
        with system_context(reason="test.messaging.matrix.history.seed"):
            channel.merge_subscription_state(history_seeded=True)
    return channel


@pytest.mark.django_db(transaction=True)
def test_create_matrix_channel_validates_and_starts_basic_auth(
    matrix_tables: Any,
) -> None:
    """Connect persists the normalized homeserver and selected durable credential."""

    admin = _platform_admin("msg-matrix-connect-admin")
    channel = _matrix_channel(admin)

    with system_context(reason="test.messaging.matrix.connect.verify"):
        channel.refresh_from_db()
        assert channel.vendor.slug == "matrix"
        assert channel.backend_class == "matrix"
        assert channel.display_name == "@ada:example.com"
        assert channel.lifecycle == "connected"
        assert channel.subscription_state["homeserver"] == "https://8.8.8.8"
        assert channel.subscription_state["desired"] == Channel.LiveState.LIVE
        assert channel.credential.kind == CredentialKind.BASIC_AUTH


@pytest.mark.django_db(transaction=True)
def test_create_matrix_channel_rejects_non_http_homeserver_before_persisting(
    matrix_tables: Any,
) -> None:
    """A malformed homeserver fails before a Matrix channel row is created."""

    connect = importlib.import_module("angee.messaging_integrate_matrix.connect")
    admin = _platform_admin("msg-matrix-invalid-url-admin")
    with system_context(reason="test.messaging.matrix.invalid_url.seed"):
        Vendor.objects.create(slug="matrix", display_name="Matrix")
    with pytest.raises(ValueError, match="valid Matrix homeserver URL"):
        connect.create_matrix_channel(
            admin,
            "matrix.example.com",
            "@ada:example.com",
            "durable-login-password",
        )

    assert Channel._base_manager.filter(backend_class="matrix").count() == 0
    assert Credential._base_manager.filter(user=admin, name="Matrix - @ada:example.com").count() == 0


@pytest.mark.parametrize("homeserver", ["http://169.254.169.254", "http://224.0.0.1", "http://0.0.0.0"])
def test_matrix_homeserver_rejects_ssrf_escapes(homeserver: str) -> None:
    """Metadata/link-local/multicast targets are refused even for a self-hosted verb."""

    connect = importlib.import_module("angee.messaging_integrate_matrix.connect")

    with pytest.raises(ValueError, match="metadata, link-local, or multicast"):
        connect.matrix_homeserver_url(homeserver)


@pytest.mark.parametrize("homeserver", ["http://127.0.0.1:8008", "http://192.168.1.10", "http://10.0.0.5:8448"])
def test_matrix_homeserver_allows_self_hosted_private_addresses(homeserver: str) -> None:
    """Self-hosted homeservers on private/loopback networks are permitted (allow_private)."""

    connect = importlib.import_module("angee.messaging_integrate_matrix.connect")

    assert connect.matrix_homeserver_url(homeserver) == homeserver


@pytest.mark.django_db(transaction=True)
def test_create_matrix_channel_reuses_named_credential_on_retry(matrix_tables: Any) -> None:
    """A repeated connect reuses the credential row instead of deadlocking on its name."""

    connect = importlib.import_module("angee.messaging_integrate_matrix.connect")
    admin = _platform_admin("msg-matrix-retry-admin")
    with system_context(reason="test.messaging.matrix.retry.seed"):
        Vendor.objects.create(slug="matrix", display_name="Matrix")

    first = connect.create_matrix_channel(
        admin,
        "https://8.8.8.8/",
        "@ada:example.com",
        "first-password",
    )
    second = connect.create_matrix_channel(
        admin,
        "https://8.8.8.8/",
        "@ada:example.com",
        "second-password",
    )

    credentials = Credential._base_manager.filter(user=admin, name="Matrix - @ada:example.com")
    assert credentials.count() == 1
    assert first.credential_id == second.credential_id == credentials.get().pk
    with system_context(reason="test.messaging.matrix.retry.verify"):
        assert credentials.get().reveal()["password"] == "second-password"


@pytest.mark.django_db(transaction=True)
def test_connect_matrix_channel_mutation_dispatches_to_the_service(matrix_tables: Any) -> None:
    """The Matrix mutation selects a credential and returns the shared Channel."""

    admin = _platform_admin("msg-matrix-graphql-admin")
    with system_context(reason="test.messaging.matrix.graphql.seed"):
        Vendor.objects.create(slug="matrix", display_name="Matrix")
    matrix_schema = importlib.import_module("angee.messaging_integrate_matrix.schema")
    addons = [
        SchemaAddon({"console": {key: tuple(module.schemas["console"].get(key, ())) for key in SCHEMA_PART_KEYS}})
        for module in (iam_schema, integrate_schema, parties_schema, messaging_schema, matrix_schema)
    ]
    schema = GraphQLSchemas(addons).build("console")

    result = execute_schema(
        schema,
        """
        mutation ConnectMatrix($homeserver: String!, $username: String!, $password: String!) {
          connect_matrix_channel(homeserver: $homeserver, username: $username, password: $password) {
            id
            display_name
            backend_class
            lifecycle
          }
        }
        """,
        {
            "homeserver": "https://8.8.8.8/",
            "username": "@ada:example.com",
            "password": "durable-login-password",
        },
        request=_request(admin),
    )

    assert result_data(result)["connect_matrix_channel"] == {
        "id": result_data(result)["connect_matrix_channel"]["id"],
        "display_name": "@ada:example.com",
        "backend_class": "MATRIX",
        "lifecycle": "CONNECTED",
    }


@pytest.mark.django_db(transaction=True)
def test_matrix_reset_wipes_only_recovery_key(
    matrix_tables: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backend declaration preserves the durable BASIC_AUTH password on reset."""

    from angee.messaging import connect as messaging_connect

    admin = _platform_admin("msg-matrix-reset-admin")
    channel = _matrix_channel(admin)
    channel.credential.update_material(recovery_key="transient-recovery-key")
    monkeypatch.setattr(messaging_connect, "await_session_exit", lambda _channel: None)
    monkeypatch.setattr(messaging_connect, "reset_session_store", lambda _channel: None)
    monkeypatch.setattr(messaging_connect, "resume_channel_pairing", lambda _channel: None)

    messaging_connect.reset_channel_pairing(channel)

    with system_context(reason="test.messaging.matrix.reset.verify"):
        material = Credential.objects.get(pk=channel.credential_id).reveal()
        assert material == {
            "username": "@ada:example.com",
            "password": "durable-login-password",
        }
