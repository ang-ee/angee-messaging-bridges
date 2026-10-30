"""Live WhatsApp session — the ``neonize`` binding, worker-only."""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from django.conf import settings
from neonize.client import NewClient
from neonize.events import (
    ConnectedEv,
    HistorySyncEv,
    LoggedOutEv,
    MessageEv,
    PairStatusEv,
    PictureEv,
)
from neonize.proto.Neonize_pb2 import GetProfilePictureParams
from neonize.utils.jid import build_jid

from angee.integrate.live import STOP_JOIN_SECONDS, WAKE_SECONDS
from angee.messaging.backends import ParsedHandle
from angee.messaging.session import LiveChannelSession
from angee.messaging_integrate_whatsapp.parser import (
    INDIVIDUAL_SERVER,
    ChatMessage,
    bare_jid,
    handle_for_jid,
    is_group_jid,
    parsed_message,
)
from angee.parties.backends import AvatarReport

logger = logging.getLogger(__name__)

AVATAR_INTERVAL_SECONDS = 1.0
"""Spacing between profile-picture queries, so a history sync's many senders never burst the account."""

_NO_AVATAR_ANSWER = "does not have a profile picture"
"""whatsmeow's not-set answer, which neonize flattens to text: the contact shows no avatar to anyone.

Its hidden-from-you answer is not this one: that hides the avatar from this account
only, so the handle keeps the avatar another account may still see.
"""


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


def _content_facts(content: Any) -> tuple[str, str, tuple[_MediaFact, ...]]:
    """Read text, quoted stanza id, and media facts off a wire message payload.

    Only ``documentMessage`` carries a ``fileName``; images, video, audio, and
    stickers arrive with a MIME type only and no name. Such nameless media flow
    through with an empty ``name`` — the core ingest owner
    (``Message.objects.ingest``) derives the stable per-message attachment name
    from the message's kind and stanza-scoped ``external_id``, so every backend
    shares one naming rule instead of each bridge synthesizing its own.
    """

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
        name = getattr(node, "fileName", "") or ""
        media.append(_MediaFact(mime=mime, name=name))
        text = text or (getattr(node, "caption", "") or "")
        quoted = quoted or (getattr(getattr(node, "contextInfo", None), "stanzaID", "") or "")
    return text, quoted, tuple(media)


