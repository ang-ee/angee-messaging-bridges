"""OAuth vocabulary and discovery composed with integrate's discovery owner."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.apps import apps
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rebac import system_context

from angee.integrate.credentials import CredentialKind
from angee.integrate.discovery import ConnectionDiscovery, DerivedCredential
from angee.integrate.errors import IntegrationError
from angee.integrate.oauth.client import OAuthClientProtocol
from angee.integrate.streams import advance_stream
from angee.posts_integrate_facebook.backend import FacebookFeedBackend, _names_a_person
from angee.posts_integrate_facebook.oauth import MetaFacebook
from tests.conftest import Credential, ExternalAccount, OAuthClient, make_integration
from tests.test_posts_facebook import carries_auth, stream, transport
from tests.test_posts_facebook import feed as feed
from tests.test_posts_facebook import graph as graph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def oauth_feed(db):
    """Discovery starts with an already-refined OAuth credential and real account."""

    del db
    with system_context(reason="test.facebook.oauth_feed"):
        feed = make_integration(
            "facebook-oauth", model=apps.get_model("posts", "Feed"), backend_class="feed",
            feed_backend_class="facebook", kind=CredentialKind.OAUTH,
            material={"access_token": "synthetic-long-user-token"}, external_id="", config={},
        )
        account = ExternalAccount.objects.link(
            feed.credential.oauth_client, "synthetic-user-identity", owner=feed.owner, display_name="Example Operator",
        )
        credential = Credential.objects.upsert_for_user(
            feed.owner, feed.credential.oauth_client, CredentialKind.OAUTH,
            {"access_token": "synthetic-long-user-token"}, external_account=account,
        )
        feed.attach_connection(credential)
    return feed


def test_preset_refines_grants_through_the_oauth_protocol(graph):
    protocol = Mock(spec=OAuthClientProtocol)
    protocol.confidential_client_secret.return_value = "synthetic-app-secret"
    protocol.exchange_grant.return_value = graph["extended_grant"]
    tokens = {"access_token": "synthetic-short-token", "scope": "pages_show_list", "expires_in": 60}
    result = MetaFacebook.refine_grant(protocol, tokens)
    protocol.exchange_grant.assert_called_once_with(
        "fb_exchange_token", fb_exchange_token="synthetic-short-token",
        appsecret_proof=MetaFacebook.appsecret_proof("synthetic-short-token", OAuthClient(
            client_secret="synthetic-app-secret",
        )),
    )
    protocol.confidential_client_secret.assert_called_once_with()
    assert tokens["access_token"] == "synthetic-short-token"
    assert result["access_token"] == "synthetic-long-user-token" and result["scope"] == tokens["scope"]
    assert result["expires_in"] == 5184000
    assert MetaFacebook.graph_url("me") == MetaFacebook.defaults["userinfo_endpoint"]


def test_refinement_never_reuses_the_short_grant_expiry_when_the_long_grant_omits_it():
    protocol = Mock(spec=OAuthClientProtocol)
    protocol.confidential_client_secret.return_value = "synthetic-app-secret"
    protocol.exchange_grant.return_value = {"access_token": "synthetic-long-token"}
    result = MetaFacebook.refine_grant(protocol, {
        "access_token": "synthetic-short-token", "expires_in": 60, "expires_at": 123,
    })
    assert "expires_in" not in result and "expires_at" not in result


def test_refinement_supplies_the_short_token_proof_to_the_protocol(graph):
    protocol = Mock(spec=OAuthClientProtocol)
    protocol.oauth_client = SimpleNamespace(client_secret="synthetic-app-secret", is_public_client=False)
    protocol.confidential_client_secret.return_value = "synthetic-app-secret"
    protocol.exchange_grant.return_value = graph["extended_grant"]
    MetaFacebook.refine_grant(protocol, {"access_token": "synthetic-short-token"})
    proof = MetaFacebook.appsecret_proof("synthetic-short-token", protocol.oauth_client)
    protocol.exchange_grant.assert_called_once_with(
        "fb_exchange_token", fb_exchange_token="synthetic-short-token", appsecret_proof=proof,
    )
    assert len(proof) == 64


def test_profile_request_is_signed_with_the_refined_grant():
    protocol = OAuthClientProtocol(OAuthClient(client_secret="synthetic-app-secret"))
    assert MetaFacebook.userinfo_params(protocol, "synthetic-long-user-token") == {
        "appsecret_proof": MetaFacebook.appsecret_proof("synthetic-long-user-token", protocol.oauth_client),
    }
    protocol.oauth_client = OAuthClient(client_secret="")
    with pytest.raises(IntegrationError, match="no client secret"):
        MetaFacebook.userinfo_params(protocol, "synthetic-long-user-token")


def test_app_proof_matches_the_hmac_sha256_known_answer():
    client = SimpleNamespace(client_secret="\x0b" * 20, is_public_client=False)
    assert MetaFacebook.appsecret_proof("Hi There", client) == (
        "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"
    )


def test_discovery_returns_typed_facts_and_derived_material_without_exchange(oauth_feed, graph, monkeypatch):
    exchange = Mock(side_effect=AssertionError("The bridge must not exchange grants"))
    monkeypatch.setattr(OAuthClientProtocol, "exchange_grant", exchange)
    calls = transport(monkeypatch, lambda *args: graph["accounts"])
    with system_context(reason="test.facebook.discovery"):
        original = oauth_feed.credential
        material = original.material
        before = Credential.objects.count()
        result = FacebookFeedBackend(oauth_feed).discover_connection(original)
        oauth_feed.refresh_from_db()
        original.refresh_from_db()
        assert isinstance(result, ConnectionDiscovery) and isinstance(result.credential, DerivedCredential)
        assert result.data["external_id"] == result.data["handle"].external_id == "900001"
        assert result.data["display_name"] == "Example Workshop"
        assert result.credential.kind == CredentialKind.STATIC_TOKEN
        assert result.credential.material["api_key"] == "synthetic-page-token"
        assert original.external_account_id is not None
        assert oauth_feed.external_id == "" and oauth_feed.credential_id == original.pk
        assert original.material == material and Credential.objects.count() == before
    assert calls[0][1] == "me/accounts"
    assert calls[0][2]["fields"] == "id,name,access_token"
    assert carries_auth(calls[0][3]["headers"], original)
    exchange.assert_not_called()


@pytest.mark.parametrize("page_count", [0, 2])
def test_discovery_requires_one_page_without_writing_partial_identity(oauth_feed, graph, monkeypatch, page_count):
    accounts = deepcopy(graph["accounts"])
    accounts["data"] = [] if not page_count else [accounts["data"][0], {**accounts["data"][0], "id": "900002"}]
    transport(monkeypatch, lambda *args: accounts)
    with system_context(reason="test.facebook.single_page"):
        before = Credential.objects.count()
        expected = "grant this app access to exactly one" if page_count else "no managed Page"
        with pytest.raises(IntegrationError, match=expected):
            FacebookFeedBackend(oauth_feed).discover_connection(oauth_feed.credential)
        oauth_feed.refresh_from_db()
        assert oauth_feed.external_id == "" and Credential.objects.count() == before


def test_a_page_token_names_its_page_at_me_and_derives_no_material(feed, graph, monkeypatch):
    calls = transport(monkeypatch, lambda *args: graph["page_identity"])
    with system_context(reason="test.facebook.static_discovery"):
        result = FacebookFeedBackend(feed).discover_connection(feed.credential)
    assert result.credential is None
    assert result.data["external_id"] == result.data["handle"].external_id == "900001"
    assert result.data["display_name"] == "Example Workshop"
    # One read names the Page and shows it is one: only a Page carries a category.
    assert [(call[0], call[1], call[2]["fields"]) for call in calls] == [("GET", "me", "id,name,category")]
    assert carries_auth(calls[0][3]["headers"], feed.credential)


def _synced_state(feed):
    """The feed's stream positions, landed posts and record links, as a refused rediscovery must leave them.

    Page streams are event feeds, so they keep no record links of their own;
    the links are read so that none appears either.
    """

    streams = apps.get_model("integrate", "SyncStream").objects.filter(integration_id=feed.pk)
    posts = apps.get_model("messaging", "Message").objects.filter(channel=feed)
    links = apps.get_model("integrate", "RecordLink").objects.filter(stream__integration_id=feed.pk)
    return (
        sorted(streams.values_list("pk", "generation", "cursor")),
        sorted(posts.values_list("pk", "external_id", "revision")),
        sorted(links.values_list("pk", "external_key", "source_hash")),
    )


@pytest.mark.parametrize("page", ["900001", "900002"])
def test_a_reentered_page_token_must_speak_for_the_feed_page(feed, graph, monkeypatch, page):
    calls = transport(monkeypatch, lambda method, path, *args: (
        {**graph["page_identity"], "id": page} if path == "me" else graph["posts_first"]
    ))
    with system_context(reason="test.facebook.reentered_token"):
        synced = advance_stream(stream(feed), FacebookFeedBackend(feed)).stream
        before = _synced_state(feed)
        assert before[0] and before[1], "the synced page holds a cursor and landed posts"
        feed.credential.replace_material({"api_key": "synthetic-reentered-page-token"})
        feed.refresh_from_db()
        assert feed.discovery_pending
        outcome = feed.run_discovery(credential_pk=feed.credential_id, generation=feed.discovery_generation)
        feed.refresh_from_db()
        assert _synced_state(feed) == before
        assert feed.external_id == feed.handle.external_id == "900001"
        if page == "900001":
            assert outcome == {"ok": True, "reason": "applied"} and feed.discovery_ready
            return
        assert outcome == {"ok": False, "reason": "discovery-failed"}
        assert feed.last_error == "This feed is connected to another Facebook Page; create a separate feed."
        with pytest.raises(IntegrationError, match="discovery must finish"):
            feed.request_sync()
        # A run admitted before the re-entry commits nothing it reads on the other Page's token.
        with pytest.raises(IntegrationError, match="another Facebook Page") as refused:
            advance_stream(synced, FacebookFeedBackend(feed))
        assert not refused.value.transient and calls[-1][3]["headers"]["Authorization"].endswith("reentered-page-token")
        assert _synced_state(feed) == before


@pytest.mark.parametrize("answer", [{"name": "Example Workshop"}, {"id": "example-workshop"}])
def test_a_page_token_that_names_no_page_is_refused_not_matched(feed, monkeypatch, answer):
    transport(monkeypatch, lambda *args: answer)
    with system_context(reason="test.facebook.unnamed_page"):
        feed.credential.replace_material({"api_key": "synthetic-reentered-page-token"})
        feed.refresh_from_db()
        outcome = feed.run_discovery(credential_pk=feed.credential_id, generation=feed.discovery_generation)
        feed.refresh_from_db()
    assert outcome == {"ok": False, "reason": "discovery-failed"} and not feed.discovery_ready
    assert feed.last_error == (
        "Facebook did not say which Page this token belongs to; "
        "enter a Page access token or connect the Page through Meta OAuth."
    )
    assert feed.external_id == "900001"


_PERSON = "This token belongs to a person, not a Page: enter the Page's access token."


def _person(graph, shape):
    """A user token's answer at ``me``: Graph refuses the Page-only field, or omits it."""

    if shape == "refused":
        return 400, graph["person_identity"], {}
    return {"id": "600001", "name": "Ada Example"}


