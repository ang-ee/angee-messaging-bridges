"""Install the declared statement principals and grants into a synced test schema.

Admission is a schema fact fed by seeded install resources (``provenance.Rule``
rows, role grants); the runtime only reads them. This test host owns the bootstrap
exactly as the framework's does (``angee/tests/bootstrap.py``): it names addon
resources, which the core ``angee.testing`` plugin may not import.
"""

from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.test import override_settings
from rebac import backend, system_context

from angee.resources.entries import ResourceEntry, resource_manifest_for


def statement_sources() -> None:
    """Load parties' install declarations once per database state, masters excluded.

    A single existence query when the principals are present; a reload after a
    flush. Both relationship stores receive the grants because campaigns switch
    storage after bootstrap. Fixtures keep their own relationship vocabulary, so
    the master tier is excluded through the resource loader's native contract.
    """

    if "provenance" not in apps.app_configs or "parties" not in apps.app_configs:
        return
    if backend().schema().get_definition("provenance/role") is None:
        call_command("rebac", "sync", verbosity=0)
    rule = apps.get_model("provenance", "Rule")
    with system_context(reason="tests.statement_sources.bootstrap"):
        if rule._base_manager.exists():
            return
        addon = apps.get_app_config("parties")
        masters = tuple(
            ":".join(ResourceEntry.from_declaration(addon, "master", declaration).key)
            for declaration in resource_manifest_for(addon)["master"]
        )
        resource = apps.get_model("resources", "Resource")
        with override_settings(
            ANGEE_RESOURCE_EXCLUDED_ENTRIES=(*getattr(settings, "ANGEE_RESOURCE_EXCLUDED_ENTRIES", ()), *masters)
        ):
            for storage in ("denormalized", "registry"):
                with override_settings(REBAC_LOCAL_BACKEND_STORAGE=storage):
                    resource.objects.load_addons((addon,), tiers=("install",))
