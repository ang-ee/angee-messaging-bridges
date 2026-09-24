"""Validate installed archive workflows against the framework's public owners."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from angee.workflows.graph import WorkflowGraph
from angee.workflows_integrate.steps import ArchiveExtractor
from django.utils.module_loading import import_string

from tests.workflows import Edge, Step, Workflow

_ADDONS = Path(__file__).resolve().parents[1] / "addons" / "angee"


@pytest.mark.parametrize("addon", ("messaging_integrate_whatsapp", "messaging_integrate_facebook"))
@pytest.mark.parametrize("prefix", (100, 110))
def test_installed_archive_workflow_is_ready(addon: str, prefix: int) -> None:
    """Every published graph names valid operations and binds their admitted inputs."""

    directory = _ADDONS / addon / "resources" / "install"

    def rows(offset: int, model: str) -> list[dict]:
        path = directory / f"{prefix + offset}_workflows.{model}.yaml"
        return yaml.safe_load(path.read_text())["rows"]

    workflow = Workflow(pk=1, **rows(0, "workflow")[0]["fields"])
    steps = []
    by_xref = {}
    for index, row in enumerate(rows(1, "step"), start=1):
        fields = dict(row["fields"])
        fields.pop("workflow")
        step = Step(pk=index, workflow=workflow, **fields)
        step.clean()
        step.resolve_impl("step_class").validate_config(step.config)
        steps.append(step)
        by_xref[f"{addon}.{row['xref']}"] = step
    edges = []
    for index, row in enumerate(rows(2, "edge"), start=1):
        fields = dict(row["fields"])
        fields.pop("workflow")
        edges.append(
            Edge(
                pk=index,
                workflow=workflow,
                source=by_xref[fields.pop("source")],
                target=by_xref[fields.pop("target")],
                **fields,
            )
        )

    assert WorkflowGraph.from_rows(workflow, steps, edges).diagnostics() == ()


@pytest.mark.parametrize(
    "addon",
    (
        "messaging_integrate_whatsapp",
        "messaging_integrate_facebook",
        "messaging_integrate_imessage",
        "messaging_integrate_telegram",
    ),
)
def test_registered_archive_extractors_resolve(addon: str) -> None:
    """Each bridge contributes a concrete extractor at its declared stable key."""

    settings = import_string(f"angee.{addon}.autoconfig.SETTINGS")
    prefix = "ANGEE_WORKFLOW_ARCHIVE_EXTRACTOR_CLASSES."
    extractors = {key.removeprefix(prefix): path for key, path in settings.items() if key.startswith(prefix)}
    assert extractors
    for key, path in extractors.items():
        extractor = import_string(path)
        assert issubclass(extractor, ArchiveExtractor)
        assert extractor.key == key
        assert isinstance(extractor(), ArchiveExtractor)
