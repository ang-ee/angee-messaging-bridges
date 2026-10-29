"""WhatsApp iPhone-backup client for the archive workflow bridge."""

from __future__ import annotations

import sqlite3
import zipfile
from pathlib import PurePosixPath
from typing import Any

from angee.messaging_integrate_whatsapp import backup
from angee.messaging_integrate_whatsapp.backend import confirmed_whatsapp_channel
from angee.storage.archives import (
    ArchiveError,
    stage_subtree,
)
from angee.storage_integrate_iphone.archives import backup_manifest, recognizes_backup
from angee.workflows_integrate.archive_steps import ArchiveExecutionReporter, ArchiveExtractor


class WhatsAppIphoneBackupExtractor(ArchiveExtractor):
    """Recognize ZIP-wrapped iPhone backups and delegate them to WhatsApp ingest.

    Archive recognition is bounded, not header-only: resolving the store
    requires materializing ``Manifest.db`` (capped at 64MB) per probe before
    the 16-byte store-header check; the store body is never read.
    """

    key = "whatsapp_iphone_backup"
    label = "WhatsApp iPhone backup"
    target_resource = "messaging.Channel"

    def recognizes(self, file: Any) -> bool:
        """Spot-check the manifest-resolved chat store without parsing messages."""

        return recognizes_backup(file, domain=backup.WHATSAPP_DOMAIN, relative_path=backup.CHAT_STORAGE_PATH)

    def execute(
        self,
        file: Any,
        target_pk: str,
        reporter: ArchiveExecutionReporter,
    ) -> dict[str, Any]:
        """Import through the existing backup importer into the confirmed channel."""

        channel = confirmed_whatsapp_channel(target_pk)
        total = _import_archive(file, channel=channel, reporter=reporter)
        return {"channel": str(channel.sqid), "imported": total}


def _import_archive(
    file: Any,
    *,
    channel: Any,
    reporter: ArchiveExecutionReporter,
) -> int:
    """Safely stage one ZIP-wrapped backup and call the importer owner."""

    try:
        with file.open_stream() as stream, zipfile.ZipFile(stream) as archive:
            manifest_name = backup_manifest(
                archive, domain=backup.WHATSAPP_DOMAIN, relative_path=backup.CHAT_STORAGE_PATH,
            )
            if manifest_name is None:
                raise backup.BackupError("This archive contains no WhatsApp iPhone backup.")
            parent = PurePosixPath(manifest_name).parent
            with stage_subtree(archive, parent) as backup_root:
                return backup.import_backup(
                    channel,
                    backup_root,
                    on_batch=lambda _total: reporter.heartbeat(),
                )
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise backup.BackupError("This file is not a readable ZIP backup archive.") from error
    except ArchiveError as error:
        raise backup.BackupError(str(error)) from error
    except sqlite3.DatabaseError as error:
        raise backup.BackupError("The WhatsApp chat store in this backup is corrupt or unreadable.") from error
