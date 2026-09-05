"""Guard the generated dependency group against addon-manifest drift."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from hatch_angee import compile_dependencies, parse_manifest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ADDON_ROOTS = (PROJECT_ROOT / "addons", PROJECT_ROOT.parent / "angee" / "addons")


def _read_toml(path: Path) -> dict[str, Any]:
    """Return the TOML document at ``path`` using the standard-library parser."""

    with path.open("rb") as stream:
        return tomllib.load(stream)


def test_addon_dependency_group_matches_manifests() -> None:
    """The checked-in group is the exact union for both composed addon roots."""

    markers = sorted(
        marker
        for addon_root in ADDON_ROOTS
        for marker in addon_root.glob("**/addon.toml")
    )
    manifests = tuple(parse_manifest(marker) for marker in markers)
    expected = compile_dependencies(manifests)
    actual = _read_toml(PROJECT_ROOT / "pyproject.toml")["dependency-groups"]["addons"]

    assert tuple(actual) == expected
