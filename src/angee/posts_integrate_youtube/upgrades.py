"""Data upgrades this addon's release declares; projects run them from their native migrations.

Each callable is a ``RunPython`` operation over historical models (the framework's
docs/composer.md, "Data upgrades").
"""

from __future__ import annotations

from typing import Any

from django.apps.registry import Apps
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

from angee.integrate_google.providers import GoogleType
from angee.integrate_google.upgrades import align_google_client

YOUTUBE_SLUG = "youtube"
RETIRED_PROVIDER_TYPE = "google_youtube"
RETIRED_DEFAULT_SCOPES = ["openid", "email", "profile", "https://www.googleapis.com/auth/youtube.force-ssl"]
"""The default scopes the retired ``google_youtube`` preset seeded; YouTube now declares its scope itself."""
RETIRED_PROMPT = "consent"
"""The retired preset forced consent; the ``google`` preset asks for it only when it helps."""
DEMO_PLACEHOLDER = ("angee.iam_integrate_oidc", "demo_google", "demo-oidc-client")
"""Seed ledger addon, xref and client id of the demo-only ``google`` placeholder the login addon used to ship.

It was the only seed of a ``google`` client before ``integrate_google``.
"""


def move_youtube_client(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Make the YouTube addon's own Google client the deployment's ``google`` client.

    The row keeps its primary key, so its credentials, external accounts and the
    integrations bound to them stay valid with no reconnect; the ``integrate_google``
    seed then adopts it. Seeded values of the retired preset (its default scopes, a
    forced ``prompt=consent``) give way to the ``google`` preset's; operator values
    stay. An unused demo placeholder ``google`` client is deleted to make room; any
    other ``google`` client in the same environment refuses the upgrade before
    anything changes. A project's migration depends on the schema migration that gives
    ``integrate.OAuthClient`` its ``issuer`` and ``jwks_uri``.
    """

    alias = schema_editor.connection.alias
    clients = apps.get_model("integrate", "OAuthClient")._base_manager.using(alias)
    youtube = list(clients.filter(slug=YOUTUBE_SLUG).order_by("environment", "pk"))
    placeholders, clashes = [], []
    for row in youtube:
        google = clients.filter(slug="google", environment=row.environment).first()
        if google is None:
            continue
        (placeholders if _is_unused_placeholder(apps, alias, google) else clashes).append(google)
    if clashes:
        environments = ", ".join(sorted(google.environment for google in clashes))
        raise RuntimeError(
            f"Both a 'youtube' and a 'google' OAuth client exist in environment(s) {environments}; "
            "delete the 'google' client if it is an unused placeholder, or move what uses it to 'youtube', "
            "then apply this migration again. No client was changed."
        )
    for google in placeholders:
        _delete_placeholder(apps, alias, google)
    for row in youtube:
        row.slug = "google"
        _save_as_google(row, "slug")
    for row in clients.filter(provider_type=RETIRED_PROVIDER_TYPE).order_by("pk"):
        _save_as_google(row)


def _save_as_google(row: Any, *changed: str) -> None:
    """Give one row the ``google`` provider type, retire the old preset's seeded values and align it."""

    row.provider_type = GoogleType.key
    fields = [*changed, "provider_type"]
    if list(row.default_scopes or []) == RETIRED_DEFAULT_SCOPES:
        row.default_scopes = list(GoogleType.effective_defaults()["default_scopes"])
        fields.append("default_scopes")
    if (row.authorize_params or {}).get("prompt") == RETIRED_PROMPT:
        row.authorize_params = {key: value for key, value in row.authorize_params.items() if key != "prompt"}
        fields.append("authorize_params")
    fields += align_google_client(row)
    row.save(update_fields=list(dict.fromkeys(fields)))


def _is_unused_placeholder(apps: Apps, alias: str, google: Any) -> bool:
    """Whether ``google`` is the login addon's untouched demo placeholder, with nothing connected to it."""

    _addon, _xref, client_id = DEMO_PLACEHOLDER
    if google.client_id != client_id or not _placeholder_ledger(apps, alias).exists():
        return False
    for label in ("integrate.ExternalAccount", "integrate.Credential"):
        if apps.get_model(label)._base_manager.using(alias).filter(oauth_client_id=google.pk).exists():
            return False
    return True


def _placeholder_ledger(apps: Apps, alias: str) -> Any:
    """The placeholder seed's ledger rows (historical models carry no public ids to match a target by)."""

    addon, xref, _client_id = DEMO_PLACEHOLDER
    return apps.get_model("resources", "Resource")._base_manager.using(alias).filter(source_addon=addon, xref=xref)


def _delete_placeholder(apps: Apps, alias: str, google: Any) -> None:
    """Delete the placeholder client and its seed ledger rows."""

    _placeholder_ledger(apps, alias).delete()
    google.delete()
