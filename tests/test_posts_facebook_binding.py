"""OAuth vocabulary and discovery composed with integrate's binding owner."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.apps import apps
from rebac import system_context

from angee.integrate.credentials import CredentialKind
from angee.integrate.discovery import ConnectionDiscovery, DerivedCredential
from angee.integrate.errors import IntegrationError
from angee.integrate.oauth.client import OAuthClientProtocol
from angee.posts_integrate_facebook.backend import FacebookFeedBackend
from angee.posts_integrate_facebook.oauth import MetaFacebook
from tests.conftest import Credential, ExternalAccount, make_integration
from tests.test_posts_facebook import graph as graph
from tests.test_posts_facebook import stream, transport

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def oauth_feed(composed_tables):
    """Discovery starts with an already-refined OAuth credential and real account."""

    del composed_tables
    with system_context(reason="test.facebook.oauth_feed"):
        feed = make_integration(
            "facebook-oauth", model=apps.get_model("posts", "Feed"), backend_class="feed",
            feed_backend_class="facebook", kind=CredentialKind.OAUTH,
            material={"access_token": "synthetic-long-user-token"}, external_id="", config={}, cursor={},
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
    protocol.oauth_client = None
    protocol.exchange_grant.return_value = graph["extended_grant"]
    tokens = {"access_token": "synthetic-short-token", "scope": "pages_show_list", "expires_in": 60}
    result = MetaFacebook.refine_grant(protocol, tokens)
    protocol.exchange_grant.assert_called_once_with("fb_exchange_token", fb_exchange_token="synthetic-short-token")
    assert tokens["access_token"] == "synthetic-short-token"
    assert result["access_token"] == "synthetic-long-user-token" and result["scope"] == tokens["scope"]
    assert result["expires_in"] == 5184000
    assert MetaFacebook.graph_url("me") == MetaFacebook.defaults["userinfo_endpoint"]


def test_refinement_never_reuses_the_short_grant_expiry_when_the_long_grant_omits_it():
    protocol = Mock(spec=OAuthClientProtocol)
    protocol.oauth_client = None
    protocol.exchange_grant.return_value = {"access_token": "synthetic-long-token"}
    result = MetaFacebook.refine_grant(protocol, {
        "access_token": "synthetic-short-token", "expires_in": 60, "expires_at": 123,
    })
    assert "expires_in" not in result and "expires_at" not in result


def test_refinement_supplies_the_short_token_proof_to_the_protocol(graph):
    protocol = Mock(spec=OAuthClientProtocol)
    protocol.oauth_client = SimpleNamespace(client_secret="synthetic-app-secret")
    protocol.exchange_grant.return_value = graph["extended_grant"]
    MetaFacebook.refine_grant(protocol, {"access_token": "synthetic-short-token"})
    proof = MetaFacebook.appsecret_proof("synthetic-short-token", protocol.oauth_client)
    protocol.exchange_grant.assert_called_once_with(
        "fb_exchange_token", fb_exchange_token="synthetic-short-token", appsecret_proof=proof,
    )
    assert len(proof) == 64


def test_profile_request_is_signed_with_the_refined_grant():
    protocol = Mock(spec=OAuthClientProtocol)
    protocol.oauth_client = SimpleNamespace(client_secret="synthetic-app-secret")
    assert MetaFacebook.userinfo_params(protocol, "synthetic-long-user-token") == {
        "appsecret_proof": MetaFacebook.appsecret_proof("synthetic-long-user-token", protocol.oauth_client),
    }
    protocol.oauth_client = SimpleNamespace(client_secret="")
    assert MetaFacebook.userinfo_params(protocol, "synthetic-long-user-token") == {}


def test_app_proof_matches_the_hmac_sha256_known_answer():
    client = SimpleNamespace(client_secret="\x0b" * 20)
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
    assert calls[0][3]["headers"] == original.auth_headers()
    exchange.assert_not_called()


@pytest.mark.parametrize("page_count", [0, 2])
def test_discovery_requires_one_page_without_writing_partial_bindings(oauth_feed, graph, monkeypatch, page_count):
    accounts = deepcopy(graph["accounts"])
    accounts["data"] = [] if not page_count else [accounts["data"][0], {**accounts["data"][0], "id": "900002"}]
    transport(monkeypatch, lambda *args: accounts)
    with system_context(reason="test.facebook.single_page"):
        before = Credential.objects.count()
        with pytest.raises(IntegrationError):
            FacebookFeedBackend(oauth_feed).discover_connection(oauth_feed.credential)
        oauth_feed.refresh_from_db()
        assert oauth_feed.external_id == "" and Credential.objects.count() == before


def test_static_page_credential_returns_before_freshness_or_transport(oauth_feed, monkeypatch):
    calls = transport(monkeypatch, lambda *args: pytest.fail("Static binding rediscovered the Page"))
    with system_context(reason="test.facebook.static_binding"):
        static = Credential.objects.create_local_credential(
            oauth_feed.owner, kind=CredentialKind.STATIC_TOKEN, name="Facebook Page (900001)",
            material={"api_key": "synthetic-page-token"},
        )
        oauth_feed.external_id = "900001"
        oauth_feed.save(update_fields=["external_id", "updated_at"])
        oauth_feed.attach_connection(static)
        refresh = Mock(side_effect=AssertionError("Static binding must return early"))
        monkeypatch.setattr(static, "ensure_fresh", refresh)
        assert FacebookFeedBackend(oauth_feed).discover_connection(static) == ConnectionDiscovery()
    refresh.assert_not_called()
    assert calls == []


def test_discovery_follows_an_empty_page_using_only_the_opaque_cursor(oauth_feed, graph, monkeypatch):
    def respond(method, path, query, kwargs):
        if query.get("after"):
            return graph["accounts"]
        return {"data": [], "paging": {"cursors": {"after": "account-next"}, "next": "https://untrusted.invalid/"}}

    calls = transport(monkeypatch, respond)
    with system_context(reason="test.facebook.discovery_pages"):
        result = FacebookFeedBackend(oauth_feed).discover_connection(oauth_feed.credential)
    assert result.data["external_id"] == "900001" and calls[1][2]["after"] == "account-next"


def test_binding_owner_attaches_discovered_page_and_preserves_the_user_account(oauth_feed, graph, monkeypatch):
    transport(monkeypatch, lambda *args: graph["accounts"])
    with system_context(reason="test.facebook.binding_apply"):
        original = oauth_feed.credential
        oauth_feed.await_binding()
        result = oauth_feed.run_binding(credential_pk=original.pk, generation=oauth_feed.binding_generation)
        oauth_feed.refresh_from_db()
        assert result["ok"]
        assert oauth_feed.external_id == oauth_feed.handle.external_id == "900001"
        assert oauth_feed.credential.kind == CredentialKind.STATIC_TOKEN
        assert oauth_feed.account_id == original.external_account_id and oauth_feed.account_id is not None
        assert oauth_feed.live_since is not None


def test_binding_generation_race_rejects_discovery_even_when_the_credential_row_is_unchanged(
    oauth_feed, graph, monkeypatch,
):
    original = oauth_feed.credential

    def reconnect(*args):
        row = type(oauth_feed).objects.get(pk=oauth_feed.pk)
        row.await_binding()
        assert row.credential_id == original.pk
        return graph["accounts"]

    transport(monkeypatch, reconnect)
    with system_context(reason="test.facebook.generation"):
        oauth_feed.await_binding()
        generation = oauth_feed.binding_generation
        outcome = oauth_feed.run_binding(credential_pk=original.pk, generation=generation)
        oauth_feed.refresh_from_db()
        assert not outcome["ok"] and oauth_feed.binding_generation == generation + 1
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


def test_app_proof_is_token_specific_and_survives_derived_page_attachment(oauth_feed, graph, monkeypatch):
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
        oauth_feed.await_binding()
        result = oauth_feed.run_binding(credential_pk=original.pk, generation=oauth_feed.binding_generation)
        assert result["ok"] and oauth_feed.credential.kind == CredentialKind.STATIC_TOKEN
        assert oauth_feed.credential.oauth_client_id is None
        assert oauth_feed.credential.reveal()["appsecret_proof"] == page_proof
        FacebookFeedBackend(oauth_feed).extract(stream(oauth_feed), 100)
    assert user_proof != page_proof
    assert calls[0][2]["appsecret_proof"] == user_proof and calls[1][2]["appsecret_proof"] == page_proof
