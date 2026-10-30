"""iMessage iPhone-backup client for the archive workflow bridge."""

from __future__ import annotations

import sqlite3
import zipfile
from pathlib import PurePosixPath
from typing import Any, BinaryIO, cast

from angee.integrate_iphone.archives import backup_manifest

from angee.integrate_iphone.backup import (
    BackupError,
)
from angee.messaging_integrate_imessage.backend import confirmed_imessage_channel
from angee.messaging_integrate_imessage.importer import import_backup
from angee.messaging_integrate_imessage.store import SMS_DOMAIN, SMS_PATH
from angee.storage.archives import (
    RECOGNITION_READ_LIMIT,
    ArchiveError,
    BoundedReader,
    archive_entries,
    stage_subtree,
)
from angee.workflows_integrate.archive_steps import ArchiveExecutionReporter, ArchiveExtractor


class ImessageIphoneBackupExtractor(ArchiveExtractor):
    """Recognize ZIP-wrapped iPhone backups and delegate them to iMessage ingest.

    Archive recognition is bounded, not header-only: resolving the store requires
    materializing ``Manifest.db`` (capped at 64MB) per probe before the 16-byte
    store-header check; the store body is never read.
    """

    key = "imessage_iphone_backup"
    label = "iMessage iPhone backup"
    target_resource = "messaging.Channel"

    def recognizes(self, file: Any) -> bool:
        """Spot-check the manifest-resolved Messages store without parsing messages."""

        try:
            with file.open_stream() as stream:
                bounded = BoundedReader(stream, limit=RECOGNITION_READ_LIMIT)
                with zipfile.ZipFile(cast(BinaryIO, bounded)) as archive:
                    def reset_budget():
                        bounded.remaining = RECOGNITION_READ_LIMIT
                    return backup_manifest(
                        archive, archive_entries(archive), domain=SMS_DOMAIN, relative_path=SMS_PATH,
                        before_candidate=reset_budget,
                    ) is not None
        except (ArchiveError, BackupError, NotImplementedError, OSError, sqlite3.DatabaseError,
                zipfile.BadZipFile, zipfile.LargeZipFile):
            return False

    def execute(
        self,
        file: Any,
        target_pk: str,
        reporter: ArchiveExecutionReporter,
    ) -> dict[str, Any]:
        """Import through the backup importer into the confirmed channel."""

        channel = confirmed_imessage_channel(target_pk)
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
                archive, archive_entries(archive), domain=SMS_DOMAIN, relative_path=SMS_PATH,
            )
            if manifest_name is None:
                raise BackupError("This archive contains no iMessage iPhone backup.")
            parent = PurePosixPath(manifest_name).parent
            with stage_subtree(archive, parent) as backup_root:
                return import_backup(
                    channel,
                    backup_root,
                    on_batch=lambda _total: reporter.heartbeat(),
                )
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise BackupError("This file is not a readable ZIP backup archive.") from error
    except ArchiveError as error:
        raise BackupError(str(error)) from error
    except sqlite3.DatabaseError as error:
        raise BackupError("The Messages store in this backup is corrupt or unreadable.") from error