@pytest.mark.parametrize("shape", ["refused", "no_category"])
def test_a_person_token_on_a_first_connection_is_refused_and_records_nothing(graph, monkeypatch, shape):
    transport(monkeypatch, lambda *args: _person(graph, shape))
    with system_context(reason="test.facebook.person_first_discovery"):
        feed = make_integration(
            "facebook-person", model=apps.get_model("posts", "Feed"), backend_class="feed",
            feed_backend_class="facebook", material={"api_key": "synthetic-user-token"}, external_id="", config={},
        )
        outcome = feed.run_discovery(credential_pk=feed.credential_id, generation=feed.discovery_generation)
        feed.refresh_from_db()
        assert not apps.get_model("parties", "Handle").objects.filter(platform="facebook", value="600001").exists()
    assert outcome == {"ok": False, "reason": "discovery-failed"} and feed.last_error == _PERSON
    assert (feed.external_id, feed.handle_id, feed.live_since) == ("", None, None)


@pytest.mark.parametrize("shape", ["refused", "no_category"])
def test_a_person_token_re_entered_on_a_page_feed_is_refused_and_leaves_the_feed(feed, graph, monkeypatch, shape):
    transport(monkeypatch, lambda method, path, *args: _person(graph, shape) if path == "me" else graph["posts_first"])
    with system_context(reason="test.facebook.person_rediscovery"):
        advance_stream(stream(feed), FacebookFeedBackend(feed))
        before = _synced_state(feed)
        feed.credential.replace_material({"api_key": "synthetic-user-token"})
        feed.refresh_from_db()
        outcome = feed.run_discovery(credential_pk=feed.credential_id, generation=feed.discovery_generation)
        feed.refresh_from_db()
        assert _synced_state(feed) == before
    assert outcome == {"ok": False, "reason": "discovery-failed"} and feed.last_error == _PERSON
    assert feed.external_id == feed.handle.external_id == "900001" and feed.display_name == "Example Workshop"


