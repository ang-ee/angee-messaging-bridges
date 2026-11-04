"""Method-only provider composition over the shared source-test Channel table.

These native proxies exercise provider row behavior without importing external
addons into the framework's reusable test support. Their proxy model labels
are test-only; they do not prove native compiler discovery/MRO/resource identity.
The companion composed-host test owns that production composition proof.
"""

from angee.messaging.testing.models import Channel
from angee.messaging_integrate_discord.models import DiscordChannelState


class DiscordChannel(DiscordChannelState, Channel):
    """Same concrete row/manager with the Discord method-only contribution."""

    class Meta:
        proxy = True
        app_label = "messaging_integrate_discord"
