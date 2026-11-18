"""Migration template: reclassify the source's shared Status container without moving messages.

Copy this module into the host's ``runtime/messaging/migrations/`` under the host's
next migration name, then replace the placeholder dependency below with the host's own
explicit migration (never ``__latest__``).

The historical chat key is channel-prefixed; source-native keys are also
accepted for retained older imports. Match the complete key, never a general
broadcast suffix: broadcast lists remain distinct. IDs, authors, content,
source accounts, visibility and reply/attachment edges remain untouched.

It is irreversible: the reclassified threads do not record the modality they had.
"""

from __future__ import annotations

from django.apps.registry import Apps
from django.db import migrations, router
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

from angee.base.fields.state import StateField


def forwards(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Mark the WhatsApp Status container threads with the ``status`` modality."""

    thread = apps.get_model("messaging", "Thread")
    alias = schema_editor.connection.alias
    if not router.allow_migrate_model(alias, thread):
        return
    thread._base_manager.using(alias).filter(
        platform="whatsapp",
        external_id__iregex=r"^(?:chat:[0-9]+:)?status@broadcast$",
    ).exclude(modality="status").update(modality="status")


class Migration(migrations.Migration):
    # Placeholder: the host's latest migration of ``messaging.Thread`` and its history.
    dependencies = [("messaging", "HOST_MIGRATION_WITH_THE_THREAD_MODALITY")]
    # Freeze the native field state before using its newly accepted value: RunPython
    # cannot rely on a later autodetected choices change.
    operations = [
        migrations.AlterField(
            model_name=model,
            name="modality",
            field=StateField(
                choices=[("email_thread", "Email thread"), ("direct", "Direct"),
                         ("group", "Group"), ("status", "Status updates"), ("public_thread", "Public thread")],
                db_index=True, default="email_thread", max_length=13,
            ),
        )
        for model in ("thread", "historicalthread")
    ] + [migrations.RunPython(forwards)]
