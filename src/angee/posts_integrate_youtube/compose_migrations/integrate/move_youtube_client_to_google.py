"""Make the YouTube addon's own Google client the deployment's ``google`` client.

The row keeps its primary key, so its credentials, external accounts and the
integrations bound to them stay valid with no reconnect. A project runs
``move_youtube_client`` from a native data migration that depends on the schema
migration giving ``integrate.OAuthClient`` its ``issuer`` and ``jwks_uri``; the
``integrate_google`` seed then adopts the row.
"""

from django.db import migrations

from angee.integrate_google.compose_migrations.integrate.align_google_clients import align_google_client
from angee.integrate_google.compose_migrations.integrate.align_google_clients import applies as applies

YOUTUBE_SLUG = "youtube"
RETIRED_PROVIDER_TYPE = "google_youtube"


def move_youtube_client(apps, schema_editor):
    clients = apps.get_model("integrate", "OAuthClient")._base_manager.using(schema_editor.connection.alias)
    youtube = list(clients.filter(slug=YOUTUBE_SLUG).order_by("environment", "pk"))
    clashes = sorted(
        row.environment for row in youtube if clients.filter(slug="google", environment=row.environment).exists()
    )
    if clashes:
        raise RuntimeError(
            f"Both a 'youtube' and a 'google' OAuth client exist in environment(s) {', '.join(clashes)}; "
            "reconnect the YouTube channels through 'google' and delete 'youtube' before applying this "
            "migration. No client was changed."
        )
    for row in youtube:
        row.slug = "google"
        row.provider_type = "google"
        row.save(update_fields=["slug", "provider_type", *align_google_client(row)])
    for row in clients.filter(provider_type=RETIRED_PROVIDER_TYPE).order_by("pk"):
        row.provider_type = "google"
        row.save(update_fields=["provider_type", *align_google_client(row)])


class Migration(migrations.Migration):
    dependencies = []
    operations = [migrations.RunPython(move_youtube_client, migrations.RunPython.noop)]
