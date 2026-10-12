"""Retain vendor foreign keys while retiring the Facebook company catalogue row."""

from __future__ import annotations

from django.db import migrations
from django.db.migrations.state import ProjectState


def applies(project_state: ProjectState) -> bool:
    """Require the historical catalogue and obsolete seed ledger fields."""

    vendor = project_state.models.get(("integrate", "vendor"))
    resource = project_state.models.get(("resources", "resource"))
    return (
        vendor is not None and {"slug", "display_name", "website_url"} <= vendor.fields.keys()
        and resource is not None and {"source_addon", "xref"} <= resource.fields.keys()
    )


def adopt_meta_vendor(apps, schema_editor) -> None:
    """Use historical rows only; resource adoption repairs its own target evidence."""

    alias = schema_editor.connection.alias
    vendor = apps.get_model("integrate", "Vendor")
    vendors = vendor._base_manager.using(alias).order_by()
    old = vendors.filter(slug="facebook").first()
    if old is not None:
        canonical = vendors.filter(slug="meta").first()
        if canonical is None:
            vendors.filter(pk=old.pk).update(slug="meta", display_name="Meta", website_url="https://www.meta.com/")
        else:
            for model in apps.get_models(include_auto_created=True):
                for field in model._meta.local_fields:
                    if field.is_relation and field.related_model is vendor and (field.many_to_one or field.one_to_one):
                        model._base_manager.using(alias).order_by().filter(**{field.attname: old.pk}).update(
                            **{field.attname: canonical.pk},
                        )
            vendors.filter(pk=old.pk).delete()
    apps.get_model("resources", "Resource")._base_manager.using(alias).order_by().filter(
        source_addon="angee.messaging_integrate_facebook", xref="facebook",
    ).delete()


class Migration(migrations.Migration):
    """Rename before resource loading adopts the shared Meta master seed.

    Composer runs this data migration before loading master resources. The
    merge branch also handles hosts on which the Meta row was already seeded.
    """

    dependencies = [("integrate", "__latest__"), ("resources", "__latest__")]
    operations = [migrations.RunPython(adopt_meta_vendor, migrations.RunPython.noop)]
