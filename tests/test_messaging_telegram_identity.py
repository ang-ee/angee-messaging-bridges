"""Telegram audience mapping is SDK-free and distinct from the channel transport."""

from types import SimpleNamespace

import pytest

from angee.messaging_integrate_telegram.identity import parsed_export_message, parsed_message


@pytest.mark.parametrize("chat_type, public", [
    ("private_channel", False), ("private_supergroup", False),
    ("public_channel", True), ("public_supergroup", True),
])
def test_export_audience_is_independent_of_channel_transport(chat_type, public):
    parsed = parsed_export_message(
        {"id": 42, "type": chat_type, "name": "Chat"},
        {"id": 1, "from_id": "user7", "from": "Person", "date_unixtime": "1000", "text": "Hi"},
        marked_chat_id=-10042,
    )
    assert parsed.thread is not None
    assert (parsed.thread.visibility == "public") is public


@pytest.mark.parametrize("username, public", [(None, False), ("public_chat", True)])
def test_live_audience_uses_public_username_instead_of_channel_peer(username, public):
    parsed = parsed_message(SimpleNamespace(id=1), chat_id=-10042, chat=SimpleNamespace(username=username),
                            is_private=False, is_group=True, is_channel=True)
    assert parsed.thread is not None
    assert parsed.thread.modality == "group"
    assert (parsed.thread.visibility == "public") is public


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("group", [False, True])
def test_active_alternate_username_matches_export_audience(active, group):
    live = parsed_message(
        SimpleNamespace(id=1), chat_id=-10042,
        chat=SimpleNamespace(username=None, usernames=[SimpleNamespace(username="alternate", active=active)]),
        is_private=False, is_group=group, is_channel=True,
    )
    export_kind = ("public_" if active else "private_") + ("supergroup" if group else "channel")
    exported = parsed_export_message(
        {"id": 42, "type": export_kind, "name": "Chat"},
        {"id": 1, "from_id": "user7", "from": "Person", "date_unixtime": "1000", "text": "Hi"},
        marked_chat_id=-10042,
    )
    assert live.thread is not None and exported.thread is not None
    assert live.thread.visibility == exported.thread.visibility
    assert live.thread.modality == exported.thread.modality
