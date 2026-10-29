"""Keep the collection dependency cohort aligned with its native uv members."""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_collection_requires_each_workspace_addon() -> None:
    """uv resolves transitive requirements; the collection names every member."""

    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    expected = {}
    for pattern in project["tool"]["uv"]["workspace"]["members"]:
        for member in sorted(PROJECT_ROOT.glob(pattern)):
            metadata = tomllib.loads((member / "pyproject.toml").read_text())["project"]
            name = canonicalize_name(metadata["name"])
            assert name not in expected, f"duplicate workspace distribution: {name}"
            expected[name] = f"=={metadata['version']}"
    requirements = [Requirement(value) for value in project["project"]["dependencies"]]
    assert len(requirements) == len(expected)
    assert {canonicalize_name(item.name): str(item.specifier) for item in requirements} == expected
