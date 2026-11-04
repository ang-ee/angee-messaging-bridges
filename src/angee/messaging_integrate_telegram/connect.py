"""Telegram channel creation and application-key attachment."""

from __future__ import annotations

from typing import Any

from django.apps import apps
from django.db import transaction
from rebac import system_context

from angee.integrate.credentials import CredentialKind
from angee.messaging_integrate_telegram.backend import TelegramChannelBackend
from angee.messaging_integrate_telegram.backend import telegram_app_keys as telegram_app_keys

Channel = apps.get_model("messaging", "Channel")

_TELEGRAM_SLUG = TelegramChannelBackend.key


def create_telegram_channel(
    user: Any,
    *,
    name: str,
    credential: Any,
) -> Any:
    """Create a connected Telegram channel on an app-key credential, then start QR pairing.

    The credential carries an ``api_id``/``api_hash`` registered at
    my.telegram.org. Telegram's published sample id is unsuitable for bridges and
    fails with ``API_ID_PUBLISHED_FLOOD``, so the registration must be the
    operator's own. It identifies the *application*, not a phone number, and one
    registration drives any number of accounts — so channels select a shared
    credential rather than each minting a copy.
    """

    display_name = Channel.objects.normalized_name(name)
    if credential.kind != CredentialKind.APP_KEYS:
        raise ValueError("A Telegram channel requires an app-keys credential.")
    telegram_app_keys(credential)
    with system_context(reason="messaging_integrate_telegram.create"), transaction.atomic():
        channel = Channel.objects.create_disconnected(
            user,
            name=display_name,
            backend_class=_TELEGRAM_SLUG,
        )
        channel.connect(credential=credential)
    channel.resume_pairing()
    return channel