def test_graph_names_a_person_only_in_its_own_wording(graph):
    """The one refusal read as a person: ``category`` asked of a User node, in Graph's words."""

    error = graph["person_identity"]["error"]
    assert error["message"] == "(#100) Tried accessing nonexisting field (category) on node type (User)"
    assert _names_a_person(error)
    for other in (
        {**error, "message": "(#100) Tried accessing nonexisting field (category) on node type (Page)"},
        {**error, "message": "(#100) Synthetic invalid parameter"},
        {**error, "code": 10},
        graph["missing"]["error"], graph["permission_denied"]["error"], None, "refused",
    ):
        assert not _names_a_person(other)


def _ambiguous(graph, shape):
    """A refusal of ``category`` that names no person: an invalid parameter, a permission error, or no Graph error."""

    if shape == "invalid_parameter":
        return {"error": {**graph["person_identity"]["error"], "message": "(#100) Synthetic invalid parameter"}}
    return graph["permission_denied"] if shape == "permission" else {}


@pytest.mark.parametrize("shape", ["invalid_parameter", "permission", "no_graph_error"])
def test_an_ambiguous_refusal_of_category_asks_again_without_it_on_a_first_connection(graph, monkeypatch, shape):
    page = {key: graph["page_identity"][key] for key in ("id", "name")}
    calls = transport(monkeypatch, lambda method, path, query, kwargs: (
        (400, _ambiguous(graph, shape), {}) if "category" in query["fields"] else page
    ))
    with system_context(reason="test.facebook.ambiguous_first_discovery"):
        feed = make_integration(
            "facebook-ambiguous", model=apps.get_model("posts", "Feed"), backend_class="feed",
            feed_backend_class="facebook", material={"api_key": "synthetic-page-token"}, external_id="", config={},
        )
        outcome = feed.run_discovery(credential_pk=feed.credential_id, generation=feed.discovery_generation)
        feed.refresh_from_db()
    # Not a person on Graph's word: it connects with what me names, as before the check.
    assert [call[2]["fields"] for call in calls] == ["id,name,category", "id,name"]
    assert outcome == {"ok": True, "reason": "applied"}
    assert feed.external_id == feed.handle.external_id == "900001" and feed.display_name == "Example Workshop"


