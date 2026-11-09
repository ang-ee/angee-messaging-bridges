"""Facebook import-channel confirmation."""

from __future__ import annotations

from typing import Any

from django.apps import apps
from django.core.exceptions import ValidationError
from rebac import system_context

from angee.messaging_integrate_facebook.backend import FacebookChannelBackend


def confirmed_facebook_channel(sqid: str) -> Any:
    """Return the Facebook import channel named by public sqid."""

    channel_model = apps.get_model("messaging", "Channel")
    with system_context(reason="messaging_integrate_facebook.channel.confirm"):
        channel = channel_model._base_manager.filter(
            sqid=sqid,
            backend_class=FacebookChannelBackend.key,
        ).first()
    if channel is None:
        raise ValidationError({"target": f"No Facebook channel {sqid!r}."})
    return channel
