"""Concrete FK targets whose addons do not yet ship reusable test models."""

from angee.intake.models import Need as AbstractNeed
from angee.knowledge.models import Link as AbstractLink
from angee.knowledge.models import Page as AbstractPage
from angee.knowledge.models import RecordBinding as AbstractRecordBinding
from angee.knowledge.models import Vault as AbstractVault
from angee.money.models import Currency as AbstractCurrency
from angee.money.models import MoneyRole as AbstractMoneyRole
from angee.proposals.models import Answer as AbstractAnswer
from angee.proposals.models import Proposal as AbstractProposal
from angee.proposals.models import ProposalsRole as AbstractProposalsRole
from angee.proposals.models import Review as AbstractReview
from angee.proposals.models import Round as AbstractRound
from angee.proposals.models import Topic as AbstractTopic


class Need(AbstractNeed):
    """intake target required by the shared framework test composition."""

    class Meta(AbstractNeed.Meta):
        abstract = False
        app_label = "intake"
        db_table = "test_intake_need"
        rebac_resource_type = "intake/need"


class Round(AbstractRound):
    """proposals target required by the shared framework test composition."""

    rebac_grantable = AbstractRound.rebac_grantable

    class Meta(AbstractRound.Meta):
        abstract = False
        app_label = "proposals"
        db_table = "test_proposals_round"
        rebac_resource_type = "proposals/round"


class Topic(AbstractTopic):
    """proposals target required by the shared framework test composition."""

    class Meta(AbstractTopic.Meta):
        abstract = False
        app_label = "proposals"
        db_table = "test_proposals_topic"
        rebac_resource_type = "proposals/topic"


class Proposal(AbstractProposal):
    """proposals target required by the shared framework test composition."""

    rebac_grantable = AbstractProposal.rebac_grantable

    class Meta(AbstractProposal.Meta):
        abstract = False
        app_label = "proposals"
        db_table = "test_proposals_proposal"
        rebac_resource_type = "proposals/proposal"


class Answer(AbstractAnswer):
    """proposals target required by the shared framework test composition."""

    rebac_grantable = AbstractAnswer.rebac_grantable

    class Meta(AbstractAnswer.Meta):
        abstract = False
        app_label = "proposals"
        db_table = "test_proposals_answer"
        rebac_resource_type = "proposals/answer"


class Review(AbstractReview):
    """proposals target required by the shared framework test composition."""

    class Meta(AbstractReview.Meta):
        abstract = False
        app_label = "proposals"
        db_table = "test_proposals_review"
        rebac_resource_type = "proposals/review"


class ProposalsRole(AbstractProposalsRole):
    """proposals target required by the shared framework test composition."""

    class Meta:
        abstract = False
        app_label = "proposals"
        managed = False
        rebac_resource_type = "proposals/role"


class Currency(AbstractCurrency):
    """money target required by the shared framework test composition."""

    class Meta(AbstractCurrency.Meta):
        abstract = False
        app_label = "money"
        db_table = "test_money_currency"
        rebac_resource_type = "money/currency"


class MoneyRole(AbstractMoneyRole):
    """money target required by the shared framework test composition."""

    class Meta(AbstractMoneyRole.Meta):
        abstract = False
        app_label = "money"
        managed = False
        rebac_resource_type = "money/role"


class Vault(AbstractVault):
    """knowledge target required by the shared framework test composition."""

    class Meta(AbstractVault.Meta):
        abstract = False
        app_label = "knowledge"
        db_table = "test_knowledge_vault"
        rebac_resource_type = "knowledge/vault"


class Page(AbstractPage):
    """knowledge target required by the shared framework test composition."""

    class Meta(AbstractPage.Meta):
        abstract = False
        app_label = "knowledge"
        db_table = "test_knowledge_page"
        rebac_resource_type = "knowledge/page"


class RecordBinding(AbstractRecordBinding):
    """knowledge target required by the shared framework test composition."""

    class Meta(AbstractRecordBinding.Meta):
        abstract = False
        app_label = "knowledge"
        db_table = "test_knowledge_record_binding"
        rebac_resource_type = "knowledge/record_binding"


class Link(AbstractLink):
    """knowledge target required by the shared framework test composition."""

    class Meta(AbstractLink.Meta):
        abstract = False
        app_label = "knowledge"
        db_table = "test_knowledge_link"
        rebac_resource_type = "knowledge/link"
