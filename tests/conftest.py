"""Shared pytest infrastructure for source-addon tests."""

from __future__ import annotations

import itertools
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

from django.apps import AppConfig
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.db import connection, models
from django.test import RequestFactory
from rebac import actor_context, system_context

from angee.graphql.schema import SCHEMA_PART_KEYS, GraphQLSchemas
from angee.integrate.credentials import CredentialKind
from angee.integrate.models import Credential as AbstractCredential
from angee.integrate.models import ExternalAccount as AbstractExternalAccount
from angee.integrate.models import Integration as AbstractIntegration
from angee.integrate.models import OAuthClient as AbstractOAuthClient
from angee.integrate.models import Repository as AbstractRepository
from angee.integrate.models import Source as AbstractSource
from angee.integrate.models import Template as AbstractTemplate
from angee.integrate.models import VcsBridge as AbstractVcsBridge
from angee.integrate.models import Vendor as AbstractVendor
from angee.integrate.models import WebhookSubscription as AbstractWebhookSubscription
from angee.integrate.vcs.backend import RepoDescriptor, TreeEntry, VCSBackend
from angee.posts.models import PostMetrics as AbstractPostMetrics
from angee.storage.models import Backend as AbstractStorageBackend
from angee.storage.models import Drive as AbstractDrive
from angee.storage.models import File as AbstractFile
from angee.storage.models import FileAttachment as AbstractFileAttachment
from angee.storage.models import Folder as AbstractFolder
from angee.storage.models import MimeType as AbstractMimeType
from angee.storage.models import StorageRole as AbstractStorageRole


class OAuthClient(AbstractOAuthClient):
    """Concrete OAuth client used by bridge tests."""

    class Meta(AbstractOAuthClient.Meta):
        """Django model options for the canonical test OAuth client."""

        abstract = False
        app_label = "integrate"
        db_table = "test_integrate_oauth_client"
        rebac_resource_type = "integrate/oauth_client"
        rebac_id_attr = "sqid"


class ExternalAccount(AbstractExternalAccount):
    """Concrete integration external account used by source-addon tests."""

    class Meta(AbstractExternalAccount.Meta):
        """Django model options for the canonical test external account."""

        abstract = False
        app_label = "integrate"
        db_table = "test_integrate_external_account"
        rebac_resource_type = "integrate/external_account"
        rebac_id_attr = "sqid"


class Credential(AbstractCredential):
    """Concrete integration credential used by source-addon tests."""

    class Meta(AbstractCredential.Meta):
        """Django model options for the canonical test credential."""

        abstract = False
        app_label = "integrate"
        db_table = "test_integrate_credential"
        rebac_resource_type = "integrate/credential"
        rebac_id_attr = "sqid"


class Vendor(AbstractVendor):
    """Concrete integration vendor catalogue row used by source-addon tests."""

    class Meta(AbstractVendor.Meta):
        """Django model options for the canonical test vendor."""

        abstract = False
        app_label = "integrate"
        db_table = "test_integrate_vendor"
        rebac_resource_type = "integrate/vendor"
        rebac_id_attr = "sqid"


class Integration(AbstractIntegration):
    """Concrete integration used by source-addon tests."""

    class Meta(AbstractIntegration.Meta):
        """Django model options for the canonical test integration."""

        abstract = False
        app_label = "integrate"
        db_table = "test_integrate_integration"
        rebac_resource_type = "integrate/integration"
        rebac_id_attr = "sqid"


class WebhookSubscription(AbstractWebhookSubscription):
    """Concrete integrate webhook subscription used by source-addon tests."""

    class Meta(AbstractWebhookSubscription.Meta):
        """Django model options for the canonical test webhook subscription."""

        abstract = False
        app_label = "integrate"
        db_table = "test_integrate_webhook_subscription"
        rebac_resource_type = "integrate/webhook_subscription"
        rebac_id_attr = "sqid"


IAM_CONNECTION_TEST_MODELS = (OAuthClient, ExternalAccount, Credential)
"""Concrete integration connection models created on demand by connection test fixtures."""

INTEGRATE_TEST_MODELS = (Vendor, Integration)
"""Concrete integration catalogue/integration models created on demand by integrate fixtures."""


class VcsBridge(AbstractVcsBridge, Integration):
    """Concrete VCS bridge used by source-addon tests.

    ``angee.integrate.schema`` binds the VCS console types at import time via
    ``apps.get_model``, so the concrete models live here (imported before any test
    module) rather than in a single test file — otherwise importing the schema from
    one test depends on another test having been collected first.
    """

    class Meta(AbstractVcsBridge.Meta):
        """Django model options for the canonical test VCS bridge."""

        abstract = False
        app_label = "integrate"
        db_table = "test_integrate_vcs_bridge"
        rebac_resource_type = "integrate/vcs_bridge"
        rebac_id_attr = "sqid"