@pytest.mark.parametrize("shape", ["invalid_parameter", "permission", "no_graph_error"])
def test_an_ambiguous_refusal_of_category_asks_again_without_it_on_rediscovery(feed, graph, monkeypatch, shape):
    page = {key: graph["page_identity"][key] for key in ("id", "name")}
    calls = transport(monkeypatch, lambda method, path, query, kwargs: (
        graph["posts_first"] if path != "me"
        else (400, _ambiguous(graph, shape), {}) if "category" in query["fields"] else page
    ))
    with system_context(reason="test.facebook.ambiguous_rediscovery"):
        advance_stream(stream(feed), FacebookFeedBackend(feed))
        before = _synced_state(feed)
        feed.credential.replace_material({"api_key": "synthetic-reentered-page-token"})
        feed.refresh_from_db()
        outcome = feed.run_discovery(credential_pk=feed.credential_id, generation=feed.discovery_generation)
        feed.refresh_from_db()
        assert _synced_state(feed) == before
    assert [call[2]["fields"] for call in calls if call[1] == "me"] == ["id,name,category", "id,name"]
    assert outcome == {"ok": True, "reason": "applied"} and feed.discovery_ready
    assert feed.external_id == "900001"


def test_a_transient_status_on_the_token_read_is_integrates_and_asks_nothing_again(feed, monkeypatch):
    """A 408 says nothing of ``category``: integrate calls it transient, and ``me`` is read once."""

    calls = transport(monkeypatch, lambda *args: (408, {}, {}))
    with system_context(reason="test.facebook.token_status_rule"), pytest.raises(IntegrationError) as caught:
        FacebookFeedBackend(feed).discover_connection(feed.credential)
    assert caught.value.transient and caught.value.public_message == "The feed is temporarily unavailable (HTTP 408)."
    assert [call[2]["fields"] for call in calls] == ["id,name,category"]


