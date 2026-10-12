"""Historical-model proofs for vendor rename, merge, and obsolete seed retirement."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from django.db import connection, models
from django.db.migrations.state import ModelState, ProjectState

from angee.messaging_integrate_facebook.runtime_migrations import meta_vendor


def state():
    """Build historical fields only, including a second addon's vendor reference."""

    project = ProjectState()
    fields = {
        ("integrate", "Vendor"): [
            ("id", models.AutoField(primary_key=True)),
            ("slug", models.CharField(max_length=64, unique=True)),
            ("display_name", models.CharField(max_length=128)),
            ("website_url", models.URLField()),
        ],
        ("integrate", "Integration"): [
            ("id", models.AutoField(primary_key=True)),
            ("vendor", models.ForeignKey("integrate.Vendor", on_delete=models.PROTECT)),
        ],
        ("catalogue", "Product"): [
            ("id", models.AutoField(primary_key=True)),
            ("publisher", models.ForeignKey("integrate.Vendor", on_delete=models.SET_NULL, null=True)),
        ],
        ("resources", "Resource"): [
            ("id", models.AutoField(primary_key=True)),
            ("source_addon", models.CharField(max_length=200)),
            ("xref", models.CharField(max_length=160)),
            ("target_id", models.CharField(max_length=120)),
            ("content_hash", models.CharField(max_length=71)),
        ],
    }
    for (label, name), columns in fields.items():
        # Exercise clearing legacy alias ordering on the migration's own rows.
        # Related historical models must have ordering over fields they carry.
        ordering = ["sqid"] if name in {"Vendor", "Resource"} else ["pk"]
        project.add_model(ModelState(label, name, columns, options={
            "db_table": f"test_facebook_vendor_{name.lower()}", "ordering": ordering,
        }))
    return project


@contextmanager
def historical_tables(project):
    """Isolate historical tables without consulting the installed app registry."""

    registry = project.apps
    concrete = [registry.get_model(label, name) for label, name in (
        ("integrate", "Vendor"), ("integrate", "Integration"), ("catalogue", "Product"), ("resources", "Resource"),
    )]
    with connection.schema_editor() as editor:
        for model in concrete:
            editor.create_model(model)
    try:
        yield registry, concrete
    finally:
        with connection.schema_editor() as editor:
            for model in reversed(concrete):
                editor.delete_model(model)


def test_transition_guard_requires_only_catalogue_and_seed_identity_fields():
    project = state()
    assert not meta_vendor.applies(ProjectState()) and meta_vendor.applies(project)
    project.remove_model("resources", "resource")
    assert not meta_vendor.applies(project)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("already_meta", [False, True])
def test_rename_or_merge_preserves_foreign_keys_without_rewriting_other_ledger_evidence(already_meta):
    with historical_tables(state()) as (registry, concrete):
        vendor, integration, product, resource = concrete
        old = vendor.objects.create(slug="facebook", display_name="Facebook", website_url="https://facebook.com/")
        canonical = old
        if already_meta:
            canonical = vendor.objects.create(slug="meta", display_name="Meta", website_url="https://www.meta.com/")
        attached = integration.objects.create(vendor=old)
        published = product.objects.create(publisher=old)
        resource.objects.create(
            source_addon="angee.messaging_integrate_facebook", xref="facebook",
            target_id="opaque-old", content_hash="old",
        )
        retained = resource.objects.create(
            source_addon="example.catalogue", xref="company", target_id="opaque-old", content_hash="retained",
        )
        with connection.schema_editor() as editor:
            meta_vendor.adopt_meta_vendor(registry, editor)
            meta_vendor.adopt_meta_vendor(registry, editor)
        attached.refresh_from_db()
        published.refresh_from_db()
        retained.refresh_from_db()
        assert attached.vendor_id == published.publisher_id == canonical.pk
        assert list(vendor.objects.order_by().values_list("slug", flat=True)) == ["meta"]
        assert retained.target_id == "opaque-old" and retained.content_hash == "retained"
        assert not resource.objects.order_by().filter(source_addon="angee.messaging_integrate_facebook").exists()


@pytest.mark.django_db(transaction=True)
def test_obsolete_seed_is_retired_even_after_the_vendor_was_already_renamed():
    with historical_tables(state()) as (registry, concrete):
        vendor, _, _, resource = concrete
        vendor.objects.create(slug="meta", display_name="Meta", website_url="https://www.meta.com/")
        resource.objects.create(
            source_addon="angee.messaging_integrate_facebook", xref="facebook", target_id="opaque", content_hash="old",
        )
        with connection.schema_editor() as editor:
            meta_vendor.adopt_meta_vendor(registry, editor)
        assert resource.objects.order_by().count() == 0
