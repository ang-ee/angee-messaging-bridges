"""Repository-wide attribution boundary for the bridges' pytest host.

Mirrors the framework's root ``conftest.py``: every captured write needs an open
changeset, and a pytest host is an entrypoint like a request or a Celery task.
"""

from collections.abc import Iterator

import pytest
from angee.base.changesets import changeset
from pytest_django.django_compat import is_django_unittest

from tests import conftest as source_models  # noqa: F401 -- register the shared source graph before database setup

pytest_plugins = ("angee.testing.fixtures", "tests.messaging_graphql_fixtures")


@pytest.fixture(autouse=True)
def captured_changeset(request: pytest.FixtureRequest) -> Iterator[None]:
    """Carry entrypoint defaults through the bridge tests, including native TestCases.

    The boundary declares no activity, so an explicit inner boundary refines it
    instead of conflicting. It depends on the test's database fixture so it is torn
    down — and its changeset closed — while database access is still allowed.
    Statement principals and their grants belong to the test's permission schema:
    ``composed_tables`` installs them after its REBAC sync.
    """

    names = set(request.fixturenames)
    if not is_django_unittest(request) and (
        request.node.get_closest_marker("django_db") or {"db", "transactional_db", "composed_tables"} & names
    ):
        request.getfixturevalue("transactional_db" if {"transactional_db", "composed_tables"} & names else "db")
    with changeset(fresh=True, change_reason="pytest"):
        yield
