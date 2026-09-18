"""Resolve WhatsApp hidden ``@lid`` sender handles to phone numbers and names.

WhatsApp delivers group senders (and, increasingly, one-to-one senders) as
hidden-identity ``<digits>@lid`` JIDs. Without resolution those land as handles
whose ``value`` is the raw LID — a bogus phone that never resolves to a person.
whatsmeow already keeps the answer in each channel's local session store:
``whatsmeow_lid_map`` maps a LID to its phone number and ``whatsmeow_contacts``
carries push/full names. This command reads that store directly (read-only, so a
live session may keep running) and rewrites existing handles to a real
``+E.164`` value and name, keeping the bare LID as the ``external_id`` and in
``metadata['lid']`` so re-sync stays idempotent and the LID stays retrievable.

It needs no live WhatsApp connection: it only reads the on-disk store. It is
re-runnable — a later run picks up LID mappings the store has since learned — and
converges a resolved LID handle into an existing phone handle when they collide
on the ``(platform, value)`` uniqueness, repointing the LID handle's messages,
participants, and identity links before removing the duplicate.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from django.apps import apps
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from rebac import system_context

from angee.integrate.live import session_store_path
from angee.integrate.session import LiveSession
from angee.messaging_integrate_whatsapp.backend import confirmed_whatsapp_channel

INDIVIDUAL_SERVER = "s.whatsapp.net"


class StoreUnreadable(Exception):
    """The session store exists but could not be read (bad file, missing table, lock).

    Distinct from an empty store: a store the backfill cannot open or query is an
    operator problem the command surfaces as a non-zero ``CommandError``, whereas
    an empty-but-readable LID map is a benign "nothing to resolve yet" warning.
    """


class LidResolver(Protocol):
    """The two lookups a LID handle rewrite needs from a channel's store."""

    def phone_for_lid(self, bare_lid: str) -> str:
        """Return the phone number (digits, no ``+``) a bare LID maps to, or ``""``."""

    def name_for_phone(self, phone_digits: str) -> str:
        """Return a display name (push name, then full name) for a phone, or ``""``."""


@dataclass
class StoreResolver:
    """LID/name lookups backed by a channel's whatsmeow session store tables."""

    lid_to_pn: dict[str, str] = field(default_factory=dict)
    contacts: dict[str, tuple[str, str]] = field(default_factory=dict)

    def phone_for_lid(self, bare_lid: str) -> str:
        """Return the mapped phone digits for a bare LID, or ``""`` when unknown."""

        return self.lid_to_pn.get(_lid_digits(bare_lid), "")

    def name_for_phone(self, phone_digits: str) -> str:
        """Return the push name, then full name, stored for a phone, or ``""``."""

        push, full = self.contacts.get((phone_digits or "").strip(), ("", ""))
        return (push or full or "").strip()

    @classmethod
    def from_store(cls, db_path: Path) -> StoreResolver:
        """Read the LID map and contacts from a whatsmeow session store, read-only.

        Opens the sqlite file in read-only mode so a live session may keep it open.
        A *missing* store is benign — there is simply nothing to resolve — so it
        yields an empty resolver. But a store that exists yet cannot be opened, or
        whose ``whatsmeow_lid_map`` table is absent or errors on read, is an
        operator fault the command must surface (not silently mistake for "no
        mappings"), so it raises :class:`StoreUnreadable`. An empty-but-readable
        table is not an error: it yields an empty map.
        """

        lid_to_pn: dict[str, str] = {}
        contacts: dict[str, tuple[str, str]] = {}
        if not db_path.exists():
            return cls(lid_to_pn, contacts)
        try:
            connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        except sqlite3.Error as error:
            raise StoreUnreadable(f"The session store at {db_path} could not be opened: {error}") from error
        try:
            for lid, pn in _read(connection, "SELECT lid, pn FROM whatsmeow_lid_map"):
                lid_key = _lid_digits(str(lid or ""))
                digits = "".join(character for character in str(pn or "") if character.isdigit())
                if lid_key and digits:
                    lid_to_pn[lid_key] = digits
            for their_jid, full_name, push_name in _read(
                connection, "SELECT their_jid, full_name, push_name FROM whatsmeow_contacts"
            ):
                digits = str(their_jid or "").split("@", 1)[0].strip()
                if digits:
                    contacts[digits] = (str(push_name or "").strip(), str(full_name or "").strip())
        except sqlite3.Error as error:
            raise StoreUnreadable(f"The session store at {db_path} is unreadable: {error}") from error
        finally:
            connection.close()
        return cls(lid_to_pn, contacts)


