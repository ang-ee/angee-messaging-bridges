"""Bare Django source-model host with native addon settings contributions."""

from __future__ import annotations

from pathlib import Path

from django.apps import AppConfig
from django_compose.addons import enabled_apps
from django_compose.conf import apply_app_settings


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
    "axes",
    "django_celery_beat",
    "rebac",
    "simple_history",
    "angee.base",
    "angee.provenance",
    "tests.settings.BareGraphQLConfig",
    "angee.jobs",
    "angee.resources",
    "angee.resources.testing",
    "tests.iam_app.TestIAMConfig",
    "angee.integrate",
    "angee.integrate_vcs",
    "angee.decisions",
    "angee.workflows",
    "angee.workflows_integrate",
    "angee.tags",
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
    "angee.portfolio",
    "angee.proposals",
    "angee.work",
    "angee.intake",
    "angee.messaging_integrate_imap",
    "angee.posts",
    "angee.posts_integrate_youtube",
    "angee.posts_integrate_facebook",
    "angee.messaging_integrate_whatsapp",
    "angee.messaging_integrate_imessage",
    "angee.messaging_integrate_telegram",
    "angee.messaging_integrate_meta",
    "angee.messaging_integrate_facebook",
    "angee.messaging_integrate_signal",
    "angee.messaging_integrate_matrix",
    "angee.messaging_integrate_discord",
    "angee.provenance.testing",
    "angee.integrate.testing",
    "angee.workflows.testing",
]
CELERY_BROKER_URL = "memory://"

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
AUTH_USER_MODEL = "iam.User"
# Bare tests run Django's per-process LocMem cache. Production OAuth redirects
# must use a shared cache; tests opt in explicitly so the state guard remains loud.
ANGEE_INTEGRATE_ALLOW_LOCAL_OAUTH_STATE_CACHE = True
ANGEE_GRAPHQL_ALLOW_INMEMORY_CHANNEL_LAYER = True

# Ordinary Django inputs for this bare source-model host. Production activation
# and composed-host tests use native configuration/receipts in another process.
BASE_DIR = Path(__file__).resolve().parent.parent
REBAC_BACKEND = "local"
REBAC_LOCAL_BACKEND_STORAGE = "registry"
REBAC_STRICT_MODE = True
REBAC_FIELD_READ_MODE = "redact"
REBAC_ALLOW_SUDO = True
REBAC_SUPERUSER_BYPASS = False
REBAC_LINT_BARE_PREFETCH = False

# Apply source app declarations without emitting models or replacing this registry.
apply_app_settings(globals(), enabled_apps(INSTALLED_APPS, group="angee.addons"))
globals()["ANGEE_VCS_BACKEND_CLASSES"]["stub"] = "tests.conftest.StubVCSBackend"
