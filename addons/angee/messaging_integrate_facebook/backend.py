"""Import-only Facebook channel backend."""

from __future__ import annotations

from typing import ClassVar

from angee.messaging.backends import ChannelBackend


class FacebookChannelBackend(ChannelBackend):
    """Channel target populated by Facebook takeout imports, never live polling."""

    key = "facebook"
    label = "Facebook"
    icon = "message-square"
    defaults = {"vendor": "meta"}

    quote_edges: ClassVar[bool] = False
