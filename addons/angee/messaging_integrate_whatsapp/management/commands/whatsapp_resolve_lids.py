"""Resolve WhatsApp hidden ``@lid`` handles from a channel's local session store."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from django.apps import apps
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import IntegrityError, transaction
from rebac import system_context

from angee.integrate.live import session_store_path
from angee.integrate.session import LiveSession
from angee.messaging_integrate_whatsapp.backend import confirmed_whatsapp_channel


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
                outcome = _resolve_lid_handle(model, handle, target=target, value=new_value, name=name)
            except ValidationError:
                stats.refused += 1
                continue
            if outcome is not None:
                stats.resolved += 1
                stats.merged += outcome[0]
                stats.named += outcome[1]
    return stats


def _resolve_lid_handle(
    model: Any, handle: Any, *, target: Any | None, value: str, name: str
) -> tuple[bool, bool] | None:
    """Fold or rewrite one LID handle under its row lock.

    Returns ``(merged, named)``, or ``None`` when a concurrent run already resolved the
    handle (it is gone, redirected, or no longer a LID), which is a skip, not a refusal.
    """
    with transaction.atomic():
        locked = model.system_queryset(lock=("self",)).filter(pk=handle.pk).first()
        if locked is None or locked.merged_into_id is not None or not locked.value.endswith("@lid"):
            return None
        if target is not None and target.pk != locked.pk:
            return _fold_lid_handle(locked, target, name=name)
        try:
            with transaction.atomic():
                return False, _rewrite_lid_handle(locked, new_value=value, name=name)
        except IntegrityError:
            # The phone handle appeared between the lookup and the rewrite: fold into it.
            target = model.objects.find_identity(platform="whatsapp", value=value)
            if target is None or target.pk == locked.pk:
                raise
        return _fold_lid_handle(locked, target, name=name)


def _rewrite_lid_handle(handle: Any, *, new_value: str, name: str) -> bool:
    """Turn the LID handle into the phone handle in place; return whether a name was filled."""
    handle.value = new_value
    fields = ["value"]
    named = bool(name and not handle.display_name)
    if named:
        handle.display_name = name
        fields.append("display_name")
    handle.save(update_fields=[*fields, "updated_at"])
    return named


def _fold_lid_handle(handle: Any, target: Any, *, name: str) -> tuple[bool, bool]:
    """Merge the LID handle into the existing phone handle; return ``(True, named)``."""
    kept = target.fold([handle])
    named = bool(name and not kept.display_name)
    if named:
        kept.display_name = name
        kept.save(update_fields=["display_name", "updated_at"])
    return True, named


class Command(BaseCommand):
    """Resolve a channel's hidden ``@lid`` senders from its local whatsmeow store."""

    help = "Resolve WhatsApp @lid sender handles to phone numbers/names from the channel's session store."

    def add_arguments(self, parser: CommandParser) -> None:
        """Declare the channel and dry-run options."""
        parser.add_argument("--channel", required=True, help="Target WhatsApp channel (sqid).")
        parser.add_argument("--dry-run", action="store_true", help="Report changes without writing them.")

    def handle(self, *args: Any, **options: Any) -> None:
        """Read the store, resolve the channel's LIDs and print the summary."""
        del args
        try:
            channel = confirmed_whatsapp_channel(options["channel"])
        except ValidationError as error:
            raise CommandError(str(error)) from error
        db_path = session_store_path(channel) / LiveSession.session_file_name
        if not db_path.exists():
            raise CommandError(f"No WhatsApp session store for channel {options['channel']!r} at {db_path}.")
        try:
            resolver = StoreResolver.from_store(db_path)
        except StoreUnreadable as error:
            raise CommandError(str(error)) from error
        stats = resolve_channel_lids(channel, resolver, dry_run=options["dry_run"])
        self.stdout.write(self.style.SUCCESS(stats.summary(dry_run=options["dry_run"])))
