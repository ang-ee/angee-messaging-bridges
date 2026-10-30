"""Reclassify the source's shared Status container without moving messages.

The historical chat key is channel-prefixed; source-native keys are also
accepted for retained older imports. Match the complete key, never a general
broadcast suffix: broadcast lists remain distinct. IDs, authors, content,
source accounts, visibility and reply/attachment edges remain untouched.
"""

from django.db import migrations, router

from angee.base.fields.state import StateField


def applies(state):
    return all(("messaging", model) in state.models for model in ("thread", "historicalthread"))


def forwards(apps, schema_editor):
    thread = apps.get_model("messaging", "Thread")
    alias = schema_editor.connection.alias
    if not router.allow_migrate_model(alias, thread):
        return
    thread._base_manager.using(alias).filter(
        platform="whatsapp",
        external_id__iregex=r"^(?:chat:[0-9]+:)?status@broadcast$",
    ).exclude(modality="status").update(modality="status")


class Migration(migrations.Migration):
    dependencies = [("messaging", "__latest__")]
    # Freeze the native field state before using its newly accepted value. Addon
    # transitions precede host autodetection, so RunPython cannot rely on a later
    # autodetected choices change. Both retained and fresh initial graphs work.
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
