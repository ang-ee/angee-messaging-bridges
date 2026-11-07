"""iMessage iPhone-backup extraction from a mounted storage drive.

A local-folder mount indexes an unencrypted iPhone backup as ``storage.File`` rows
under a dedicated ``storage.Drive``. In reference mode the drive's bytes are the
folder on disk, so the backup resolves to a real directory and the directory
importer (:func:`~.importer.import_backup`) runs unchanged. Unlike WhatsApp there
is one store (SMS + iMessage together), so one extractor maps the backup onto its
confirmed channel.

Store and blob resolution stay owned by :mod:`.store`; this module only locates the
backup root inside the drive.
"""

from __future__ import annotations

from typing import Any

from angee.integrate_iphone.backup import BackupError, IosBackup
from angee.messaging_integrate_imessage.backend import confirmed_imessage_channel
from angee.messaging_integrate_imessage.importer import import_backup
from angee.messaging_integrate_imessage.store import has_sms_store
from angee.workflows_integrate.archive_steps import ArchiveExecutionReporter, ArchiveExtractor


class ImessageMountBackupExtractor(ArchiveExtractor):
    """Recognize an iMessage iPhone backup inside a mounted drive and import it.

    The subject is a ``storage.Drive`` produced by a local-folder mount;
    recognition succeeds when the backup's ``sms.db`` store resolves on disk, and
    import reuses the directory reader against the drive's backup root.
    """

    key = "imessage_iphone_mount"
    label = "iMessage iPhone backup (mounted)"
    subject_resource = "storage.Drive"
    target_resource = "messaging.Channel"

    def recognizes(self, subject: Any) -> bool:
        """Return whether the Messages store resolves in the drive's backup."""

        manifest = subject.local_file_path("Manifest.db")
        root = manifest.parent if manifest is not None else None
        if root is None:
            return False
        try:
            iphone = IosBackup(root)
        except BackupError:
            return False
        try:
            return has_sms_store(iphone)
        except BackupError:
            return False
        finally:
            iphone.close()

    def execute(
        self,
        subject: Any,
        target_pk: str,
        reporter: ArchiveExecutionReporter,
    ) -> dict[str, Any]:
        """Import the drive's backup Messages store into the confirmed channel."""

        channel = confirmed_imessage_channel(target_pk)
        manifest = subject.local_file_path("Manifest.db")
        root = manifest.parent if manifest is not None else None
        if root is None:
            raise BackupError("This drive does not expose an iPhone backup on disk.")
        total = import_backup(channel, root, resume=True, on_batch=lambda _total: reporter.heartbeat())
        return {"channel": str(channel.sqid), "imported": total}
