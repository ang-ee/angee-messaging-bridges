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
