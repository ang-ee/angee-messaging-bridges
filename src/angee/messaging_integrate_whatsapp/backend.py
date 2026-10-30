"""WhatsApp channel backend: live-session identity and message parsing hooks."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from django.apps import apps
from django.core.exceptions import ValidationError
from rebac import system_context

from angee.integrate.live import SessionLoggedOut
from angee.messaging.backends import LiveChannelBackend, ParsedMessage
from angee.messaging_integrate_whatsapp.client import DuplicateAccountRejected
from angee.messaging_integrate_whatsapp.constants import SESSION_QUEUE
from angee.messaging_integrate_whatsapp.parser import (
    ChatMessage,
    bare_jid,
    parsed_message,
    phone_for_jid,
)


class WhatsAppChannelBackend(LiveChannelBackend):
    """Channel backend for a linked WhatsApp account — one live session per channel."""

    key = "whatsapp"
    label = "WhatsApp"
    icon = "message-circle"
    session_queue = SESSION_QUEUE
    session_isolation = "process"
    """Keep each account's native Go runtime inside its own fresh process."""
    session_class = "angee.messaging_integrate_whatsapp.session.WhatsAppSession"
    state_identity_key = "own_jid"

    def normalize_account_id(self, raw: str) -> str:
        """Return WhatsApp's durable bare-JID account identity."""

        return bare_jid(raw)

    def account_label(self, own_id: str) -> str:
        """Return a human label for a WhatsApp account id."""

        return phone_for_jid(own_id) or own_id

    def parse_live_message(self, message: ChatMessage) -> ParsedMessage:
        """Map one queued WhatsApp live message onto the neutral messaging seam."""

        return parsed_message(message)

    def duplicate_account_error(self) -> Exception:
        """Return the runtime error recorded for a duplicate WhatsApp account."""

        return DuplicateAccountRejected("Another channel already owns this WhatsApp account.")

    def logged_out_error(self) -> SessionLoggedOut:
        """Restore WhatsApp's operator-visible phone/device logout wording."""

        return SessionLoggedOut("The linked phone removed this device.")


def confirmed_whatsapp_channel(sqid: str) -> Any:
    """Return the confirmed WhatsApp channel named by its public sqid.

    The single owner of "resolve an import target to a WhatsApp channel", shared
    by the archive (ZIP) and mount (drive) backup extractors. The Channel model
    is resolved lazily so importing this module never touches the app registry.
    """

    channel_model = apps.get_model("messaging", "Channel")
    with system_context(reason="messaging_integrate_whatsapp.channel.confirm"):
        channel = channel_model._base_manager.filter(
            sqid=sqid, backend_class=WhatsAppChannelBackend.key
        ).first()
    if channel is None:
        raise ValidationError({"target": f"No WhatsApp channel {sqid!r}."})
    return channel


class StoreUnreadable(Exception):
    """The whatsmeow store cannot supply its LID map."""


class LidResolver(Protocol):
    """What the resolution needs from a session store: the phone behind a LID and a contact name."""

    def phone_for_lid(self, bare_lid: str) -> str: ...

    def name_for_phone(self, phone_digits: str) -> str: ...


@dataclass
class StoreResolver:
    """The whatsmeow session store's LID map and contact names, read once."""

    lid_to_pn: dict[str, str] = field(default_factory=dict)
    contacts: dict[str, tuple[str, str]] = field(default_factory=dict)

    def phone_for_lid(self, bare_lid: str) -> str:
        """Return the phone digits mapped to ``bare_lid``, or "" when the store has no mapping."""
        return self.lid_to_pn.get(_lid_digits(bare_lid), "")

    def name_for_phone(self, phone_digits: str) -> str:
        """Return the contact's push name, else full name, for a phone number."""
        push, full = self.contacts.get(phone_digits.strip(), ("", ""))
        return (push or full).strip()

    @classmethod
    def from_store(cls, db_path: Path) -> StoreResolver:
        """Read the LID map and contacts from the store; an absent store resolves nothing."""
        if not db_path.exists():
            return cls()
        try:
            connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
            try:
                lids = connection.execute("SELECT lid, pn FROM whatsmeow_lid_map").fetchall()
                rows = connection.execute("SELECT their_jid, full_name, push_name FROM whatsmeow_contacts").fetchall()
            finally:
                connection.close()
        except sqlite3.Error as error:
            raise StoreUnreadable(f"The session store at {db_path} is unreadable: {error}") from error
        lid_to_pn = {
            key: digits
            for lid, pn in lids
            if (key := _lid_digits(str(lid or "")))
            and (digits := "".join(character for character in str(pn or "") if character.isdigit()))
        }
        names = {
            digits: (str(push or "").strip(), str(full or "").strip())
            for jid, full, push in rows
            if (digits := str(jid or "").split("@", 1)[0].strip())
        }
        return cls(lid_to_pn, names)


def _lid_digits(lid: str) -> str:
    """Return the digits before ``@`` of a LID, lower-cased and trimmed."""
    return lid.strip().lower().split("@", 1)[0]


@dataclass
class LidResolveStats:
    """Counts of one resolution pass, printed by the command."""

    candidates: int = 0
    resolved: int = 0
    merged: int = 0
    named: int = 0
    unresolved: int = 0
    refused: int = 0

    def summary(self, *, dry_run: bool) -> str:
        """Return the one-line report for the operator."""
        verb = "Would resolve" if dry_run else "Resolved"
        return (
            f"{verb} {self.resolved}/{self.candidates} @lid handle(s): "
            f"{self.merged} merged into an existing phone handle, "
            f"{self.named} name(s) filled, {self.unresolved} left unresolved (no mapping), "
            f"{self.refused} refused by the handle merge."
        )


def resolve_channel_lids(channel: Any, resolver: LidResolver, *, dry_run: bool = False) -> LidResolveStats:
    """Resolve every canonical ``@lid`` handle of ``channel`` the store maps to a phone number.

    A mapped LID whose phone already has a handle is folded into it (the alias stays as a
    redirect); otherwise the LID handle is rewritten to the phone number. Names fill empty
    display names only. A merge the handle owner refuses is counted, never forced.
    """
    model = apps.get_model("parties", "Handle")
    stats = LidResolveStats()
    with system_context(reason="whatsapp_resolve_lids"):
        handles = list(
            model.objects.canonical()
            .filter(
                platform="whatsapp", value__endswith="@lid", participations__thread__channel=channel
            )
            .distinct()
            .order_by("pk")
        )
        stats.candidates = len(handles)
        for handle in handles:
            bare_lid = (handle.external_id or handle.value).strip().lower()
            phone_digits = resolver.phone_for_lid(bare_lid)
            if not phone_digits:
                stats.unresolved += 1
                continue
            new_value = f"+{phone_digits}"
            name = resolver.name_for_phone(phone_digits)
            target = model.objects.find_identity(platform="whatsapp", value=new_value)
            if dry_run:
                stats.resolved += 1
                stats.merged += target is not None and target.pk != handle.pk
                stats.named += bool(name and not handle.display_name)
                continue
            try:
                outcome = model.objects.reidentify(
                    handle, expected_value=handle.value, value=new_value, display_name=name,
                )
            except ValidationError:
                stats.refused += 1
                continue
            if outcome is not None:
                stats.resolved += 1
                stats.merged += outcome[0]
                stats.named += outcome[1]
    return stats