class WhatsAppSession(LiveChannelSession):
    """One WhatsApp connection: vendor callbacks translate and enqueue only."""

    client_class: type[Any] = NewClient

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._avatar_queue: queue.Queue[ParsedHandle] = queue.Queue()
        """Senders whose avatar to read, fed by the vendor callbacks."""
        self._avatar_seen: set[str] = set()
        """Handle identities whose avatar this session already queued."""

    def _connect(self) -> None:
        """Run the blocking vendor connect; unwound by ``client.stop()``."""

        threading.Thread(target=self._read_avatars, name=f"whatsapp-avatars-{self.bridge.sqid}", daemon=True).start()
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

    def _want_avatar(self, message: ChatMessage) -> None:
        """Queue a received message's sender avatar the first time this session sees them.

        Callers queue only after the message itself, so the task thread lands the
        sender's handle before its avatar.
        """

        sender = parsed_message(message).sender
        if message.from_me or sender is None or sender.external_id in self._avatar_seen:
            return
        if is_group_jid(sender.external_id):
            return
        self._avatar_seen.add(sender.external_id)
        self._avatar_queue.put(sender)

    def _read_avatars(self) -> None:
        """Read the queued senders' avatars, one per interval, until the session stops.

        Each answer lands as a ``handle_avatars`` report, which the task thread
        sets on its handle.
        """

        while not self._vendor_stopping():
            try:
                handle = self._avatar_queue.get(timeout=WAKE_SECONDS)
            except queue.Empty:
                continue
            report = self._read_avatar(handle)
            if report is not None:
                self.events.put(("handle_avatars", [report]))
            self._stopping.wait(AVATAR_INTERVAL_SECONDS)

    def _read_avatar(self, handle: ParsedHandle) -> AvatarReport | None:
        """Return the avatar WhatsApp shows for ``handle``, or ``None`` when that is unknown.

        Its not-set answer reports no avatar; any other failure, including the
        avatar hidden from this account, reports nothing. The download is capped
        at storage's image size.
        """

        def report(avatar: bytes | None) -> AvatarReport:
            return AvatarReport(
                platform=handle.platform, value=handle.value, external_id=handle.external_id, avatar=avatar
            )

        try:
            info = self.client.get_profile_picture(self._jid_proto(handle.external_id), GetProfilePictureParams())
        except Exception as error:
            if _NO_AVATAR_ANSWER in str(error):
                return report(None)
            logger.info("WhatsApp avatar lookup failed on channel %s.", self.bridge.sqid)
            return None
        url = str(getattr(info, "URL", "") or "")
        if not url:
            return None
        try:
            data = self.live_impl.http.download_capped(url, cap=int(settings.ANGEE_STORAGE_IMAGE_MAX_BYTES))
        except Exception:
            logger.info("WhatsApp avatar download failed on channel %s.", self.bridge.sqid)
            return None
        return report(data) if data else None

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
        client.event(PictureEv)(self._on_avatar_change)
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
        titles = self._joined_group_titles(client)
        if titles:
            self.events.put(("chat_titles", titles))

    def _group_titles(self) -> dict[str, str]:
        """Group subjects known to this session, keyed by bare group JID."""

        return self.__dict__.setdefault("_group_title_cache", {})

    def _joined_group_titles(self, client: Any) -> dict[str, str]:
        """Read every joined group's subject once per connection (best-effort)."""

        try:
            groups = client.get_joined_groups()
        except Exception:
            logger.info("WhatsApp joined-group lookup failed on channel %s.", self.bridge.sqid, exc_info=True)
            return {}
        titles = {}
        for group in groups:
            jid = bare_jid(_jid_str(getattr(group, "JID", None)))
            name = str(getattr(getattr(group, "GroupName", None), "Name", "") or "").strip()
            if jid and name:
                titles[jid] = name
        self._group_titles().update(titles)
        return titles

    def _on_pair_status(self, _client: Any, event: Any) -> None:
        self.events.put(("paired", _jid_str(getattr(event, "ID", None))))

    def _on_logged_out(self, _client: Any, _event: Any) -> None:
        self.events.put(("logged_out", None))

    def _on_avatar_change(self, _client: Any, event: Any) -> None:
        """Read a changed avatar again for a sender this session already read.

        Others are read at their first message, so reads stay with the people
        this account corresponds with.
        """

        jid = _jid_str(getattr(event, "JID", None))
        if not bare_jid(jid):
            return
        phone_jid, name = self._resolve_identity(jid, "")
        handle = handle_for_jid(jid, name, phone_jid=phone_jid)
        if handle.external_id in self._avatar_seen:
            self._avatar_queue.put(handle)

    def _on_message(self, _client: Any, event: Any) -> None:
        info = event.Info
        source = info.MessageSource
        content = event.Message
        stanza_id = str(info.ID or "")
        text, quoted, facts = _content_facts(content)
        sender_jid = _jid_str(source.Sender)
        pushname = str(getattr(info, "Pushname", "") or "")
        from_me = bool(source.IsFromMe)
        if from_me:
            # Our own JID never needs a phone/LID lookup — keep the push name as-is.
            sender_phone_jid, sender_name = "", pushname
        else:
            sender_phone_jid, sender_name = self._resolve_identity(sender_jid, pushname)
        chat_jid = _jid_str(source.Chat)
        message = ChatMessage(
            chat_jid=chat_jid,
            chat_name=self._group_titles().get(bare_jid(chat_jid), ""),
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
            self._want_avatar(message)

    def _on_history(self, _client: Any, event: Any) -> None:
        batch: list[tuple[ChatMessage, Any]] = []
        for conversation in getattr(getattr(event, "Data", None), "conversations", ()) or ():
            chat_jid = str(getattr(conversation, "ID", "") or "")
            # Direct chats stay untitled; the thread label names the counterpart.
            chat_name = (
                str(getattr(conversation, "name", "") or "").strip() or self._group_titles().get(bare_jid(chat_jid), "")
                if is_group_jid(chat_jid)
                else ""
            )
            for item in getattr(conversation, "messages", ()) or ():
                web_message = getattr(item, "message", None)
                if web_message is None:
                    continue
                key = getattr(web_message, "key", None)
                content = getattr(web_message, "message", None)
                if key is None or content is None:
                    continue
                stanza_id = str(getattr(key, "ID", "") or "")
                text, quoted, facts = _content_facts(content)
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
                    chat_name=chat_name,
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
        for message, _payload in batch:
            self._want_avatar(message)
