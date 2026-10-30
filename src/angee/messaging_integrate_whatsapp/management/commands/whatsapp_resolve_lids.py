"""Resolve WhatsApp hidden LIDs through the backend's admitted identity owner."""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser

from angee.integrate.live import session_store_path
from angee.integrate.session import LiveSession
from angee.messaging_integrate_whatsapp.backend import (
    StoreResolver,
    StoreUnreadable,
    confirmed_whatsapp_channel,
    resolve_channel_lids,
)


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
