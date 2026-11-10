"""iMessage channel creation service.

iMessage is import-only, so this addon owns just the disconnected-channel create
that ``imessage_import`` targets — there is no live pairing lifecycle (contrast
:mod:`angee.messaging_integrate_whatsapp.connect`, which also starts a session).
Base ``messaging`` owns the neutral ``Channel`` model and its list/detail surface.
"""

from __future__ import annotations

from typing import Any

from django.apps import apps
from django.db import transaction
from rebac import system_context

from angee.messaging_integrate_imessage.backend import ImessageChannelBackend

_IMESSAGE_SLUG = ImessageChannelBackend.key
"""This addon's one name — the seeded vendor catalogue slug and the channel
backend registry key are the same fact spelled once."""


def create_imessage_channel(user: Any, *, name: str) -> Any:
    """Create a disconnected iMessage channel for a backup import to land on.

    The ``Channel`` model is resolved lazily (as in :mod:`.backend`) so importing
    this module never touches the app registry.
    """

    channel_model = apps.get_model("messaging", "Channel")
    with system_context(reason="messaging_integrate_imessage.create"), transaction.atomic():
        channel = channel_model.objects.create_disconnected(
            user,
            name=name,
            backend_class=_IMESSAGE_SLUG,
        )
    return channel
