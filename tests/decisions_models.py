"""Concrete decision models for the bare bridge test database."""

from angee.decisions import models as sources
from angee.workflows.models import DecisionWorkflow


class DecisionGroup(sources.DecisionGroup):
    """Retained group referenced by archive review runs."""

    class Meta(sources.DecisionGroup.Meta):
        abstract = False
        app_label = "decisions"
        db_table = "test_decisions_group"
        rebac_resource_type = "decisions/group"


class Decision(DecisionWorkflow, sources.Decision):
    """Decision seat with the installed workflow contribution."""

    class Meta(sources.Decision.Meta):
        abstract = False
        app_label = "decisions"
        db_table = "test_decisions_decision"
        rebac_resource_type = "decisions/decision"


class DecisionEvidence(sources.DecisionEvidence):
    """Evidence projection linked to a decision seat."""

    class Meta(sources.DecisionEvidence.Meta):
        abstract = False
        app_label = "decisions"
        db_table = "test_decisions_evidence"
        rebac_resource_type = "decisions/evidence"
