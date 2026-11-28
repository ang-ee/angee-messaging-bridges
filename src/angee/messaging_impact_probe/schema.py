"""GraphQL schema contributions for messaging impact probe."""

from __future__ import annotations

import strawberry_django
from django.apps import apps
from strawberry import auto

from angee.graphql.data.hasura import AngeeHasuraWriteBackend, hasura_model_resource
from angee.graphql.node import NODE_DISPLAY_NAME_DESCRIPTION, AngeeNode

MessagingImpactProbe = apps.get_model("messaging_impact_probe", "MessagingImpactProbe")


@strawberry_django.type(MessagingImpactProbe)
class MessagingImpactProbeType(AngeeNode):
    """GraphQL projection of one messagingimpactprobe."""

    display_name: str = strawberry_django.field(
        resolver=AngeeNode.display_name, only=["title"], description=NODE_DISPLAY_NAME_DESCRIPTION
    )
    title: auto
    body: auto
    created_at: auto
    updated_at: auto


_MESSAGINGIMPACTPROBE_RESOURCE = hasura_model_resource(
    MessagingImpactProbeType,
    model=MessagingImpactProbe,
    name="messaging_impact_probe",
    filterable=["id", "title", "updated_at"],
    sortable=["title", "updated_at", "created_at"],
    aggregatable=["id"],
    groupable=["updated_at"],
    writable=["title", "body"],
    write_backend=AngeeHasuraWriteBackend(MessagingImpactProbe),
    id_column="sqid",
)


schemas = {
    "console": {
        "query": [_MESSAGINGIMPACTPROBE_RESOURCE.query],
        "mutation": [_MESSAGINGIMPACTPROBE_RESOURCE.mutation],
        "types": [MessagingImpactProbeType, *_MESSAGINGIMPACTPROBE_RESOURCE.types],
    },
}
