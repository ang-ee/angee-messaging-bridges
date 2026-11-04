"""Discord bot-token channel creation and credential attachment."""

from __future__ import annotations

from typing import Any

from django.apps import apps
from django.db import transaction
from django.views.decorators.debug import sensitive_variables
from rebac import system_context

from angee.integrate.credentials import CredentialKind
from angee.messaging_integrate_discord.backend import DiscordChannelBackend
from angee.messaging_integrate_discord.backend import discord_bot_token as discord_bot_token

Channel = apps.get_model("messaging", "Channel")
Credential = apps.get_model("integrate", "Credential")


@sensitive_variables("token")
def create_discord_channel(user: Any, name: str, token: str) -> Any:
    """Atomically persist one bot token and channel, then start Gateway login."""

    try:
        clean_name = Channel.objects.normalized_name(name)
    except ValueError as error:
        raise ValueError("A Discord connection name is required.") from error
    clean_token = str(token or "").strip()
    if not clean_token:
        raise ValueError("A Discord bot token is required.")

    with system_context(reason="messaging_integrate_discord.create"), transaction.atomic():
        credential = Credential.objects.create_local_credential(
            user,
            kind=CredentialKind.STATIC_TOKEN,
            name=f"Discord — {clean_name}",
            material={"api_key": clean_token},
        )
        channel = Channel.objects.create_disconnected(
            user,
            name=clean_name,
            backend_class=DiscordChannelBackend.key,
        )
        channel.connect(credential=credential)
    channel.resume_pairing()
    return channel
