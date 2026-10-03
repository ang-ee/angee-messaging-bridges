"""Minimal Django settings for backend unit tests."""

from __future__ import annotations

from pathlib import Path

from angee.compose.autoconfig import AutoConfig
from django.apps import AppConfig

from angee import integrate


class BareComposeConfig(AppConfig):
    """Register the core composer without emitting a generated runtime."""

    name = "angee.compose"
    label = "compose"


class BareGraphQLConfig(AppConfig):
    """Register the GraphQL folder addon without process-wide ready hooks."""

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
    "angee.resources.testing",
    "tests.iam_app.TestIAMConfig",
    "angee.integrate",
    "angee.integrate_vcs",
    "angee.integrate_iphone",
    "angee.decisions",
    "angee.workflows",
    "angee.workflows_integrate",
    "angee.storage",
    "angee.parties",
    "angee.messaging",
    # Source apps required by angee.messaging.testing's composed model graph.
    "angee.knowledge",
    "angee.money",
    "angee.scheduling",
    "angee.sequence",
    "angee.spaces",
    "angee.projects",
    "angee.proposals",
    "angee.work",
    "angee.intake",
    "angee.messaging_integrate_imap",
    "angee.posts",
    "angee.messaging_integrate_whatsapp",
    "angee.messaging_integrate_imessage",
    "angee.messaging_integrate_telegram",
    "angee.messaging_integrate_meta",
    "angee.messaging_integrate_facebook",
    "angee.messaging_integrate_signal",
    "angee.messaging_integrate_matrix",
    "angee.messaging_integrate_discord",
    "angee.integrate.testing",
    "angee.workflows.testing",
]
AutoConfig.apply_installed(globals(), environment=False)

# Checkout-local so parallel git worktrees do not share one SQLite file.
# Runs within this checkout must execute sequentially. `.test-db/` is gitignored.
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
    Path(integrate.__file__).resolve().parents[2],
)
ANGEE_STORAGE_DEFAULT_DRIVE = "assets"
ANGEE_STORAGE_PROXY_UPLOAD_MAX_BYTES = 64 * 1024 * 1024
ANGEE_STORAGE_DRAFT_TTL_HOURS = 24
ANGEE_STORAGE_TRASH_TTL_DAYS = 30
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
