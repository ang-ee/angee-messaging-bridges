"""Import-only Facebook channel backend."""

from __future__ import annotations

from typing import ClassVar

from angee.messaging.backends import ChannelBackend
from angee.provenance.statements import SourceKind


class FacebookChannelBackend(ChannelBackend):
    """Channel target populated by Facebook takeout imports, never live polling.

    The channel is the identified takeout integration: friend facts it lands are
    statements it asserts as an IMPORT source, on links in its ``connections`` stream.
    """

    key = "facebook"
    label = "Facebook"
    icon = "message-square"
    source_kind = SourceKind.IMPORT
    defaults = {"vendor": "meta"}

    quote_edges: ClassVar[bool] = False
