"""Native collection and fixture-addon distribution contracts."""

from __future__ import annotations

import tomllib
from pathlib import Path

from django_compose.addons import enabled_apps, installed_addons
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from angee.graphql.schema import GraphQLSchemas
from tests.conftest import make_addon

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


def test_fixture_addons_rediscover_native_requirement_edges() -> None:
    """The shared writer feeds native closure and capability readers immediately."""

    first = make_addon(schemas={"native_fixture": {}})
    assert first.name in installed_addons("angee.addons").providers
    second = make_addon(depends_on=(first.name,))
    closure = enabled_apps((second.name,), group="angee.addons")
    assert closure.entries == (first.name, second.name)
    assert closure.dependencies[second.name] == (first.name,)
    assert GraphQLSchemas((first,)).names() == ("native_fixture",)
