"""Live WhatsApp session — the ``neonize`` binding, worker-only."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from neonize.client import NewClient
from neonize.events import (
    ConnectedEv,
    HistorySyncEv,
    LoggedOutEv,
    MessageEv,
    PairStatusEv,
)
from neonize.utils.jid import build_jid

from angee.integrate.live import STOP_JOIN_SECONDS
from angee.messaging.session import LiveChannelSession
from angee.messaging_integrate_whatsapp.parser import INDIVIDUAL_SERVER, ChatMessage, bare_jid
from angee.storage.uploads import attachment_extension, fallback_attachment_name

logger = logging.getLogger(__name__)


def _jid_str(jid: Any) -> str:
    """Return ``user@server`` from a JID proto."""

    user = getattr(jid, "User", "") or ""
    server = getattr(jid, "Server", "") or ""
    return f"{user}@{server}" if user or server else ""


def _timestamp(value: Any) -> datetime | None:
    """Convert a wire timestamp (unix seconds; tolerate milliseconds) to UTC."""

    try:
        seconds = int(value)
    except TypeError, ValueError:
        return None
    if seconds <= 0:
        return None
    if seconds > 10**12:
        seconds //= 1000
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


@dataclass(frozen=True)
class _MediaFact:
    """Which media payload a message carries; bytes are fetched on the task thread."""

    mime: str
    name: str = ""


_MEDIA_FIELDS = ("imageMessage", "videoMessage", "audioMessage", "stickerMessage", "documentMessage")


def _media_name(stanza_id: str, mime: str, index: int) -> str:
    """Synthesize a display name for media WhatsApp delivers without a ``fileName``.

    Only ``documentMessage`` carries a ``fileName``; images, video, audio, and
    stickers arrive with a MIME type only, so without this they all landed as the
    opaque ``attachment.bin``. The message's stanza id makes the name stable and
    per-message unique (with an index for the rare multi-media stanza), while the
    extension comes from the shared storage naming rule so the core ingest
    fallback and this synthesis agree.
    """

    if not stanza_id:
        return fallback_attachment_name(mime)
    suffix = f"-{index}" if index else ""
    return f"{stanza_id}{suffix}{attachment_extension(mime)}"


def _content_facts(content: Any, stanza_id: str = "") -> tuple[str, str, tuple[_MediaFact, ...]]:
    """Read text, quoted stanza id, and media facts off a wire message payload."""

    text = getattr(content, "conversation", "") or ""
    extended = getattr(content, "extendedTextMessage", None)
    if not text and extended is not None:
        text = getattr(extended, "text", "") or ""
    quoted = ""
    if extended is not None:
        quoted = getattr(getattr(extended, "contextInfo", None), "stanzaID", "") or ""
    media: list[_MediaFact] = []
    for field in _MEDIA_FIELDS:
        node = getattr(content, field, None)
        mime = getattr(node, "mimetype", "") or "" if node is not None else ""
        if not mime:
            continue
        name = getattr(node, "fileName", "") or _media_name(stanza_id, mime, len(media))
        media.append(_MediaFact(mime=mime, name=name))
        text = text or (getattr(node, "caption", "") or "")
        quoted = quoted or (getattr(getattr(node, "contextInfo", None), "stanzaID", "") or "")
    return text, quoted, tuple(media)


class WhatsAppSession(LiveChannelSession):
    """One WhatsApp connection: vendor callbacks translate and enqueue only."""

    client_class: type[Any] = NewClient

    def _connect(self) -> None:
        """Run the blocking vendor connect; unwound by ``client.stop()``."""

        try:
            self.client.connect()
        except Exception:
            logger.exception("WhatsApp connection for channel %s crashed.", self.bridge.sqid)
        finally:
            self.events.put(("disconnected", None))

    def _shutdown(self, connection: threading.Thread) -> bool:
        """Cancel the vendor connection; report whether the Go call unwound."""

        try:
            self.client.stop()
        except Exception:
            logger.exception("Stopping the WhatsApp client for channel %s failed.", self.bridge.sqid)
        connection.join(timeout=STOP_JOIN_SECONDS)
        return not connection.is_alive()

    def _download(self, payload: Any, _fact: Any) -> bytes | None:
        """Fetch one message's media bytes; ``None`` lands the marker part."""

        if payload is None:
            return None
        try:
            return self.client.download_any(payload)
        except Exception:
            logger.info("WhatsApp media download failed for channel %s.", self.bridge.sqid)
            return None

    def _resolve_identity(self, jid: str, name: str) -> tuple[str, str]:
        """Resolve a sender's phone JID (for ``@lid``) and enrich a missing name.

        Returns ``(phone_jid, name)``: ``phone_jid`` is the phone-number JID a
        hidden ``@lid`` sender maps to (``""`` when it is not a LID or the mapping
        is unknown), and ``name`` is the event's push name, or a contact-store
        name when the event had none. Both lookups run here on the vendor callback
        thread, where ``self.client`` and its local whatsmeow LID/contact stores
        live — the session owns its own child process. Lookups are best-effort (a
        miss logs at info and yields the empty resolution) and cached per bare JID,
        so a chatty group costs one vendor round-trip per participant, not one per
        message.
        """

        bare = bare_jid(jid)
        if not bare:
            return "", name
        # A named, non-hidden sender has nothing to resolve — skip the round-trip.
        if name and bare.partition("@")[2] != "lid":
            return "", name
        cache = self.__dict__.setdefault("_identity_cache", {})
        if bare not in cache:
            cache[bare] = self._lookup_identity(bare)
        phone_jid, resolved_name = cache[bare]
        return phone_jid, name or resolved_name

    def _lookup_identity(self, bare: str) -> tuple[str, str]:
        """Query the vendor stores once for a bare JID's phone mapping and name.

        A LID resolves to its phone JID first; the name is then read for the phone
        identity — whatsmeow keys contacts by the phone JID — falling back to the
        original JID when the LID did not resolve.
        """

        phone_jid = self._lid_to_phone(bare) if bare.partition("@")[2] == "lid" else ""
        return phone_jid, self._contact_name(phone_jid or bare)

    def _lid_to_phone(self, jid: str) -> str:
        """Return the phone-number JID a hidden LID maps to, or ``""``."""

        resolver = getattr(self.client, "get_pn_from_lid", None)
        proto = self._jid_proto(jid)
        if resolver is None or proto is None:
            return ""
        try:
            return _jid_str(resolver(proto))
        except Exception:
            logger.info("WhatsApp LID resolution failed on channel %s.", self.bridge.sqid)
            return ""

    def _contact_name(self, jid: str) -> str:
        """Return a contact-store display name for a JID, or ``""``."""

        getter = getattr(getattr(self.client, "contact", None), "get_contact", None)
        proto = self._jid_proto(jid)
        if getter is None or proto is None:
            return ""
        try:
            info = getter(proto)
        except Exception:
            logger.info("WhatsApp contact lookup failed on channel %s.", self.bridge.sqid)
            return ""
        for attr in ("PushName", "FullName", "FirstName", "BusinessName"):
            value = str(getattr(info, attr, "") or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _jid_proto(jid: str) -> Any:
        """Build a vendor JID proto from a bare ``user@server`` string, or ``None``."""

        user, _, server = bare_jid(jid).partition("@")
        if not user:
            return None
        try:
            return build_jid(user, server or INDIVIDUAL_SERVER)
        except Exception:
            return None

    def _build_client(self, store: Path) -> Any:
        """Instantiate the vendor client against the session store and wire events."""

        client = self.client_class(str(store))
        client.event(ConnectedEv)(self._on_connected)
        client.event(PairStatusEv)(self._on_pair_status)
        client.event(LoggedOutEv)(self._on_logged_out)
        client.event(MessageEv)(self._on_message)
        client.event(HistorySyncEv)(self._on_history)
        client.event.qr(self._on_qr)
        return client

    def _on_qr(self, _client: Any, payload: bytes) -> None:
        self.events.put(("qr", payload))

    def _on_connected(self, client: Any, _event: Any) -> None:
        jid = ""
        me = getattr(client, "me", None)
        if me is not None:
            jid = _jid_str(getattr(me, "JID", None))
        self.events.put(("paired", jid))

    def _on_pair_status(self, _client: Any, event: Any) -> None:
        self.events.put(("paired", _jid_str(getattr(event, "ID", None))))

    def _on_logged_out(self, _client: Any, _event: Any) -> None:
        self.events.put(("logged_out", None))

    def _on_message(self, _client: Any, event: Any) -> None:
        info = event.Info
        source = info.MessageSource
        content = event.Message
        stanza_id = str(info.ID or "")
        text, quoted, facts = _content_facts(content, stanza_id)
        sender_jid = _jid_str(source.Sender)
        pushname = str(getattr(info, "Pushname", "") or "")
        from_me = bool(source.IsFromMe)
        if from_me:
            # Our own JID never needs a phone/LID lookup — keep the push name as-is.
            sender_phone_jid, sender_name = "", pushname
        else:
            sender_phone_jid, sender_name = self._resolve_identity(sender_jid, pushname)
        message = ChatMessage(
            chat_jid=_jid_str(source.Chat),
            stanza_id=stanza_id,
            sender_jid=sender_jid,
            sender_phone_jid=sender_phone_jid,
            sender_name=sender_name,
            from_me=from_me,
            timestamp=_timestamp(getattr(info, "Timestamp", 0)),
            text=text,
            quoted_stanza_id=quoted,
            metadata={"_media_facts": facts} if facts else {},
        )
        if message.stanza_id and (message.text or facts):
            self.events.put(("messages", [(message, content if facts else None)]))

    def _on_history(self, _client: Any, event: Any) -> None:
        batch: list[tuple[ChatMessage, Any]] = []
        for conversation in getattr(getattr(event, "Data", None), "conversations", ()) or ():
            chat_jid = str(getattr(conversation, "ID", "") or "")
            for item in getattr(conversation, "messages", ()) or ():
                web_message = getattr(item, "message", None)
                if web_message is None:
                    continue
                key = getattr(web_message, "key", None)
                content = getattr(web_message, "message", None)
                if key is None or content is None:
                    continue
                stanza_id = str(getattr(key, "ID", "") or "")
                text, quoted, facts = _content_facts(content, stanza_id)
                if not (text or facts):
                    continue
                if not stanza_id:
                    continue
                from_me = bool(getattr(key, "fromMe", False))
                sender = str(getattr(key, "participant", "") or "")
                if from_me:
                    sender = self.own_id
                elif not sender:
                    sender = chat_jid
                sender_phone_jid, sender_name = self._resolve_identity(
                    sender, str(getattr(web_message, "pushName", "") or "")
                )
                message = ChatMessage(
                    chat_jid=chat_jid,
                    stanza_id=stanza_id,
                    sender_jid=sender,
                    sender_phone_jid=sender_phone_jid,
                    sender_name=sender_name,
                    from_me=from_me,
                    timestamp=_timestamp(getattr(web_message, "messageTimestamp", 0)),
                    text=text,
                    quoted_stanza_id=quoted,
                    metadata={"_media_facts": facts, "history": True} if facts else {"history": True},
                )
                batch.append((message, content if facts else None))
        if batch:
            self.events.put(("messages", batch))
