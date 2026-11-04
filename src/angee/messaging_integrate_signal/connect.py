"""Signal pairing over the shared messaging channel creation and retry service."""

from __future__ import annotations

from typing import Any

from django.apps import apps

from angee.messaging_integrate_signal.backend import SignalChannelBackend


def create_signal_channel(user: Any) -> Any:
    """Restart the user's newest unfinished Signal channel or create one.

    Signal has no separate ``integrate.Credential``: signal-cli's per-channel
    config directory is the credential. Reusing an unfinished channel resets
    that directory so a rejected account cannot bypass fresh QR pairing.
    """

    return apps.get_model("messaging", "Channel").objects.resume_or_create_pairing(
        user, name=SignalChannelBackend.label, backend_class=SignalChannelBackend.key,
    )
