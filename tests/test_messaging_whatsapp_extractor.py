"""Tests for WhatsApp's iPhone-backup workflow extractor client."""

from __future__ import annotations

import hashlib
import sqlite3
import zipfile
from collections.abc import Iterator
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from angee.addons import addon_manifest
from django.apps import apps
from django.core.management import call_command
from django.db import connection
from rebac import system_context

from angee.messaging import backup_ingest
from angee.messaging_integrate_whatsapp import backup
from angee.messaging_integrate_whatsapp import extractor as extractor_module
from angee.messaging_integrate_whatsapp.autoconfig import SETTINGS as WHATSAPP_SETTINGS
from angee.messaging_integrate_whatsapp.extractor import WhatsAppIphoneBackupExtractor
from angee.resources.entries import resource_manifest_for
from angee.resources.models import Resource as AbstractResource
from tests.conftest import Vendor, _clear_model_tables, _create_missing_tables
from tests.workflows import WORKFLOW_RUNTIME_MODELS, Workflow


class ArchiveResourceLedger(AbstractResource):
    """Concrete resource ledger for the stock messaging archive workflow fixtures."""

    class Meta(AbstractResource.Meta):
        """Keep this test ledger isolated from composed runtime output."""

        abstract = False
        app_label = "base"
        db_table = "test_archive_workflow_resource"


_WORKFLOW_RESOURCE_MODELS = (Vendor, *WORKFLOW_RUNTIME_MODELS, ArchiveResourceLedger)


class _BackupArchiveFile:
    """Minimal storage.File-shaped object opening one ZIP-wrapped backup."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def open_stream(self) -> Any:
        """Open the stored backup bytes for extractor recognition/execution."""

        return self.path.open("rb")


@pytest.fixture()
def archive_workflow_resource_tables(transactional_db: Any) -> Iterator[None]:
    """Create the vendor, workflow-definition, and resource-ledger tables."""

    del transactional_db
    created_models = _create_missing_tables(_WORKFLOW_RESOURCE_MODELS)
    call_command("rebac", "sync", verbosity=0)
    _clear_model_tables(_WORKFLOW_RESOURCE_MODELS)
    try:
        yield
    finally:
        _clear_model_tables(_WORKFLOW_RESOURCE_MODELS)
        if created_models:
            with connection.schema_editor() as schema_editor:
                for model in reversed(created_models):
                    schema_editor.delete_model(model)


def test_whatsapp_iphone_backup_recognizes_manifest_resolved_chat_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recognition reads the manifest and the store header — never the store body."""

    consumed: list[int] = []

    class RecordingReader(extractor_module.BoundedReader):
        def read(self, size: int = -1) -> bytes:
            value = super().read(size)
            consumed.append(len(value))
            return value

    monkeypatch.setattr(extractor_module, "BoundedReader", RecordingReader)
    store_padding = 1024 * 1024
    archive = _minimal_backup_archive(tmp_path, store_padding=store_padding)

    assert WhatsAppIphoneBackupExtractor().recognizes(_BackupArchiveFile(archive)) is True
    # ZIP metadata + the small fixture manifest + the 16-byte header stay far
    # below the padded store body a full read would consume.
    assert sum(consumed) < store_padding // 4


def test_whatsapp_iphone_backup_rejects_foreign_archives(tmp_path: Path) -> None:
    """Recognition returns False for near-miss layouts instead of over-claiming."""

    extractor = WhatsAppIphoneBackupExtractor()

    storeless = _minimal_backup_archive(tmp_path / "storeless", include_store=False)
    assert extractor.recognizes(_BackupArchiveFile(storeless)) is False

    manifestless_dir = tmp_path / "manifestless"
    manifestless_dir.mkdir()
    manifestless = manifestless_dir / "plain.zip"
    with zipfile.ZipFile(manifestless, "w") as archive:
        archive.writestr("device/notes.txt", "not a backup")
    assert extractor.recognizes(_BackupArchiveFile(manifestless)) is False

    random_dir = tmp_path / "random"
    random_dir.mkdir()
    random_bytes = random_dir / "noise.bin"
    random_bytes.write_bytes(b"\x00\x01definitely not a zip archive\xff" * 64)
    assert extractor.recognizes(_BackupArchiveFile(random_bytes)) is False