def test_a_first_connection_with_a_page_token_records_its_page(graph, monkeypatch):
    transport(monkeypatch, lambda *args: graph["page_identity"])
    with system_context(reason="test.facebook.static_first_discovery"):
        feed = make_integration(
            "facebook-token", model=apps.get_model("posts", "Feed"), backend_class="feed",
            feed_backend_class="facebook", material={"api_key": "synthetic-page-token"}, external_id="", config={},
        )
        token = feed.credential_id
        outcome = feed.run_discovery(credential_pk=token, generation=feed.discovery_generation)
        feed.refresh_from_db()
    assert outcome == {"ok": True, "reason": "applied"} and feed.discovery_ready
    assert feed.external_id == feed.handle.external_id == "900001"
    assert feed.display_name == "Example Workshop"
    assert feed.credential_id == token and feed.live_since is not None


def test_discovery_follows_an_empty_page_using_only_the_opaque_cursor(oauth_feed, graph, monkeypatch):
    def respond(method, path, query, kwargs):
        if query.get("after"):
            return graph["accounts"]
        return {"data": [], "paging": {"cursors": {"after": "account-next"}, "next": "https://untrusted.invalid/"}}

    calls = transport(monkeypatch, respond)
    with system_context(reason="test.facebook.discovery_pages"):
        result = FacebookFeedBackend(oauth_feed).discover_connection(oauth_feed.credential)
    assert result.data["external_id"] == "900001" and calls[1][2]["after"] == "account-next"


def test_discovery_owner_attaches_discovered_page_and_preserves_the_user_account(oauth_feed, graph, monkeypatch):
    transport(monkeypatch, lambda *args: graph["accounts"])
    with system_context(reason="test.facebook.discovery_apply"):
        original = oauth_feed.credential
        oauth_feed.await_discovery()
        result = oauth_feed.run_discovery(credential_pk=original.pk, generation=oauth_feed.discovery_generation)
        oauth_feed.refresh_from_db()
        assert result["ok"]
        assert oauth_feed.external_id == oauth_feed.handle.external_id == "900001"
        assert oauth_feed.credential.kind == CredentialKind.STATIC_TOKEN
        assert oauth_feed.account_id == original.external_account_id and oauth_feed.account_id is not None
        assert oauth_feed.live_since is not None


def test_discovery_generation_race_rejects_discovery_even_when_the_credential_row_is_unchanged(
    oauth_feed, graph, monkeypatch,
):
    original = oauth_feed.credential

    def reconnect(*args):
        row = type(oauth_feed).objects.get(pk=oauth_feed.pk)
        row.await_discovery()
        assert row.credential_id == original.pk
        return graph["accounts"]

    transport(monkeypatch, reconnect)
    with system_context(reason="test.facebook.generation"):
        oauth_feed.await_discovery()
        generation = oauth_feed.discovery_generation
        outcome = oauth_feed.run_discovery(credential_pk=original.pk, generation=generation)
        oauth_feed.refresh_from_db()
        assert not outcome["ok"] and oauth_feed.discovery_generation == generation + 1
        assert oauth_feed.credential_id == original.pk and oauth_feed.external_id == ""
        assert not Credential.objects.filter(user=oauth_feed.owner, kind=CredentialKind.STATIC_TOKEN).exists()


