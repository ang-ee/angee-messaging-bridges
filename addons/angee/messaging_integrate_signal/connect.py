"""Signal pairing over the shared messaging channel creation and retry service."""

from __future__ import annotations

from typing import Any

from angee.messaging.connect import resume_or_create_channel
from angee.messaging_integrate_signal.backend import SignalChannelBackend


def create_signal_channel(user: Any) -> Any:
    """Restart the user's newest unfinished Signal channel or create one.

    Signal has no separate ``integrate.Credential``: signal-cli's per-channel
    config directory is the credential. Reusing an unfinished channel resets
    that directory so a rejected account cannot bypass fresh QR pairing.
    """

    return resume_or_create_channel(user, name=SignalChannelBackend.label, backend_class=SignalChannelBackend.key)
