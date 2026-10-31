"""SDK-free backup admission and WhatsApp native watermark conversion."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.core.management.base import CommandError

from angee.messaging_integrate_facebook.management.commands.facebook_import import Command as FacebookImport
from angee.messaging_integrate_imessage.management.commands.imessage_import import Command as ImessageImport
from angee.messaging_integrate_whatsapp.backup import CORE_DATA_EPOCH, BackupImporter
from angee.messaging_integrate_whatsapp.management.commands.whatsapp_import import Command as WhatsAppImport


@pytest.mark.parametrize("command", [FacebookImport, ImessageImport, WhatsAppImport])
def test_count_only_import_refuses_new_target_before_any_persistence(command):
    with pytest.raises(CommandError, match="existing --channel"):
        command()._channel({"dry_run": True, "create": "New account", "channel": None})


def test_whatsapp_resume_uses_shared_datetimes_and_only_converts_native_identity(monkeypatch):
    channel = SimpleNamespace(subscription_state={})
    calls = []
    def watermarks(target, *, reason):
        calls.append(target)
        return {"12345@S.WHATSAPP.NET": CORE_DATA_EPOCH + timedelta(seconds=12)}
    monkeypatch.setattr("angee.messaging_integrate_whatsapp.backup.thread_watermarks", watermarks)
    importer = BackupImporter(channel, SimpleNamespace(), resume=True)
    assert importer._resume_watermarks() == {"12345@s.whatsapp.net": 12.0}
    assert calls == [channel]


def test_whatsapp_failed_sqlite_open_closes_backup(monkeypatch):
    """The reader owns its manifest even if SQLite refuses the resolved blob."""
    from unittest.mock import Mock

    import pytest

    from angee.messaging_integrate_whatsapp import backup as module

    backup = Mock()
    backup.blob_path.return_value = "/missing/chat.sqlite"

    def refuse(*args, **kwargs):
        raise module.sqlite3.OperationalError("cannot open")

    monkeypatch.setattr(module.sqlite3, "connect", refuse)
    with pytest.raises(module.sqlite3.OperationalError, match="cannot open"):
        module.ChatStorage(backup)
    backup.close.assert_called_once_with()


def test_whatsapp_identity_command_loads_canonical_backend():
    from django.core.management import load_command_class

    command = load_command_class("angee.messaging_integrate_whatsapp", "whatsapp_resolve_lids")
    assert command.help