@pytest.mark.django_db
def test_whatsapp_iphone_backup_execute_delegates_to_backup_importer_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execution resolves the confirmed WhatsApp channel and calls the importer facade."""

    target = "int_confirmed"
    channel = SimpleNamespace(sqid=target)
    resolved: list[str] = []
    monkeypatch.setattr(
        extractor_module,
        "confirmed_whatsapp_channel",
        lambda sqid: resolved.append(sqid) or channel,
    )
    calls: list[dict[str, Any]] = []

    def import_backup(
        resolved_channel: Any,
        backup_dir: Path,
        *,
        on_batch: Any,
    ) -> int:
        calls.append(
            {
                "channel": resolved_channel,
                "manifest": (backup_dir / "Manifest.db").is_file(),
            }
        )
        on_batch(4)
        return 4

    monkeypatch.setattr(backup, "import_backup", import_backup)
    heartbeats: list[bool] = []
    reporter = SimpleNamespace(heartbeat=lambda: heartbeats.append(True))
    archive = _minimal_backup_archive(tmp_path)

    result = WhatsAppIphoneBackupExtractor().execute(
        _BackupArchiveFile(archive),
        target,
        reporter,
    )

    assert resolved == [target]
    assert calls == [{"channel": channel, "manifest": True}]
    assert heartbeats == [True]
    assert result == {"channel": target, "imported": 4}


def test_whatsapp_addon_registers_extractor_and_depends_on_bridge() -> None:
    """WhatsApp contributes its vendor extractor through the bridge's settings seam."""

    config = apps.get_app_config("messaging_integrate_whatsapp")
    contract = addon_manifest(config)

    assert contract is not None
    assert "angee.workflows_integrate" in contract.depends_on
    assert WHATSAPP_SETTINGS[
        "ANGEE_WORKFLOW_ARCHIVE_EXTRACTOR_CLASSES.whatsapp_iphone_backup"
    ] == "angee.messaging_integrate_whatsapp.extractor.WhatsAppIphoneBackupExtractor"


