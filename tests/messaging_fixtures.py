"""Framework messaging models and bridge-specific integration fixtures."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from angee.base.mixins import AuditMixin, SqidMixin
from angee.base.models import AngeeModel
from django.conf import settings
from django.db import models

from angee.messaging.models import ThreadedModelMixin
from angee.messaging.testing.models import ActivityType as ActivityType
from angee.messaging.testing.models import Address as Address
from angee.messaging.testing.models import Circle as Circle
from angee.messaging.testing.models import CircleMember as CircleMember
from angee.messaging.testing.models import Directory as Directory
from angee.messaging.testing.models import Folder as Folder
from angee.messaging.testing.models import Fragment as Fragment
from angee.messaging.testing.models import Handle as Handle
from angee.messaging.testing.models import MergeVeto as MergeVeto
from angee.messaging.testing.models import Message as Message
from angee.messaging.testing.models import MessageEdge as MessageEdge
from angee.messaging.testing.models import MessageStar as MessageStar
from angee.messaging.testing.models import MessageSubtype as MessageSubtype
from angee.messaging.testing.models import Organization as Organization
from angee.messaging.testing.models import Part as Part
from angee.messaging.testing.models import Participant as Participant
from angee.messaging.testing.models import Party as Party
from angee.messaging.testing.models import PartyHandle as PartyHandle
from angee.messaging.testing.models import Person as Person
from angee.messaging.testing.models import Reaction as Reaction
from angee.messaging.testing.models import Relationship as Relationship
from angee.messaging.testing.models import RelationshipKind as RelationshipKind
from angee.messaging.testing.models import Thread as Thread
from angee.messaging.testing.models import ThreadActivity as ThreadActivity
from angee.messaging.testing.models import ThreadAttachment as ThreadAttachment
from angee.messaging.testing.models import ThreadFollower as ThreadFollower
from angee.messaging.testing.models import ThreadNotification as ThreadNotification
from angee.messaging.testing.models import TrackingValue as TrackingValue
from tests.conftest import Backend, Drive, MimeType


class ThreadedTicket(SqidMixin, AuditMixin, ThreadedModelMixin, AngeeModel):
    """Concrete model that opts into record chatter for messaging tests."""

    sqid_prefix = "tkt_"
    thread_tracking_fields = ("title", "status")
    thread_suggested_recipient_fields = ("assigned_user",)

    title = models.CharField(max_length=160)
    assigned_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    status = models.CharField(
        max_length=32,
        choices=(("open", "Open"), ("closed", "Closed")),
        default="open",
    )

    class Meta:
        """Django model options for the canonical threaded test record."""

        abstract = False
        app_label = "messaging"
        db_table = "test_messaging_threaded_ticket"

    def __str__(self) -> str:
        """Return the ticket title for the thread's title fragment."""

        return self.title


def _storage_drive(tmp_path: Path, *, owner: Any) -> Any:
    """Create the default storage drive used by attachment tests."""

    backend = Backend._base_manager.create(
        slug="local",
        label="Local",
        backend_class="local",
        backend_config={"root": str(tmp_path), "base_url": "/media/"},
    )
    MimeType._base_manager.get_or_create(
        mime_type="text/plain",
        defaults={"category": "text", "label": "Text"},
    )
    MimeType._base_manager.get_or_create(
        mime_type="application/octet-stream",
        defaults={"category": "other", "label": "Binary file"},
    )
    return Drive._base_manager.create(
        backend=backend,
        slug="assets",
        name="Assets",
        prefix="assets",
        created_by=owner,
    )