def _lid_digits(lid: str) -> str:
    """Return the digits that identify a LID, whether written bare or as ``<digits>@lid``.

    whatsmeow stores the map key bare (``113352894324870``) while the bridge keeps
    the handle's ``external_id`` as the full JID (``113352894324870@lid``); keying
    both sides on the digits lets either spelling resolve.
    """

    return (lid or "").strip().lower().split("@", 1)[0]


def _read(connection: sqlite3.Connection, sql: str) -> list[tuple[Any, ...]]:
    """Run one read query and return its rows.

    An absent table, a lock, or a corrupt store raises ``sqlite3.Error``, which
    the caller turns into :class:`StoreUnreadable`; only a readable-but-empty
    table returns ``[]``. Distinguishing the two is the whole point — an
    unreadable store must not masquerade as "no LID mappings yet".
    """

    return list(connection.execute(sql).fetchall())


@dataclass
class LidResolveStats:
    """Per-run counters the command reports."""

    candidates: int = 0
    resolved: int = 0
    merged: int = 0
    named: int = 0
    unresolved: int = 0

    def summary(self, *, dry_run: bool) -> str:
        """Return a one-line human summary of the run."""

        verb = "Would resolve" if dry_run else "Resolved"
        return (
            f"{verb} {self.resolved}/{self.candidates} @lid handle(s): "
            f"{self.merged} merged into an existing phone handle, "
            f"{self.named} name(s) filled, {self.unresolved} left unresolved (no mapping)."
        )


def resolve_channel_lids(channel: Any, resolver: LidResolver, *, dry_run: bool = False) -> LidResolveStats:
    """Rewrite this channel's ``@lid`` handles through ``resolver``; return counts.

    Scoped to handles that participate in the channel's threads, so the run only
    touches the account whose store was read. Each still-unresolved LID handle
    (``value`` ends with ``@lid``) is looked up; a hit rewrites the handle to
    ``+E.164`` in place, or — when a phone handle already owns that value — merges
    into it. A miss is left for a later run to resolve.
    """

    handle_model = apps.get_model("parties", "Handle")
    stats = LidResolveStats()
    with system_context(reason="whatsapp_resolve_lids"):
        candidates = list(
            handle_model._base_manager.filter(
                platform="whatsapp",
                value__endswith="@lid",
                participations__thread__channel=channel,
            )
            .distinct()
            .order_by("pk")
        )
        stats.candidates = len(candidates)
        for handle in candidates:
            bare_lid = (handle.external_id or handle.value or "").strip().lower()
            phone_digits = resolver.phone_for_lid(bare_lid)
            if not phone_digits:
                stats.unresolved += 1
                continue
            new_value = f"+{phone_digits}"
            name = resolver.name_for_phone(phone_digits)
            stats.resolved += 1
            if dry_run:
                _tally_dry_run(handle_model, handle, new_value=new_value, name=name, stats=stats)
                continue
            with transaction.atomic():
                target = (
                    handle_model._base_manager.select_for_update()
                    .filter(platform="whatsapp", value=new_value)
                    .exclude(pk=handle.pk)
                    .first()
                )
                if target is not None:
                    _merge_into_phone_handle(handle, target, bare_lid=bare_lid, name=name, stats=stats)
                    stats.merged += 1
                else:
                    _rewrite_lid_handle(handle, new_value=new_value, bare_lid=bare_lid, name=name, stats=stats)
    return stats


def _tally_dry_run(handle_model: Any, handle: Any, *, new_value: str, name: str, stats: LidResolveStats) -> None:
    """Count the merge/name outcomes a real run would produce for one handle."""

    if handle_model._base_manager.filter(platform="whatsapp", value=new_value).exclude(pk=handle.pk).exists():
        stats.merged += 1
    if name and not handle.display_name:
        stats.named += 1


