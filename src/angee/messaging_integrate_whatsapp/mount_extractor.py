"""WhatsApp iPhone-backup extraction from a mounted storage drive.

A local-folder mount indexes an unencrypted iPhone backup as ``storage.File``
rows under a dedicated ``storage.Drive``. In reference mode the drive's bytes
are the folder on disk, so the backup resolves to a real directory and the
existing directory importer (:func:`~.backup.import_backup`) runs unchanged —
one WhatsApp app domain (personal or business) per channel.

Container and blob resolution stay owned by :mod:`.backup`; this module only
locates the backup root inside the drive and maps each detected app domain onto
its confirmed channel.
"""

from __future__ import annotations

from typing import Any, ClassVar

from angee.messaging_integrate_whatsapp import backup
from angee.messaging_integrate_whatsapp.backend import confirmed_whatsapp_channel
from angee.storage_integrate_iphone.mounts import backup_root
from angee.workflows_integrate.archive_steps import ArchiveExecutionReporter, ArchiveExtractor


class WhatsAppMountBackupExtractor(ArchiveExtractor):
    """Recognize a WhatsApp iPhone backup inside a mounted drive and import it.

    The subject is a ``storage.Drive`` produced by a local-folder mount. Each
    concrete subclass binds one WhatsApp app :attr:`domain`; recognition
    succeeds only when that domain's chat store resolves in the backup, so a
    device carrying both accounts yields two mappable rows — one per channel.
    Import reuses the directory reader against the drive's on-disk backup root.
    """

    subject_resource = "storage.Drive"
    target_resource = "messaging.Channel"
    domain: ClassVar[str] = ""

    def recognizes(self, subject: Any) -> bool:
        """Return whether this domain's chat store resolves in the drive's backup."""

        root = backup_root(subject)
        if root is None:
            return False
        try:
            iphone = backup.IosBackup(root)
        except backup.BackupError:
            return False
        try:
            return backup.has_chat_storage(iphone, self.domain)
        except backup.BackupError:
            return False
        finally:
            iphone.close()

    def execute(
        self,
        subject: Any,
        target_pk: str,
        reporter: ArchiveExecutionReporter,
    ) -> dict[str, Any]:
        """Import this domain's chats from the drive's backup into the confirmed channel."""

        channel = confirmed_whatsapp_channel(target_pk)
        root = backup_root(subject)
        if root is None:
            raise backup.BackupError("This drive does not expose an iPhone backup on disk.")
        total = backup.import_backup(
            channel,
            root,
            domain=self.domain,
            resume=True,
            on_batch=lambda _total: reporter.heartbeat(),
        )
        return {"channel": str(channel.sqid), "domain": self.domain, "imported": total}


class WhatsAppPersonalMountExtractor(WhatsAppMountBackupExtractor):
    """Personal WhatsApp store from a mounted iPhone backup."""

    key = "whatsapp_iphone_mount"
    label = "WhatsApp iPhone backup (mounted)"
    domain = backup.WHATSAPP_DOMAIN


class WhatsAppBusinessMountExtractor(WhatsAppMountBackupExtractor):
    """WhatsApp Business (SMB) store from a mounted iPhone backup."""

    key = "whatsapp_smb_iphone_mount"
    label = "WhatsApp Business iPhone backup (mounted)"
    domain = backup.WHATSAPP_SMB_DOMAIN
