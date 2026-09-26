"""Concrete messaging and parties models shared by bridge integration tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from angee.base.mixins import AuditMixin, SqidMixin
from angee.base.models import AngeeModel
from django.conf import settings
from django.db import models

from angee.messaging.models import Fragment as AbstractFragment
from angee.messaging.models import Message as AbstractMessage
from angee.messaging.models import MessageEdge as AbstractMessageEdge
from angee.messaging.models import MessageStar as AbstractMessageStar
from angee.messaging.models import MessageSubtype as AbstractMessageSubtype
from angee.messaging.models import Part as AbstractPart
from angee.messaging.models import Participant as AbstractParticipant
from angee.messaging.models import Reaction as AbstractReaction
from angee.messaging.models import Thread as AbstractThread
from angee.messaging.models import ThreadActivity as AbstractThreadActivity
from angee.messaging.models import ThreadAttachment as AbstractThreadAttachment
from angee.messaging.models import ThreadedModelMixin
from angee.messaging.models import ThreadFollower as AbstractThreadFollower
from angee.messaging.models import ThreadNotification as AbstractThreadNotification
from angee.messaging.models import TrackingValue as AbstractTrackingValue
from angee.parties.models import Address as AbstractAddress
from angee.parties.models import Circle as AbstractCircle
from angee.parties.models import CircleMember as AbstractCircleMember
from angee.parties.models import Directory as AbstractDirectory
from angee.parties.models import Folder as AbstractContactFolder
from angee.parties.models import Handle as AbstractHandle
from angee.parties.models import MergeVeto as AbstractMergeVeto
from angee.parties.models import Organization as AbstractOrganization
from angee.parties.models import Party as AbstractParty
from angee.parties.models import PartyHandle as AbstractPartyHandle
from angee.parties.models import Person as AbstractPerson
from angee.parties.models import Relationship as AbstractRelationship
from angee.parties.models import RelationshipKind as AbstractRelationshipKind
from angee.posts.models import MessagePublic, ThreadPublic
from tests.conftest import (
    Backend,
    Drive,
    Integration,
    MimeType,
)

_PartyHandleMeta = getattr(AbstractPartyHandle, "Meta", object)
_OrganizationMeta = getattr(AbstractOrganization, "Meta", object)
_PersonMeta = getattr(AbstractPerson, "Meta", object)
_AddressMeta = getattr(AbstractAddress, "Meta", object)


class Directory(AbstractDirectory, Integration):
    """Concrete contacts directory (Integration child) used by messaging tests."""

    class Meta(AbstractDirectory.Meta):
        """Django model options for the canonical test directory."""

        abstract = False
        app_label = "parties"
        db_table = "test_parties_directory"
        rebac_resource_type = "parties/directory"
        rebac_id_attr = "sqid"


class Folder(AbstractContactFolder):
    """Concrete parties folder used by messaging tests."""

    class Meta(AbstractContactFolder.Meta):
        """Django model options for the canonical test contacts folder."""

        abstract = False
        app_label = "parties"
        db_table = "test_parties_folder"
        rebac_resource_type = "parties/folder"
        rebac_id_attr = "sqid"


class Party(AbstractParty):
    """Concrete party used by messaging tests."""

    class Meta(AbstractParty.Meta):
        """Django model options for the canonical test party."""

        abstract = False
        app_label = "parties"
        db_table = "test_parties_party"
        rebac_resource_type = "parties/party"
        rebac_id_attr = "sqid"


class Organization(AbstractOrganization, Party):
    """Concrete organization matching the composer inheritance shape."""

    class Meta(_OrganizationMeta):
        """Django model options for the canonical test organization."""

        abstract = False
        app_label = "parties"
        db_table = "test_parties_organization"
        rebac_resource_type = "parties/organization"
        rebac_id_attr = "sqid"


class Handle(AbstractHandle):
    """Concrete handle (a message sender/recipient) used by messaging tests."""

    class Meta(AbstractHandle.Meta):
        """Django model options for the canonical test handle."""

        abstract = False
        app_label = "parties"
        db_table = "test_parties_handle"
        rebac_resource_type = "parties/handle"
        rebac_id_attr = "sqid"


class Person(AbstractPerson, Party):
    """Concrete person used when messaging attributes a user-owned handle."""

    class Meta(_PersonMeta):
        """Django model options for the canonical test person."""

        abstract = False
        app_label = "parties"
        db_table = "test_parties_person"
        rebac_resource_type = "parties/person"
        rebac_id_attr = "sqid"


class MergeVeto(AbstractMergeVeto):
    """Concrete keep-separate pair used by parties-schema imports across the suite."""

    class Meta(AbstractMergeVeto.Meta):
        """Django model options for the canonical test merge veto."""

        abstract = False
        app_label = "parties"
        db_table = "test_parties_merge_veto"
        rebac_resource_type = "parties/merge_veto"
        rebac_id_attr = "sqid"


class Address(AbstractAddress):
    """Concrete party address used by contact-ingest tests."""

    class Meta(_AddressMeta):
        """Django model options for the canonical test address."""

        abstract = False
        app_label = "parties"
        db_table = "test_parties_address"
        rebac_resource_type = "parties/address"
        rebac_id_attr = "sqid"


class PartyHandle(AbstractPartyHandle):
    """Concrete identity link used when messaging attributes a user-owned handle."""

    class Meta(_PartyHandleMeta):
        """Django model options for the canonical test party-handle."""

        abstract = False
        app_label = "parties"
        db_table = "test_parties_party_handle"
        rebac_resource_type = "parties/party_handle"
        rebac_id_attr = "sqid"


class Circle(AbstractCircle):
    """Concrete circle used by parties-schema imports across the suite."""

    class Meta(AbstractCircle.Meta):
        """Django model options for the canonical test circle."""

        abstract = False
        app_label = "parties"
        db_table = "test_parties_circle"
        rebac_resource_type = "parties/circle"
        rebac_id_attr = "sqid"


class CircleMember(AbstractCircleMember):
    """Concrete circle membership used by parties-schema imports across the suite."""

    class Meta(AbstractCircleMember.Meta):
        """Django model options for the canonical test circle membership."""

        abstract = False
        app_label = "parties"
        db_table = "test_parties_circle_member"
        rebac_resource_type = "parties/circle_member"
        rebac_id_attr = "sqid"


class RelationshipKind(AbstractRelationshipKind):
    """Concrete relationship kind used by parties-schema imports across the suite."""

    class Meta(AbstractRelationshipKind.Meta):
        """Django model options for the canonical test relationship kind."""

        abstract = False
        app_label = "parties"
        db_table = "test_parties_relationship_kind"
        rebac_resource_type = "parties/relationship_kind"
        rebac_id_attr = "sqid"


class Relationship(AbstractRelationship):
    """Concrete relationship edge used by parties-schema imports across the suite."""

    class Meta(AbstractRelationship.Meta):
        """Django model options for the canonical test relationship."""

        abstract = False
        app_label = "parties"
        db_table = "test_parties_relationship"
        rebac_resource_type = "parties/relationship"
        rebac_id_attr = "sqid"


class Fragment(AbstractFragment):
    """Concrete content-addressed fragment used by messaging tests.

    Unscoped substrate (no REBAC type), like the abstract source model.
    """

    class Meta(AbstractFragment.Meta):
        """Django model options for the canonical test fragment."""

        abstract = False
        app_label = "messaging"
        db_table = "test_messaging_fragment"


class Thread(ThreadPublic, AbstractThread):
    """Concrete thread used by messaging tests.

    Folds spaces' group pointer and posts' public-post payload onto the one table,
    mirroring the composer output for the installed base addons.
    """

    class Meta(AbstractThread.Meta):
        """Django model options for the canonical test thread."""

        abstract = False
        app_label = "messaging"
        db_table = "test_messaging_thread"
        rebac_resource_type = "messaging/thread"
        rebac_id_attr = "sqid"


class ThreadAttachment(AbstractThreadAttachment):
    """Concrete record-thread attachment used by messaging tests."""

    class Meta(AbstractThreadAttachment.Meta):
        """Django model options for the canonical test thread attachment."""

        abstract = False
        app_label = "messaging"
        db_table = "test_messaging_thread_attachment"
        rebac_resource_type = "messaging/thread_attachment"
        rebac_id_attr = "sqid"


class ThreadFollower(AbstractThreadFollower):
    """Concrete record-thread follower used by messaging tests."""

    class Meta(AbstractThreadFollower.Meta):
        """Django model options for the canonical test thread follower."""

        abstract = False
        app_label = "messaging"
        db_table = "test_messaging_thread_follower"
        rebac_resource_type = "messaging/thread_follower"
        rebac_id_attr = "sqid"


class ThreadActivity(AbstractThreadActivity):
    """Concrete record-thread activity used by messaging tests."""

    class Meta(AbstractThreadActivity.Meta):
        """Django model options for the canonical test thread activity."""

        abstract = False
        app_label = "messaging"
        db_table = "test_messaging_thread_activity"
        rebac_resource_type = "messaging/thread_activity"
        rebac_id_attr = "sqid"


class MessageSubtype(AbstractMessageSubtype):
    """Concrete message subtype used by messaging tests."""

    class Meta(AbstractMessageSubtype.Meta):
        """Django model options for the canonical test message subtype."""

        abstract = False
        app_label = "messaging"
        db_table = "test_messaging_message_subtype"


class Message(MessagePublic, AbstractMessage):
    """Concrete message used by messaging tests.

    Folds posts' same-row ``MessagePublic`` extension (``is_original_post``) onto
    the one table, the way the composer emits ``Message(MessageExtension1,
    AbstractMessage)`` now that posts is a composed base addon.
    """

    class Meta(AbstractMessage.Meta):
        """Django model options for the canonical test message."""

        abstract = False
        app_label = "messaging"
        db_table = "test_messaging_message"
        rebac_resource_type = "messaging/message"
        rebac_id_attr = "sqid"


class ThreadNotification(AbstractThreadNotification):
    """Concrete notification used by messaging tests."""

    class Meta(AbstractThreadNotification.Meta):
        """Django model options for the canonical test notification."""

        abstract = False
        app_label = "messaging"
        db_table = "test_messaging_thread_notification"
        rebac_resource_type = "messaging/thread_notification"
        rebac_id_attr = "sqid"


class Reaction(AbstractReaction):
    """Concrete message reaction used by messaging tests."""

    class Meta(AbstractReaction.Meta):
        """Django model options for the canonical test reaction."""

        abstract = False
        app_label = "messaging"
        db_table = "test_messaging_reaction"
        rebac_resource_type = "messaging/reaction"
        rebac_id_attr = "sqid"


class MessageStar(AbstractMessageStar):
    """Concrete message star used by messaging tests."""

    class Meta(AbstractMessageStar.Meta):
        """Django model options for the canonical test message star."""

        abstract = False
        app_label = "messaging"
        db_table = "test_messaging_message_star"
        rebac_resource_type = "messaging/message_star"
        rebac_id_attr = "sqid"


class TrackingValue(AbstractTrackingValue):
    """Concrete tracking value used by messaging tests."""

    class Meta(AbstractTrackingValue.Meta):
        """Django model options for the canonical test tracking value."""

        abstract = False
        app_label = "messaging"
        db_table = "test_messaging_tracking_value"
        rebac_resource_type = "messaging/tracking_value"
        rebac_id_attr = "sqid"


class Part(AbstractPart):
    """Concrete message body part used by messaging tests."""

    class Meta(AbstractPart.Meta):
        """Django model options for the canonical test part."""

        abstract = False
        app_label = "messaging"
        db_table = "test_messaging_part"
        rebac_resource_type = "messaging/part"
        rebac_id_attr = "sqid"


class MessageEdge(AbstractMessageEdge):
    """Concrete cross-message edge used by messaging tests."""

    class Meta(AbstractMessageEdge.Meta):
        """Django model options for the canonical test message edge."""

        abstract = False
        app_label = "messaging"
        db_table = "test_messaging_message_edge"
        rebac_resource_type = "messaging/message_edge"
        rebac_id_attr = "sqid"


class Participant(AbstractParticipant):
    """Concrete participant used by messaging tests."""

    class Meta(AbstractParticipant.Meta):
        """Django model options for the canonical test participant."""

        abstract = False
        app_label = "messaging"
        db_table = "test_messaging_participant"
        rebac_resource_type = "messaging/participant"
        rebac_id_attr = "sqid"


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
