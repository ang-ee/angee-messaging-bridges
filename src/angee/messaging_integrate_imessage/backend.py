"""iMessage channel backend — a backup/mount import target, not a live source.

Unlike WhatsApp, an Apple Messages account is not linkable as a live device: this
addon imports history from an iPhone backup only. The backend therefore carries no
session, pairing, or OAuth — it exists so a ``messaging.Channel`` can name the
``imessage`` platform, and so the archive/mount extractors can resolve a confirmed
channel of this platform. It inherits the empty stream declaration; the backup
importer populates it.
"""

from __future__ import annotations

from typing import ClassVar

from angee.messaging.backends import ChannelBackend


class ImessageChannelBackend(ChannelBackend):
    """Channel backend for imported SMS + iMessage history — import only."""

    key = "imessage"
    label = "iMessage"
    icon = "message-square"

    quote_edges: ClassVar[bool] = False
