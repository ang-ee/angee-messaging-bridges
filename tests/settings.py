"""Minimal Django settings for backend unit tests."""

from __future__ import annotations

from pathlib import Path

from django.apps import AppConfig


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
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "iam.User"
USE_TZ = True
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

# Ordinary Django inputs for this bare source-model host. Production activation
# and composed-host tests use native configuration/receipts in another process.
BASE_DIR = Path(__file__).resolve().parent.parent
ANGEE_CHANGESET_MODEL = "provenance.Changeset"
ANGEE_GRAPHQL_WRITE_BACKEND_ADAPTER = "angee.provenance.graphql.write_backend"
SIMPLE_HISTORY_HISTORY_CHANGE_REASON_USE_TEXT_FIELD = True
REBAC_BACKEND = "local"
REBAC_LOCAL_BACKEND_STORAGE = "registry"
REBAC_STRICT_MODE = True
REBAC_FIELD_READ_MODE = "redact"
REBAC_ALLOW_SUDO = True
REBAC_SUPERUSER_BYPASS = False
REBAC_LINT_BARE_PREFETCH = False
ANGEE_MERGE_GUARDS = ["angee.resources.managers.seeded_record_merge_guard"]
ANGEE_TIMELINE_KINDS = ["angee.provenance.timeline.timeline_kinds"]
STATIC_URL = "/static/"

# The fixture roster owns these explicit implementation selections.
ANGEE_CHANNEL_BACKEND_CLASSES = {
    "email": "angee.messaging.email.AnymailEmailChannelBackend",
    "manual": "angee.messaging.backends.ManualChannelBackend",
    "webform": "angee.messaging.backends.WebformChannelBackend",
    "imap": "angee.messaging_integrate_imap.backend.ImapChannelBackend",
    "feed": "angee.posts.backends.FeedChannelBackend",
    "whatsapp": "angee.messaging_integrate_whatsapp.backend.WhatsAppChannelBackend",
    "imessage": "angee.messaging_integrate_imessage.backend.ImessageChannelBackend",
    "telegram": "angee.messaging_integrate_telegram.backend.TelegramChannelBackend",
    "facebook": "angee.messaging_integrate_facebook.backend.FacebookChannelBackend",
    "signal": "angee.messaging_integrate_signal.backend.SignalChannelBackend",
    "matrix": "angee.messaging_integrate_matrix.backend.MatrixChannelBackend",
    "discord": "angee.messaging_integrate_discord.backend.DiscordChannelBackend",
}
ANGEE_DIRECTORY_BACKEND_CLASSES = {"manual": "angee.parties.backends.ManualDirectoryBackend"}
ANGEE_KNOWLEDGE_RETRIEVAL_CLASSES = {"lexical": "angee.knowledge.retrieval.LexicalRetrievalBackend"}
ANGEE_OAUTH_PROVIDER_TYPE_CLASSES = {
    "generic_oauth2": "angee.integrate.oauth.providers.GenericOAuth2",
    "google_youtube": "angee.posts_integrate_youtube.oauth.GoogleYouTube",
    "meta_facebook": "angee.posts_integrate_facebook.oauth.MetaFacebook",
}
ANGEE_POSTS_FEED_BACKEND_CLASSES = {
    "manual": "angee.posts.backends.ManualFeedBackend",
    "youtube": "angee.posts_integrate_youtube.backend.YouTubeFeedBackend",
    "facebook": "angee.posts_integrate_facebook.backend.FacebookFeedBackend",
}
ANGEE_RESOURCE_ENTRY_KIND_CLASSES = {
    "workflows.workflowextension": "angee.workflows.resources.WorkflowExtensionResourceKind"
}
ANGEE_RESOURCE_SOURCE_CLASSES = {
    "path": "angee.resources.sources.PathSource",
    "url": "angee.integrate.resource_source.UrlSource",
}
ANGEE_STORAGE_BACKEND_CLASSES = {"local": "angee.storage.backends.LocalBackend"}
ANGEE_VCS_BACKEND_CLASSES = {
    "local": "angee.integrate_vcs.backend.LocalVCSBackend",
    "stub": "tests.conftest.StubVCSBackend",
}
ANGEE_WORKFLOW_ARCHIVE_EXTRACTOR_CLASSES = {
    "whatsapp_iphone_backup": "angee.messaging_integrate_whatsapp.extractor.WhatsAppIphoneBackupExtractor",
    "whatsapp_iphone_mount": "angee.messaging_integrate_whatsapp.mount_extractor.WhatsAppPersonalMountExtractor",
    "whatsapp_smb_iphone_mount": "angee.messaging_integrate_whatsapp.mount_extractor.WhatsAppBusinessMountExtractor",
    "imessage_iphone_backup": "angee.messaging_integrate_imessage.extractor.ImessageIphoneBackupExtractor",
    "imessage_iphone_mount": "angee.messaging_integrate_imessage.mount_extractor.ImessageMountBackupExtractor",
    "telegram_takeout": "angee.messaging_integrate_telegram.extractor.TelegramTakeoutExtractor",
    "facebook_takeout": "angee.messaging_integrate_facebook.extractor.FacebookTakeoutExtractor",
    "facebook_takeout_mount": "angee.messaging_integrate_facebook.mount_extractor.FacebookMountTakeoutExtractor",
}
ANGEE_WORKFLOW_STEP_CLASSES = {
    "ask_decision": "angee.workflows.decision_steps.AskDecision",
    "map": "angee.workflows.maps.Map",
    "await_run": "angee.workflows.awaits.AwaitRun",
    "integrate_stream": "angee.workflows_integrate.steps.StreamStage",
    "integrate_rescan": "angee.workflows_integrate.steps.Rescan",
    "integrate_conflicts": "angee.workflows_integrate.steps.ConflictReview",
    "archive_probe": "angee.workflows_integrate.archive_steps.ArchiveProbe",
    "archive_gate": "angee.workflows_integrate.archive_steps.ArchiveGate",
    "archive_execute": "angee.workflows_integrate.archive_steps.ArchiveExecute",
    "archive_summary": "angee.workflows_integrate.archive_steps.ArchiveSummary",
}
ANGEE_WORKFLOW_TRIGGER_SOURCE_CLASSES = {"record_changed": "angee.workflows.triggers.RecordChanged"}