def test_whatsapp_backup_ingest_is_historical(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backup batches use the ingest owner's suppression of live notifications."""

    channel = SimpleNamespace(subscription_state={}, _state=SimpleNamespace(adding=False, db="default"))
    message = SimpleNamespace(body=None)
    calls: list[dict[str, Any]] = []

    def ingest(messages: list[Any], **kwargs: Any) -> None:
        calls.append({"messages": list(messages), **kwargs})

    manager = SimpleNamespace(ingest=ingest)
    manager.db_manager = lambda using: manager if using == "default" else pytest.fail("Wrong write alias")
    monkeypatch.setattr(
        backup_ingest, "apps", SimpleNamespace(get_model=lambda *_args: SimpleNamespace(objects=manager))
    )
    monkeypatch.setattr(backup_ingest, "system_context", lambda **_kwargs: nullcontext())
    monkeypatch.setattr(backup, "parsed_message", lambda item: item)
    store = SimpleNamespace(messages=lambda **_kwargs: iter((message,)))

    assert backup.BackupImporter(channel, store).run() == 1
    assert calls == [{"messages": [message], "channel": channel, "quote_edges": False, "historical": True}]


@pytest.mark.parametrize(
    ("addon", "workflow_names"),
    (
        ("messaging_integrate_whatsapp", ("Archive import", "Backup import")),
        ("messaging_integrate_facebook", ("Facebook archive import", "Facebook takeout import")),
    ),
)
def test_archive_import_resource_loads_published_valid_graph(
    archive_workflow_resource_tables: None,
    addon: str,
    workflow_names: tuple[str, str],
) -> None:
    """The ledger loads, validates and idempotently publishes all stock archive graphs."""

    del archive_workflow_resource_tables
    config = apps.get_app_config(addon)
    manifest = resource_manifest_for(config)

    assert tuple(entry["path"] for entry in manifest["install"]) == (
        "resources/install/100_workflows.workflow.yaml",
        "resources/install/101_workflows.step.yaml",
        "resources/install/102_workflows.edge.yaml",
        "resources/install/110_workflows.workflow.yaml",
        "resources/install/111_workflows.step.yaml",
        "resources/install/112_workflows.edge.yaml",
    )
    result = ArchiveResourceLedger.objects.load_addons(
        (config,), tiers=[AbstractResource.Tier.INSTALL],
    )
    assert result.loaded == 19
    reloaded = ArchiveResourceLedger.objects.load_addons(
        (config,), tiers=[AbstractResource.Tier.INSTALL],
    )
    assert reloaded.loaded == 0

    with system_context(reason="test messaging archive workflow resources"):
        for name, subject in zip(workflow_names, ("storage.file", "storage.drive"), strict=True):
            assert Workflow._base_manager.filter(name=name).count() == 2
            draft = Workflow._base_manager.get(name=name, published_from__isnull=True)
            published = Workflow.objects.current_published_for(draft)
            assert published is not None
            assert published.subject_declaration == subject
            steps = {step.key: step for step in published.steps.order_by("key")}
            edges = list(published.edges.select_related("source", "target"))
            assert {key: step.step_class.value for key, step in steps.items()} == {
                "execute_unit": "archive_execute",
                "gate": "archive_gate",
                "map": "map",
                "prepare": "archive_execute",
                "probe": "archive_probe",
            }
            assert steps["prepare"].config["mode"] == "prepare"
            assert steps["map"].config["items"] == "input"
            assert steps["map"].config["target_step"] == "execute_unit"
            assert steps["execute_unit"].config["mode"] == "unit"
            assert [step.key for step in steps.values() if step.is_entry] == ["probe"]
            assert {(edge.source.key, edge.target.key, edge.condition) for edge in edges} == {
                ("gate", "prepare", "completed"),
                ("prepare", "map", "prepared"),
                ("probe", "gate", "recognized"),
            }
            for step in steps.values():
                step.full_clean()
            for edge in edges:
                edge.full_clean()


def _minimal_backup_layout(
    tmp_path: Path,
    *,
    include_store: bool = True,
    store_padding: int = 0,
) -> Path:
    """Build the smallest Manifest.db plus ChatStorage.sqlite backup fixture."""

    backup_root = tmp_path / "backup"
    backup_root.mkdir(parents=True)
    file_id = "ab" * 20
    manifest = sqlite3.connect(backup_root / "Manifest.db")
    manifest.execute("CREATE TABLE Files (fileID TEXT, domain TEXT, relativePath TEXT)")
    manifest.execute(
        "INSERT INTO Files VALUES (?, ?, ?)",
        (file_id, backup.WHATSAPP_DOMAIN, backup.CHAT_STORAGE_PATH),
    )
    manifest.commit()
    manifest.close()

    if include_store:
        chat_store = tmp_path / "ChatStorage.sqlite"
        store = sqlite3.connect(chat_store)
        store.execute("CREATE TABLE fixture (id INTEGER PRIMARY KEY)")
        store.commit()
        store.close()
        blob = backup_root / file_id[:2] / file_id
        blob.parent.mkdir()
        content = chat_store.read_bytes()
        if store_padding:
            content += b"\x00" * store_padding
        blob.write_bytes(content)
    assert file_id != hashlib.sha1(
        f"{backup.WHATSAPP_DOMAIN}-{backup.CHAT_STORAGE_PATH}".encode()
    ).hexdigest()
    return backup_root


def _minimal_backup_archive(
    tmp_path: Path,
    *,
    include_store: bool = True,
    store_padding: int = 0,
) -> Path:
    """Wrap the minimal fixture layout in the storage.File archive shape."""

    backup_root = _minimal_backup_layout(
        tmp_path, include_store=include_store, store_padding=store_padding
    )
    archive_path = tmp_path / "iphone-backup.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for path in sorted(backup_root.rglob("*")):
            if path.is_file():
                archive.write(path, arcname=(Path("device") / path.relative_to(backup_root)).as_posix())
    return archive_path