class Repository(AbstractRepository):
    """Concrete repository used by source-addon tests."""

    class Meta(AbstractRepository.Meta):
        """Django model options for the canonical test repository."""

        abstract = False
        app_label = "integrate"
        db_table = "test_integrate_repository"
        rebac_resource_type = "integrate/repository"
        rebac_id_attr = "sqid"


class Source(AbstractSource):
    """Concrete source used by source-addon tests."""

    class Meta(AbstractSource.Meta):
        """Django model options for the canonical test source."""

        abstract = False
        app_label = "integrate"
        db_table = "test_integrate_source"
        rebac_resource_type = "integrate/source"
        rebac_id_attr = "sqid"


class Template(AbstractTemplate):
    """Concrete template used by source-addon tests."""

    source_kind = "template"

    class Meta(AbstractTemplate.Meta):
        """Django model options for the canonical test template."""

        abstract = False
        app_label = "integrate"
        db_table = "test_integrate_template"
        rebac_resource_type = "integrate/template"
        rebac_id_attr = "sqid"


VCS_TEST_MODELS = (VcsBridge, Repository, Source, Template)
"""Concrete VCS inventory models created on demand by VCS test fixtures."""

def make_integration(
    slug: str,
    *,
    kind: Any = CredentialKind.STATIC_TOKEN,
    material: dict[str, Any] | None = None,
    impl_class: str = "none",
    backend_class: str | None = None,
    model: type[Any] = Integration,
    **attrs: Any,
) -> Any:
    """Create the iam credential chain and an integration model row for tests.

    Builds owner → OAuth client → credential → vendor → model row. ``kind``/
    ``material`` pick the credential kind (default a static token); pass
    ``kind=CredentialKind.OAUTH`` for an OAuth-backed integration. ``model`` may
    be a concrete MTI child such as ``VcsBridge``; VCS child rows choose
    ``backend_class`` while parent-only integrations choose ``impl_class``.
    """

    if material is None:
        material = {"access_token": "token"} if kind == CredentialKind.OAUTH else {"api_key": "x"}
    user_model = get_user_model()
    with system_context(reason="test integrate integration setup"):
        user = user_model.objects.create_user(username=f"{slug}-owner", email=f"{slug}@example.com")
        oauth_client = OAuthClient.objects.create(
            slug=slug,
            display_name=slug.title(),
            client_id=f"{slug}-cid",
        )
        credential = Credential.objects.upsert_for_user(user, oauth_client, kind, material)
        vendor = Vendor.objects.create(slug=slug, display_name=slug.title())
        values = {
            "vendor": vendor,
            "credential": credential,
            "owner": user,
            "lifecycle": "connected",
            **attrs,
        }
        field_names = {field.name for field in model._meta.fields}
        if "backend_class" in field_names:
            values["backend_class"] = backend_class or ("local" if impl_class == "none" else impl_class)
        else:
            values["impl_class"] = impl_class
        return model.objects.create(**values)


class StubVCSBackend(VCSBackend):
    """In-memory VCS backend for tests; canned data rides on ``VcsBridge.config``.

    Registered as the ``stub`` key in the test ``ANGEE_VCS_BACKEND_CLASSES`` so a
    ``VcsBridge(backend_class="stub")`` resolves to it. Each test injects
    ``stub_repos``/``stub_tree``/``stub_blobs`` through the bridge config.
    """

    repository_search_scope_config_key = "stub_org"

    def ls_repos(self, *, org: str = "") -> list[RepoDescriptor]:
        """Return the configured repositories (filtered to ``org`` when given)."""

        repos = [RepoDescriptor(**spec) for spec in self.bridge.config.get("stub_repos", [])]
        return [repo for repo in repos if not org or repo.org == org]

    def get_repo(self, name: str) -> RepoDescriptor:
        """Return one configured repository by name or raise."""

        for spec in self.bridge.config.get("stub_repos", []):
            if spec["name"] == name:
                return RepoDescriptor(**spec)
        raise FileNotFoundError(name)

    def search_repos(self, query: str, *, org: str = "") -> list[RepoDescriptor]:
        """Return configured repositories whose name contains ``query``."""

        return [repo for repo in self.ls_repos(org=org) if query in repo.name]

    def ls_tree(self, repository: Any, *, ref: str, path: str, recursive: bool = False) -> list[TreeEntry]:
        """Return the configured tree entries under ``path``."""

        del repository, ref, recursive
        prefix = path.strip("/")
        entries = [TreeEntry(**spec) for spec in self.bridge.config.get("stub_tree", [])]
        return [entry for entry in entries if not prefix or entry.path == prefix or entry.path.startswith(f"{prefix}/")]

    def cat_file(self, repository: Any, *, ref: str, path: str) -> bytes:
        """Return the configured blob bytes for ``path`` or raise."""

        del repository, ref
        blobs = self.bridge.config.get("stub_blobs", {})
        if path in blobs:
            return str(blobs[path]).encode("utf-8")
        raise FileNotFoundError(path)

    def rev_parse(self, repository: Any, ref: str) -> str:
        """Return a fixed stub commit oid."""

        del repository, ref
        return "stubsha"

    def verify_webhook(self, vcs_bridge: Any, request: Any) -> bool:
        """Accept every webhook in tests."""

        del vcs_bridge, request
        return True


