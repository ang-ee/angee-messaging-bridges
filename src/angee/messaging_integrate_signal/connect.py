"""Signal pairing over the messaging channel manager's create-or-retry verb."""

from __future__ import annotations

from typing import Any

from django.apps import apps

from angee.messaging_integrate_signal.backend import SignalChannelBackend

Channel = apps.get_model("messaging", "Channel")


def create_signal_channel(user: Any) -> Any:
    """Restart the user's newest unfinished Signal channel or create one.

    Signal has no separate ``integrate.Credential``: signal-cli's per-channel
    config directory is the credential. Reusing an unfinished channel resets
    that directory so a rejected account cannot bypass fresh QR pairing.
    """

    return Channel.objects.resume_or_create_pairing(
        user, name=SignalChannelBackend.label, backend_class=SignalChannelBackend.key
    )
