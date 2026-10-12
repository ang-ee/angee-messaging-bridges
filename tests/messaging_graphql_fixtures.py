"""Bridge GraphQL fixtures; framework behavior is tested in the framework repository."""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory
from rebac import app_settings
from rebac.roles import grant

from angee.messaging.testing.models import Channel as Channel

User = get_user_model()
parties_schema = importlib.import_module("angee.parties.schema")
messaging_schema = importlib.import_module("angee.messaging.schema")
iam_schema = importlib.import_module("angee.iam.schema")
integrate_schema = importlib.import_module("angee.integrate.schema")


@pytest.fixture
def messaging_graphql_tables(composed_tables: None) -> None:
    """Use the framework's composed tables and native cleanup."""

    del composed_tables


def _platform_admin(username: str) -> Any:
    """Create a superuser holding the universal admin role."""

    admin = User.objects.create_superuser(username=username, email=f"{username}@example.com", password="admin")
    grant(actor=admin, role=app_settings.REBAC_UNIVERSAL_ADMIN_ROLE)
    return admin


def _request(user: Any) -> Any:
    """Return a console-shaped POST request bound to ``user``."""

    request = RequestFactory().post("/graphql/console/")
    request.user = user
    return request