def test_discovery_throttle_leaves_the_refined_grant_unchanged_for_connect_resume(oauth_feed, graph, monkeypatch):
    throttle = graph["page_throttle"]
    transport(monkeypatch, lambda *args: (throttle["status"], throttle["body"], throttle["headers"]))
    with system_context(reason="test.facebook.discovery_throttle"):
        original_material = oauth_feed.credential.material
        with pytest.raises(IntegrationError) as raised:
            FacebookFeedBackend(oauth_feed).discover_connection(oauth_feed.credential)
        oauth_feed.credential.refresh_from_db()
        assert oauth_feed.credential.material == original_material
    assert raised.value.transient and raised.value.retry_after == timedelta(minutes=37)


def test_app_proof_uses_current_secret_after_derived_page_attachment(oauth_feed, graph, monkeypatch):
    calls = transport(monkeypatch, lambda method, path, *args: (
        graph["accounts"] if path == "me/accounts" else {"data": []}
    ))
    with system_context(reason="test.facebook.app_proof"):
        original = oauth_feed.credential
        client = original.oauth_client
        client.client_secret = "synthetic-app-secret"
        client.save(update_fields=["client_secret", "updated_at"])
        user_proof = MetaFacebook.appsecret_proof("synthetic-long-user-token", client)
        page_proof = MetaFacebook.appsecret_proof("synthetic-page-token", client)
        oauth_feed.await_discovery()
        result = oauth_feed.run_discovery(credential_pk=original.pk, generation=oauth_feed.discovery_generation)
        assert result["ok"] and oauth_feed.credential.kind == CredentialKind.STATIC_TOKEN
        assert oauth_feed.credential.oauth_client_id is None
        assert "appsecret_proof" not in oauth_feed.credential.reveal()
        backend = FacebookFeedBackend(oauth_feed)
        backend.extract(stream(oauth_feed), 100)
        client.client_secret = "synthetic-rotated-secret"
        client.save(update_fields=["client_secret", "updated_at"])
        rotated_proof = MetaFacebook.appsecret_proof("synthetic-page-token", client)
        oauth_feed.refresh_from_db()
        FacebookFeedBackend(oauth_feed).extract(stream(oauth_feed), 100)
    assert calls[2][2]["appsecret_proof"] == rotated_proof != page_proof
    assert user_proof != page_proof
    assert calls[0][2]["appsecret_proof"] == user_proof and calls[1][2]["appsecret_proof"] == page_proof


@pytest.mark.parametrize("public", [False, True])
def test_meta_signing_refuses_public_or_secretless_registrations(public):
    client = SimpleNamespace(is_public_client=public, client_secret="" if not public else "synthetic-secret")
    protocol = OAuthClientProtocol(client)
    for sign in (
        lambda: MetaFacebook.appsecret_proof("synthetic-token", client),
        lambda: MetaFacebook.refine_grant(protocol, {"access_token": "synthetic-token"}),
        lambda: MetaFacebook.userinfo_params(protocol, "synthetic-token"),
    ):
        with pytest.raises(IntegrationError, match="Public OAuth|no client secret"):
            sign()


def test_derived_page_requests_reuse_fk_descriptors_within_one_cycle(oauth_feed, graph, monkeypatch):
    transport(monkeypatch, lambda method, path, *args: (
        graph["accounts"] if path == "me/accounts" else {"data": []}
    ))
    with system_context(reason="test.facebook.proof_identity_queries"):
        oauth_feed.await_discovery()
        result = oauth_feed.run_discovery(
            credential_pk=oauth_feed.credential_id, generation=oauth_feed.discovery_generation,
        )
        assert result["ok"]
        oauth_feed.refresh_from_db()
        backend = FacebookFeedBackend(oauth_feed)
        state = stream(oauth_feed)
        with CaptureQueriesContext(connection) as first:
            backend.extract(state, 100)
        with CaptureQueriesContext(connection) as second:
            backend.extract(state, 100)
        assert sum(OAuthClient._meta.db_table in query["sql"] for query in first) == 1
        assert not any(
            table in query["sql"] for query in second
            for table in (OAuthClient._meta.db_table, Credential._meta.db_table)
        )
