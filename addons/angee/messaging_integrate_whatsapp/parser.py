"""WhatsApp DTO-carrying parser shared by live and backup ingest.

Bare JID normalization makes device-qualified live events converge with backup
rows; external ids are chat-scoped stanza ids; handles keep the bare JID as the
stable identity and expose a phone value only when derivable. Unlike Telegram's
identity-only mapping, this parser owns the DTO also produced by ``backup.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

from angee.messaging.backends import MediaItem, ParsedHandle, ParsedMessage, ParsedThread, body_part

PLATFORM = "whatsapp"
GROUP_SERVERS = ("g.us", "broadcast")
INDIVIDUAL_SERVER = "s.whatsapp.net"

DIRECTION_INBOUND = "inbound"
DIRECTION_OUTBOUND = "outbound"


def bare_jid(jid: str) -> str:
    """Normalize a JID to its bare ``user@server`` identity, lowercased.

    Strips the multi-device qualifiers from the user part — ``:device`` and a
    numeric ``.agent`` — so a live event's ``4917….3:12@s.whatsapp.net`` and a
    backup's ``4917…@s.whatsapp.net`` name the same identity. Non-JID strings
    pass through lowercased, so a caller never crashes on vendor surprises.
    """

    jid = (jid or "").strip().lower()
    if "@" not in jid:
        return jid
    user, _, server = jid.partition("@")
    user = user.partition(":")[0]
    head, dot, tail = user.partition(".")
    if dot and head.isdigit() and tail.isdigit():
        user = head
    return f"{user}@{server}"


def phone_for_jid(jid: str) -> str:
    """Return the E.164 phone (``+digits``) behind an individual JID, or ``""``.

    Only ``s.whatsapp.net`` user parts are phone numbers; group ids, broadcast
    channels, and hidden-identity (``@lid``) JIDs have no derivable phone.
    """

    user, _, server = bare_jid(jid).partition("@")
    if server == INDIVIDUAL_SERVER and user.isdigit():
        return f"+{user}"
    return ""


def is_group_jid(jid: str) -> bool:
    """Return whether a chat JID names a group or broadcast conversation."""

    return bare_jid(jid).partition("@")[2] in GROUP_SERVERS


def is_lid_jid(jid: str) -> bool:
    """Return whether a JID is a hidden-identity ``@lid`` address."""

    return bare_jid(jid).partition("@")[2] == "lid"


def handle_for_jid(jid: str, display_name: str = "", *, phone_jid: str = "") -> ParsedHandle:
    """Return the whatsapp handle for a JID — phone as the value when derivable.

    ``phone_jid`` carries the phone-number JID a hidden ``@lid`` sender resolved
    to (the session owns that vendor lookup); it lets the handle expose a real
    E.164 ``value`` while ``external_id`` stays the bare LID — the stable, re-sync
    idempotency key. When the JID is a LID the bare LID is also stamped into
    ``metadata`` so it stays retrievable even if the row later merges into an
    existing phone handle on the ``(platform, value)`` uniqueness.
    """

    bare = bare_jid(jid)
    metadata = {"lid": bare} if bare.partition("@")[2] == "lid" else {}
    return ParsedHandle(
        platform=PLATFORM,
        value=phone_for_jid(bare) or phone_for_jid(phone_jid) or bare,
        display_name=display_name,
        external_id=bare,
        metadata=metadata,
    )


def external_id(chat_jid: str, stanza_id: str) -> str:
    """Compose the chat-scoped idempotency key both ingest paths converge on."""

    return f"{bare_jid(chat_jid)}/{stanza_id}"


@dataclass(frozen=True)
class ChatMessage:
    """The WhatsApp message DTO both live and backup producers emit."""

    chat_jid: str
    stanza_id: str = ""
    fallback_id: str = ""
    chat_name: str = ""
    group: bool | None = None
    sender_jid: str = ""
    sender_phone_jid: str = ""
    sender_name: str = ""
    from_me: bool = False
    timestamp: datetime | None = None
    text: str = ""
    quoted_stanza_id: str = ""
    media: tuple[MediaItem, ...] = ()
    metadata: dict = field(default_factory=dict)

    def with_media(self, media: tuple[MediaItem, ...]) -> ChatMessage:
        """Return this message with task-thread-resolved media attached."""

        return replace(self, media=media)


def parsed_message(message: ChatMessage) -> ParsedMessage:
    """Map one :class:`ChatMessage` onto the neutral messaging seam."""

    chat = bare_jid(message.chat_jid)
    stanza = message.stanza_id or message.fallback_id
    group = message.group if message.group is not None else is_group_jid(chat)
    return ParsedMessage(
        external_id=external_id(chat, stanza),
        platform=PLATFORM,
        direction=DIRECTION_OUTBOUND if message.from_me else DIRECTION_INBOUND,
        sender=(
            handle_for_jid(message.sender_jid, message.sender_name, phone_jid=message.sender_phone_jid)
            if message.sender_jid
            else None
        ),
        sent_at=message.timestamp,
        in_reply_to=external_id(chat, message.quoted_stanza_id) if message.quoted_stanza_id else "",
        thread=ParsedThread(
            external_id=chat,
            modality="group" if group else "direct",
            title=message.chat_name,
        ),
        body=body_part(message.text, message.media),
        metadata={"chat_jid": chat, **message.metadata},
    )
