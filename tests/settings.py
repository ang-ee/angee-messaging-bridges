"""Minimal Django settings for backend unit tests."""

from __future__ import annotations

from pathlib import Path

from django.apps import AppConfig


class BareComposeConfig(AppConfig):
    """Register the compose core addon without emitting a generated runtime."""

    name = "angee.compose"
    label = "compose"


class BareGraphQLConfig(AppConfig):
    """Register the GraphQL core addon without process-wide ready hooks."""

    name = "angee.graphql"
    label = "graphql"

SECRET_KEY = "angee-tests"
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.postgres",
    "rebac",
    "reversion",
    "simple_history",
    "tests.settings.BareComposeConfig",
    "angee.base",
    "tests.settings.BareGraphQLConfig",
    "angee.jobs",
    "angee.resources",
    "tests.iam_app.TestIAMConfig",
    "angee.integrate",
    "angee.integrate_iphone",
    "angee.workflows",
    "angee.workflows_integrate",
    "angee.storage",
    "angee.parties",
    "angee.messaging",
    "angee.posts",
    "angee.messaging_integrate_whatsapp",
    "angee.messaging_integrate_imessage",
    "angee.messaging_integrate_telegram",
    "angee.messaging_integrate_meta",
    "angee.messaging_integrate_facebook",
    "angee.messaging_integrate_signal",
    "angee.messaging_integrate_matrix",
    "angee.messaging_integrate_discord",
]
# Checkout-local (NOT a global tempdir) so parallel git worktrees / concurrent test
# runs on the same machine never share one SQLite file and corrupt each other with
# "disk I/O error". `.test-db/` is purpose-named, gitignored, and per-checkout.
_TEST_DB_DIR = Path(__file__).resolve().parent.parent / ".test-db"
_TEST_DB_DIR.mkdir(parents=True, exist_ok=True)
_TEST_DB_FILE = str(_TEST_DB_DIR / "angee_pytest_db.sqlite3")
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        # A file-backed test DB (not ":memory:") so each thread gets its own
        # connection. Threaded session tests (Matrix/Telegram/Signal live sessions)
        # write the bridge row from a worker thread while the test's operator thread
        # writes too; production is Postgres, where those serialize on a row lock. A
        # shared in-memory SQLite connection instead raises "database table is locked".
        # With a file DB + busy timeout each writer waits for the other, matching
        # production. WAL keeps concurrent reads non-blocking.
        "NAME": _TEST_DB_FILE,
        "OPTIONS": {"timeout": 30, "init_command": "PRAGMA journal_mode=WAL;"},
        "TEST": {"NAME": _TEST_DB_FILE},
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "iam.User"
USE_TZ = True
ANGEE_RUNTIME_MODULE = "tests.runtime"
ANGEE_ADDON_DIRS = (
    Path(__file__).resolve().parent.parent / "addons",
    Path(__file__).resolve().parents[2] / "angee-base" / "addons",
)
ANGEE_STORAGE_DEFAULT_DRIVE = "assets"
ANGEE_STORAGE_PROXY_UPLOAD_MAX_BYTES = 64 * 1024 * 1024
ANGEE_STORAGE_DRAFT_TTL_HOURS = 24
ANGEE_STORAGE_TRASH_TTL_DAYS = 30
# Bare test settings do not run the composer, so the ImplClassField registries
# (normally supplied by each addon's autoconfig) are declared explicitly here;
# the enum field requires each to be non-empty at model-import time.
ANGEE_STORAGE_BACKEND_CLASSES = {
    "local": "angee.storage.backends.LocalBackend",
}
ANGEE_INTEGRATION_IMPLS = {
    "none": "angee.integrate.impl.NullIntegrationImpl",
}
ANGEE_RESOURCE_SOURCE_CLASSES = {
    "path": "angee.resources.sources.path_source",
    "url": "angee.integrate.resource_source.url_source",
}
ANGEE_VCS_BACKEND_CLASSES = {
    "local": "angee.integrate.vcs.backend.LocalVCSBackend",
}
ANGEE_WORKFLOW_STEP_CLASSES = {
    "handler": "angee.workflows.steps.HandlerStep",
    "wait": "angee.workflows.steps.WaitStep",
    "gate": "angee.workflows.steps.GateStep",
    "map": "angee.workflows.steps.MapStep",
    "archive_probe": "angee.workflows_integrate.steps.ArchiveProbeStepImpl",
    "archive_gate": "angee.workflows_integrate.steps.ArchiveGateStepImpl",
    "archive_execute": "angee.workflows_integrate.steps.ArchiveExecuteStepImpl",
}
# Directory/channel backends each addon's autoconfig normally contributes; declared
# here so the ImplClassField registries are non-empty at model-import time.
ANGEE_DIRECTORY_BACKEND_CLASSES = {
    "manual": "angee.parties.backends.ManualDirectoryBackend",
}
ANGEE_CHANNEL_BACKEND_CLASSES = {
    "manual": "angee.messaging.backends.ManualChannelBackend",
    "discord": "angee.messaging_integrate_discord.backend.DiscordChannelBackend",
    "facebook": "angee.messaging_integrate_facebook.backend.FacebookChannelBackend",
    "signal": "angee.messaging_integrate_signal.backend.SignalChannelBackend",
    "matrix": "angee.messaging_integrate_matrix.backend.MatrixChannelBackend",
    "telegram": "angee.messaging_integrate_telegram.backend.TelegramChannelBackend",
    "whatsapp": "angee.messaging_integrate_whatsapp.backend.WhatsAppChannelBackend",
    "imessage": "angee.messaging_integrate_imessage.backend.ImessageChannelBackend",
}
# Feed backends a ``posts.Feed`` may select (posts' autoconfig normally
# contributes these). ``stub`` returns canned posts queued by the posts tests.
ANGEE_POSTS_FEED_BACKEND_CLASSES = {
    "manual": "angee.posts.backends.ManualFeedBackend",
}
# OAuth provider types (normally each addon's autoconfig contributes these); the
# ImplClassField enum requires a non-empty registry at model-import time.
ANGEE_OAUTH_PROVIDER_TYPES = {
    "generic_oauth2": "angee.integrate.oauth.providers.GenericOAuth2",
}
# Bare tests run Django's per-process LocMem cache. Production OAuth redirects
# must use a shared cache; tests opt in explicitly so the state guard remains loud.
ANGEE_INTEGRATE_ALLOW_LOCAL_OAUTH_STATE_CACHE = True
ANGEE_GRAPHQL_ALLOW_INMEMORY_CHANNEL_LAYER = True
STRAWBERRY_DJANGO = {
    # Mirror the composer-owned public ID contract for source-addon tests that
    # bypass compose settings.
    "DEFAULT_PK_FIELD_NAME": "sqid",
    "MAP_AUTO_ID_AS_GLOBAL_ID": False,
}