class Backend(AbstractStorageBackend):
    """Concrete storage backend used by source-addon tests."""

    class Meta(AbstractStorageBackend.Meta):
        """Django model options for the canonical test storage backend."""

        abstract = False
        app_label = "storage"
        db_table = "test_storage_backend"
        rebac_resource_type = "storage/backend"
        rebac_id_attr = "sqid"


class Drive(AbstractDrive):
    """Concrete storage drive used by source-addon tests."""

    class Meta(AbstractDrive.Meta):
        """Django model options for the canonical test drive."""

        abstract = False
        app_label = "storage"
        db_table = "test_storage_drive"
        rebac_resource_type = "storage/drive"
        rebac_id_attr = "sqid"


class Folder(AbstractFolder):
    """Concrete storage folder used by source-addon tests."""

    class Meta(AbstractFolder.Meta):
        """Django model options for the canonical test folder."""

        abstract = False
        app_label = "storage"
        db_table = "test_storage_folder"
        rebac_resource_type = "storage/folder"
        rebac_id_attr = "sqid"


class MimeType(AbstractMimeType):
    """Concrete MIME type used by source-addon tests."""

    class Meta(AbstractMimeType.Meta):
        """Django model options for the canonical test MIME type."""

        abstract = False
        app_label = "storage"
        db_table = "test_storage_mimetype"


class File(AbstractFile):
    """Concrete storage file used by source-addon tests."""

    class Meta(AbstractFile.Meta):
        """Django model options for the canonical test file."""

        abstract = False
        app_label = "storage"
        db_table = "test_storage_file"
        rebac_resource_type = "storage/file"
        rebac_id_attr = "sqid"


class FileAttachment(AbstractFileAttachment):
    """Concrete polymorphic file edge used by storage tests."""

    class Meta(AbstractFileAttachment.Meta):
        """Django model options for the canonical test file attachment."""

        abstract = False
        app_label = "storage"
        db_table = "test_storage_file_attachment"
        rebac_resource_type = "storage/file_attachment"
        rebac_id_attr = "sqid"


class StorageRole(AbstractStorageRole):
    """Concrete table-less REBAC anchor for the ``storage/role`` namespace.

    The composer emits this anchor in the runtime; the bare test env must
    register it too so the const-backed ``admin`` arm of ``storage/role`` (reached
    for a non-member through ``storage/backend``/``storage/drive``'s
    ``manager->effective_member``) resolves to a deny instead of raising
    ``SchemaError``. ``managed = False`` — never a table, only a type anchor.
    """

    class Meta(AbstractStorageRole.Meta):
        """Django model options for the canonical test storage role anchor."""

        abstract = False
        managed = False
        app_label = "storage"
        rebac_resource_type = "storage/role"


STORAGE_TEST_MODELS = (Backend, Drive, Folder, MimeType, File, FileAttachment)
"""Concrete storage models created on demand by storage test fixtures."""


class PostMetrics(AbstractPostMetrics):
    """Concrete rolled-up engagement counters used by posts tests."""

    class Meta(AbstractPostMetrics.Meta):
        """Django model options for the canonical test post metrics."""

        abstract = False
        managed = False
        app_label = "posts"
        db_table = "test_posts_post_metrics"
        rebac_resource_type = "posts/post_metrics"
        rebac_id_attr = "sqid"


def _create_missing_tables(
    test_models: tuple[type[models.Model], ...] = IAM_CONNECTION_TEST_MODELS,
) -> list[type[models.Model]]:
    """Create concrete source-addon test tables when pytest did not sync them."""

    existing_tables = set(connection.introspection.table_names())
    missing = []
    for model in test_models:
        if model._meta.db_table in existing_tables:
            continue
        missing.append(model)
        existing_tables.add(model._meta.db_table)
    if not missing:
        return []
    with connection.schema_editor() as schema_editor:
        for model in missing:
            schema_editor.create_model(model)
    return missing


