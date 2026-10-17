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
converges a resolved LID handle into the phone handle that owns its value on the
``(platform, value)`` uniqueness (including one sync creates during the run)
through the Handle's system fold: everything that pointed at the LID handle,
its handle statements included, moves to the phone survivor while the LID stays
as an identity redirect.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple, Protocol

from django.apps import apps
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import IntegrityError, transaction
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
    refused: int = 0

    def summary(self, *, dry_run: bool) -> str:
        """Return a one-line human summary of the run."""

        verb = "Would resolve" if dry_run else "Resolved"
        return (
            f"{verb} {self.resolved}/{self.candidates} @lid handle(s): "
            f"{self.merged} merged into an existing phone handle, "
            f"{self.named} name(s) filled, {self.unresolved} left unresolved (no mapping), "
            f"{self.refused} refused by the handle merge."
        )


class LidOutcome(NamedTuple):
    """What resolving one LID handle did: folded it into a phone handle, filled a name."""

    merged: bool
    named: bool


def resolve_channel_lids(channel: Any, resolver: LidResolver, *, dry_run: bool = False) -> LidResolveStats:
    """Rewrite this channel's ``@lid`` handles through ``resolver``; return counts.

    Scoped to handles that participate in the channel's threads, so the run only
    touches the account whose store was read. Each still-unresolved LID handle
    (``value`` ends with ``@lid``) is looked up; a hit rewrites the handle to
    ``+E.164`` in place, or — when a phone handle already owns that value — merges
    into it. A miss is left for a later run to resolve, as is a handle the merge
    refuses (counted). Each handle commits on its own, and counts only once its
    outcome is known: an alias a concurrent run already resolved counts nowhere.
    """

    handle_model = apps.get_model("parties", "Handle")
    stats = LidResolveStats()
    with system_context(reason="whatsapp_resolve_lids"):
        candidates = list(
            handle_model.objects.canonical().filter(
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
            if dry_run:
                stats.resolved += 1
                _tally_dry_run(handle_model, handle, new_value=new_value, name=name, stats=stats)
                continue
            try:
                outcome = _resolve_lid_handle(handle_model, handle, new_value=new_value, bare_lid=bare_lid, name=name)
            except ValidationError:
                # Something the merge cannot move still refers to the alias; a later run retries it.
                stats.refused += 1
                continue
            if outcome is None:
                continue
            stats.resolved += 1
            stats.merged += outcome.merged
            stats.named += outcome.named
    return stats


def _resolve_lid_handle(
    handle_model: Any, handle: Any, *, new_value: str, bare_lid: str, name: str
) -> LidOutcome | None:
    """Rewrite one LID handle to its phone value, or merge it into the handle owning that value.

    Returns ``None`` when a concurrent run already resolved the alias. A phone
    handle sync creates after the lookup surfaces as the rewrite's unique-value
    collision, rolled back to a savepoint; the alias then merges into that row.
    """

    with transaction.atomic():
        target = handle_model.objects.find_identity(platform="whatsapp", value=new_value)
        target_pk = target.pk if target is not None and target.pk != handle.pk else None
        # Lock both handles in the parties identity order before anything that
        # points at them moves, then act on the locked rows: a concurrent run
        # may already have resolved this alias.
        handles, _parties = apps.get_model("parties", "Party").objects.lock_identity_rows(
            party_ids=(), handle_ids=(handle.pk,) if target_pk is None else (handle.pk, target_pk)
        )
        locked = handles.get(handle.pk)
        if locked is None or locked.merged_into_id is not None or not locked.value.endswith("@lid"):
            return None
        target = handles.get(target_pk) if target_pk is not None else None
        if target is None:
            try:
                with transaction.atomic():
                    named = _rewrite_lid_handle(locked, new_value=new_value, bare_lid=bare_lid, name=name)
                return LidOutcome(merged=False, named=named)
            except IntegrityError:
                target = handle_model.objects.find_identity(platform="whatsapp", value=new_value)
                if target is None or target.pk == locked.pk:
                    raise
        return _merge_into_phone_handle(locked, target, bare_lid=bare_lid, name=name)


def _tally_dry_run(handle_model: Any, handle: Any, *, new_value: str, name: str, stats: LidResolveStats) -> None:
    """Count the merge/name outcomes a real run would produce for one handle."""

    target = handle_model.objects.find_identity(platform="whatsapp", value=new_value)
    if target is not None and target.pk != handle.pk:
        stats.merged += 1
    if name and not handle.display_name:
        stats.named += 1


def _rewrite_lid_handle(handle: Any, *, new_value: str, bare_lid: str, name: str) -> bool:
    """Rewrite a LID handle to its phone value in place, keeping the LID identity; return whether it was named."""

    handle.value = new_value
    updates = ["value", "metadata"]
    named = bool(name and not handle.display_name)
    if named:
        handle.display_name = name
        updates.append("display_name")
    handle.metadata = {**(handle.metadata or {}), "lid": bare_lid}
    # Handle.save() recomputes normalized_value because "value" is in update_fields.
    handle.save(update_fields=[*updates, "updated_at"])
    return named


def _merge_into_phone_handle(handle: Any, target: Any, *, bare_lid: str, name: str) -> LidOutcome:
    """Merge a resolved LID handle into the phone handle that already owns its value.

    The phone handle folds the alias in: it keeps its own values (and its own
    ``external_id``), takes the ones it lacks, and receives every row that pointed
    at the alias — messages, participations, reactions, tags and handle statements,
    where every party's decisions and evidence survive. The alias remains its
    retained identity redirect. This adapter then records what only the store knows:
    the LID under ``metadata['lid']`` and the store's contact name when the phone
    handle still has none.
    """

    kept = target.fold([handle])
    updates: list[str] = []
    merged_metadata = {**(kept.metadata or {}), "lid": bare_lid}
    if merged_metadata != kept.metadata:
        kept.metadata = merged_metadata
        updates.append("metadata")
    named = bool(name and not kept.display_name)
    if named:
        kept.display_name = name
        updates.append("display_name")
    if updates:
        kept.save(update_fields=[*updates, "updated_at"])
    return LidOutcome(merged=True, named=named)


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