def _rewrite_lid_handle(handle: Any, *, new_value: str, bare_lid: str, name: str, stats: LidResolveStats) -> None:
    """Rewrite a LID handle to its phone value in place, keeping the LID identity."""

    handle.value = new_value
    updates = ["value"]
    if name and not handle.display_name:
        handle.display_name = name
        updates.append("display_name")
        stats.named += 1
    handle.metadata = {**(handle.metadata or {}), "lid": bare_lid}
    updates.append("metadata")
    # Handle.save() recomputes normalized_value because "value" is in update_fields.
    handle.save(update_fields=[*updates, "updated_at"])


def _merge_into_phone_handle(handle: Any, target: Any, *, bare_lid: str, name: str, stats: LidResolveStats) -> None:
    """Merge a resolved LID handle into the phone handle that already owns its value.

    The phone handle survives (it keeps its own ``external_id``); the LID and any
    missing name are carried onto it, and the LID handle's messages, participants,
    and identity links repoint before it is deleted. Reactions and any post feeds
    keep their ``SET_NULL``/cascade behaviour — a chat sender does not own them.
    """

    updates: list[str] = []
    merged_metadata = {**(target.metadata or {}), "lid": bare_lid}
    if merged_metadata != target.metadata:
        target.metadata = merged_metadata
        updates.append("metadata")
    if name and not target.display_name:
        target.display_name = name
        updates.append("display_name")
        stats.named += 1
    if updates:
        target.save(update_fields=[*updates, "updated_at"])

    _repoint_unique(apps.get_model("messaging", "Participant"), handle, target, ("message_id", "role"))
    _repoint_unique(apps.get_model("parties", "PartyHandle"), handle, target, ("party_id",))
    apps.get_model("messaging", "Message")._base_manager.filter(sender=handle).update(sender=target)
    handle.delete()


def _repoint_unique(model: Any, from_handle: Any, to_handle: Any, conflict_fields: tuple[str, ...]) -> None:
    """Repoint ``handle`` FKs from one handle to another, honouring a unique key.

    A row whose repoint would duplicate an existing ``(to_handle, *conflict)`` row
    is dropped instead; the survivor already carries that fact. A NULL in any
    conflict field means the row's uniqueness does not apply, so it always moves.
    """

    for row in model._base_manager.filter(handle=from_handle).order_by("pk"):
        conflict = {field_name: getattr(row, field_name) for field_name in conflict_fields}
        collides = None not in conflict.values() and (model._base_manager.filter(handle=to_handle, **conflict).exists())
        if collides:
            row.delete()
        else:
            model._base_manager.filter(pk=row.pk).update(handle=to_handle)


class Command(BaseCommand):
    """Resolve a WhatsApp channel's ``@lid`` handles from its local session store."""

    help = "Resolve WhatsApp @lid sender handles to phone numbers/names from the channel's session store."

    def add_arguments(self, parser: CommandParser) -> None:
        """Register the target channel and the dry-run switch."""

        parser.add_argument("--channel", required=True, help="Target WhatsApp channel (sqid).")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing anything.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        """Read the channel's store and resolve its LID handles."""

        del args
        try:
            channel = confirmed_whatsapp_channel(options["channel"])
        except ValidationError as error:
            raise CommandError(str(error)) from error
        db_path = session_store_path(channel) / LiveSession.session_file_name
        if not db_path.exists():
            raise CommandError(
                f"No WhatsApp session store for channel {options['channel']!r} at {db_path}. "
                "Pair or sync the channel first, or run where its data dir is mounted."
            )
        try:
            resolver = StoreResolver.from_store(db_path)
        except StoreUnreadable as error:
            raise CommandError(str(error)) from error
        if not resolver.lid_to_pn:
            self.stdout.write(
                self.style.WARNING(f"The session store at {db_path} holds no LID mappings yet; nothing to resolve.")
            )
        stats = resolve_channel_lids(channel, resolver, dry_run=options["dry_run"])
        self.stdout.write(self.style.SUCCESS(stats.summary(dry_run=options["dry_run"])))