def _clear_model_tables(test_models: tuple[type[models.Model], ...]) -> None:
    """Delete rows from schema-editor-created model tables without dropping them.

    Source-addon tests share concrete unmanaged tables across modules. Keeping the
    schema lets post-migrate hooks see registered models; clearing rows before
    pytest-django flushes the managed tables prevents dangling FKs and uniqueness
    leaks when a later fixture reuses an already-created table.
    """

    existing_tables = set(connection.introspection.table_names())
    table_names = []
    for model in test_models:
        table_name = model._meta.db_table
        if table_name not in existing_tables:
            continue
        table_names.append(table_name)
        for field in model._meta.many_to_many:
            through_table_name = field.remote_field.through._meta.db_table
            if through_table_name in existing_tables:
                table_names.append(through_table_name)

    if not table_names:
        return

    with connection.constraint_checks_disabled(), connection.cursor() as cursor:
        for table_name in reversed(tuple(dict.fromkeys(table_names))):
            cursor.execute(f"DELETE FROM {connection.ops.quote_name(table_name)}")


def create_user(username: str) -> Any:
    """Create one plain test user."""

    return get_user_model().objects.create_user(username=username, password=username)


_TEST_ADDON_SEQ = itertools.count()


def make_addon(
    *,
    schemas: dict[str, Any] | None = None,
    depends_on: tuple[str, ...] = (),
    name: str | None = None,
) -> AppConfig:
    """Return a fake AppConfig backed by a real tmp ``addon.toml`` (+ schema module).

    Bridges the old in-memory test idiom to the addon.toml contract: ``schemas`` is
    exposed through a registered ``<name>.schema`` module that the manifest's
    ``schemas = "schema.schemas"`` reference resolves to, and ``depends_on`` is written
    straight into the manifest. So the readers (the manifest is their sole source)
    see exactly what the test declares.
    """

    name = name or f"tests._addon_{next(_TEST_ADDON_SEQ)}"
    tmp = Path(tempfile.mkdtemp())
    module = ModuleType(name)
    module.__file__ = str(tmp / "apps.py")
    setattr(module, "__path__", [str(tmp)])
    sys.modules[name] = module

    body = ["[addon]", f'name = "{name}"']
    if depends_on:
        body.append("depends_on = [" + ", ".join(f'"{dep}"' for dep in depends_on) + "]")
    if schemas is not None:
        schema_module = ModuleType(f"{name}.schema")
        setattr(schema_module, "schemas", schemas)
        sys.modules[f"{name}.schema"] = schema_module
        body.append('schemas = "schema.schemas"')
    (tmp / "addon.toml").write_text("\n".join(body) + "\n")

    return AppConfig(name, module)


def SchemaAddon(schemas: dict[str, dict[str, tuple[object, ...]]]) -> AppConfig:  # noqa: N802 - kept for call sites
    """Build an addon stand-in whose manifest exposes the given GraphQL schemas."""

    return make_addon(schemas=schemas)



def addon_schema(schemas: dict[str, Any], name: str) -> Any:
    """Build one addon-only GraphQL schema from its raw ``schemas`` mapping."""

    parts = {key: tuple(schemas[name].get(key, ())) for key in SCHEMA_PART_KEYS}
    return GraphQLSchemas([SchemaAddon({name: parts})]).build(name)


def graphql_request(user: Any) -> Any:
    """Return a bare POST request carrying ``user``."""

    request = RequestFactory().post("/graphql/public/")
    request.user = user
    return request


def execute_schema(
    schema: Any,
    query: str,
    variables: dict[str, Any] | None = None,
    *,
    user: Any | None = None,
    request: Any | None = None,
) -> Any:
    """Execute a GraphQL operation with a request-shaped context."""

    request = request or graphql_request(user or AnonymousUser())
    actor = getattr(request, "user", AnonymousUser())
    with actor_context(actor):
        return schema.execute_sync(
            query,
            variable_values=variables or {},
            context_value=SimpleNamespace(request=request),
        )


def result_data(result: Any) -> dict[str, Any]:
    """Return result data after asserting the operation succeeded."""

    assert result.errors is None, result.errors
    assert result.data is not None
    return cast(dict[str, Any], result.data)


def assert_private_hasura_insert_access(
    schema: Any,
    *,
    creator: Any,
    outsider: Any,
    create_mutation: str,
    create_root: str,
    detail_query: str,
    detail_root: str,
    update_mutation: str,
    update_root: str,
    create_variables: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Execute and assert the private-row create/read/write/denied-read contract."""

    assert not creator.is_superuser
    created = result_data(
        execute_schema(
            schema,
            create_mutation,
            create_variables,
            user=creator,
        )
    )[create_root]
    variables = {"id": created["id"]}
    readable = result_data(
        execute_schema(
            schema,
            detail_query,
            variables,
            user=creator,
        )
    )[detail_root]
    updated = result_data(
        execute_schema(
            schema,
            update_mutation,
            variables,
            user=creator,
        )
    )[update_root]
    denied = result_data(
        execute_schema(
            schema,
            detail_query,
            variables,
            user=outsider,
        )
    )[detail_root]
    assert denied is None
    return created, readable, updated
