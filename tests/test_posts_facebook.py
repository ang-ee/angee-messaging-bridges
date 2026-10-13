"""Public Facebook stream and delivery contracts with composed framework owners."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from time import monotonic
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import connection
from django.utils import timezone
from rebac import system_context

from angee.integrate.errors import IntegrationError, RequestNotSentError
from angee.integrate.http import HttpClient
from angee.integrate.streams import CursorInvalid, advance_stream, open_stream, reset_stream
from angee.messaging.backends import DeliveryOutcome, DeliveryUncertain
from angee.messaging.delivery import RECONCILE_BACKOFF
from angee.messaging.events import message_ingested
from angee.posts.ingest import land_posts
from angee.posts_integrate_facebook.backend import FacebookFeedBackend
from angee.posts_integrate_facebook.oauth import MetaFacebook
from angee.posts_integrate_facebook.parsing import comment_post, root_post, timestamp
from tests.conftest import claim_feed_reply, make_integration

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def graph():
    """Reference-derived wire data, never provider captures."""

    return json.loads((Path(__file__).parent / "fixtures/facebook/graph.json").read_text())



def _failures(stream):
    """Retained failures of ``stream``'s current row (the lane's SyncFailure value objects)."""

    row = apps.get_model("integrate", "SyncStream").objects.get(pk=stream.pk)
    return list(row.sync_failures(open_only=False))

@pytest.fixture
def feed(composed_tables):
    """Use the shared Feed/Quota composition and real credential methods."""

    del composed_tables
    with system_context(reason="test.facebook.feed"):
        row = make_integration(
            "facebook-page", model=apps.get_model("posts", "Feed"), backend_class="feed",
            feed_backend_class="facebook", material={"api_key": "synthetic-page-token"},
            external_id="900001", display_name="Example Workshop", config={}, cursor={},
        )
        row.handle = apps.get_model("parties", "Handle").objects.upsert(
            platform="facebook", value=row.external_id, external_id=row.external_id, created_by_id=row.owner_id,
        )
        row.save(update_fields=["handle", "updated_at"])
    return row


def transport(monkeypatch, handler):
    """Replace only the public pinned HTTP request seam."""

    calls = []

    def request(client, method, url, **kwargs):
        del client
        assert not connection.in_atomic_block
        parts = urlsplit(url)
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        assert (parts.scheme, parts.netloc) == ("https", "graph.facebook.com")
        assert "access_token" not in query
        path = parts.path.removeprefix(f"/{MetaFacebook.graph_api_version}/")
        calls.append((method, path, query, kwargs))
        result = handler(method, path, query, kwargs)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, httpx2.Response):
            return result
        status, payload, headers = result if isinstance(result, tuple) else (200, result, {})
        return httpx2.Response(status, json=deepcopy(payload), headers=headers, request=httpx2.Request(method, url))

    monkeypatch.setattr(HttpClient, "request", request)
    return calls


def stream(feed, key="history", cursor=None):
    """Open a real durable stream, optionally seeding a recovery position."""

    state = open_stream(feed, key, feed.external_id, FacebookFeedBackend(feed))
    return reset_stream(state, cursor=cursor).stream if cursor is not None else state


def reply(feed, graph, *, actor, text="A public response"):
    """Prepare a held reply through the messaging/posts owner, without sending it."""

    root = root_post(graph["posts_first"]["data"][0], page_id=feed.external_id, page_name=feed.display_name)
    comment = comment_post(
        graph["comments_first"]["data"][0], post_id=root.message.external_id, page_id=feed.external_id,
    )
    land_posts(feed, [root, comment], owner_id=feed.owner_id, historical=True)
    parent = apps.get_model("messaging", "Message").objects.get(channel=feed, external_id=comment.message.external_id)
    assert parent.with_actor(actor).has_access("read")
    return parent.reply_to_comment(body=text, actor=actor)


def settle_uncertain_reply(message, error, *, monkeypatch):
    """Settle ambiguity, then claim reconciliation at the idle horizon."""

    moment = timezone.now()
    monkeypatch.setattr(timezone, "now", lambda: moment)
    model = type(message)
    with system_context(reason="test.facebook.delivery.uncertain"):
        assert model.objects.record_delivery(
            message.pk, token=message.external_id, claim=message.delivery_claim, error=error,
        ) == "uncertain"
        message.refresh_from_db()
        backoff = (
            RECONCILE_BACKOFF * 2 ** (message.delivery_reconcile_attempts - 1)
            if message.delivery_reconcile_attempts else timedelta()
        )
        assert message.delivery_lease_until is None
        assert message.scheduled_at == max(
            moment + backoff, message.delivery_started_at + FacebookFeedBackend.delivery_visibility_delay,
        )
        horizon = message.scheduled_at
        monkeypatch.setattr(timezone, "now", lambda: horizon)
        claimed = model.objects.claim_delivery(message.pk, token=message.external_id, reconcile=True)
        assert claimed is not None
        assert claimed.scheduled_at is None and claimed.delivery_lease_until > horizon
        return claimed


def test_parser_uses_provider_ids_and_hidden_flag_without_anonymous_handles(graph):
    assert "parent" not in graph["comments_first"]["data"][0]
    assert graph["posts_first"]["data"][0]["comments"]["data"] == []
    root = root_post(graph["posts_first"]["data"][0], page_id="900001", page_name="Example Workshop")
    hidden = comment_post(graph["comments_first"]["data"][2], post_id=root.message.external_id, page_id="900001")
    child = comment_post(graph["comments_first"]["data"][1], post_id=root.message.external_id, page_id="900001")
    assert root.message.external_id == root.message.thread.external_id == "900001_700001"
    assert root.metrics.reply_count == 4
    assert hidden.hidden and hidden.message.sender is None
    assert child.message.external_id == "700001_800002" and child.message.in_reply_to == "700001_800001"


@pytest.mark.parametrize("value", [None, "invalid", "2026-19-01T09:00:00Z", "2026-10-01T09:00:00"])
def test_missing_or_invalid_dates_remain_unknown(value):
    assert timestamp(value) is None


def test_history_resumes_across_backend_instances_and_stops_when_exhausted(feed, graph, monkeypatch):
    def respond(method, path, query, kwargs):
        assert method == "GET" and kwargs["headers"] == feed.credential.auth_headers()
        if path.endswith("/posts"):
            assert "comments.filter(stream).limit(0).summary(true)" in query["fields"]
            return graph["posts_last" if query.get("after") else "posts_first"]
        assert query["filter"] == "stream" and query["order"] == "chronological"
        if path.startswith("900001_700001/"):
            return graph["comments_last" if query.get("after") else "comments_first"]
        return graph["old_comments"]

    calls = transport(monkeypatch, respond)
    with system_context(reason="test.facebook.history"):
        state = stream(feed)
        assert [item.key for item in FacebookFeedBackend(feed).streams()] == ["activity", "history", "reobserve"]
        for index in range(5):
            result = advance_stream(state, FacebookFeedBackend(feed))
            state = result.stream
            assert len(calls) == index + 1
        assert result.exhausted
        assert advance_stream(state, FacebookFeedBackend(feed)).exhausted
        assert len(calls) == 5
        assert apps.get_model("posts", "Quota").objects.get(integration=feed).quota_used == 5
        identities = set(apps.get_model("messaging", "Message").objects.filter(channel=feed).values_list(
            "external_id", flat=True,
        ))
    assert identities == {
        "900001_700001", "900001_700002", "700001_800001", "700001_800002",
        "700001_800003", "700001_800004", "700002_800005",
    }
    assert calls[2][2]["after"] == "comments-next" and calls[3][2]["after"] == "posts-next"
    assert "token" not in json.dumps(state.cursor)


def test_activity_skips_unchanged_counts_but_keeps_changed_work_after_root_landing(feed, graph, monkeypatch):
    raw = deepcopy(graph["posts_last"]["data"][0])
    raw["created_time"] = "2026-10-01T09:00:00Z"
    moment = timestamp("2026-10-10T12:00:00Z")
    monkeypatch.setattr(timezone, "now", lambda: moment)
    calls = transport(monkeypatch, lambda method, path, query, kwargs: (
        {"data": [raw]} if path.endswith("/posts") else graph["old_comments"]
    ))
    with system_context(reason="test.facebook.activity"):
        feed.live_since = moment - timedelta(days=30)
        feed.save(update_fields=["live_since", "updated_at"])
        land_posts(
            feed, [root_post(raw, page_id="900001", page_name=feed.display_name)],
            owner_id=feed.owner_id, historical=True,
        )
        state = stream(feed, "activity", {
            "newest_published_at": raw["created_time"],
        })
        unchanged = advance_stream(state, FacebookFeedBackend(feed))
        assert unchanged.exhausted and len(calls) == 1
        raw["comments"]["summary"]["total_count"] = 2
        changed = advance_stream(unchanged.stream, FacebookFeedBackend(feed))
        assert not changed.exhausted
        resumed = advance_stream(changed.stream, FacebookFeedBackend(feed))
        assert resumed.exhausted
        assert apps.get_model("messaging", "Message").objects.filter(channel=feed, external_id="700002_800005").exists()
    assert len(calls) == 3 and calls[-1][1] == "900001_700002/comments"


def test_empty_pages_advance_and_bad_or_repeated_after_requests_a_new_baseline(feed, graph, monkeypatch):
    empty = {"data": [], "paging": graph["posts_first"]["paging"]}
    transport(monkeypatch, lambda *args: empty)
    with system_context(reason="test.facebook.cursor"):
        page = advance_stream(stream(feed), FacebookFeedBackend(feed), page_bound=1)
        assert not page.exhausted and page.count == 0 and page.stream.cursor["posts_after"] == "posts-next"
        reset = advance_stream(page.stream, FacebookFeedBackend(feed), page_bound=1)
        assert reset.reset and reset.stream.cursor["posts_resets"] == 1
        assert not reset.stream.cursor.get("posts_after")
        replayed = advance_stream(reset.stream, FacebookFeedBackend(feed), page_bound=1)
        with pytest.raises(IntegrationError, match="repeatedly"):
            advance_stream(replayed.stream, FacebookFeedBackend(feed), page_bound=1)
        empty["paging"] = {"next": "https://untrusted.invalid/"}
        with pytest.raises(CursorInvalid):
            FacebookFeedBackend(feed).extract(stream(feed, cursor={}), 1)


def test_stream_records_use_per_record_history_and_trash_hidden_comments(feed, graph, monkeypatch):
    events = []

    def observe(sender, instance, **kwargs):
        events.append(instance.external_id)

    message_ingested.connect(observe, weak=False)
    try:
        with system_context(reason="test.facebook.live_records"):
            feed.live_since = timestamp("2026-10-01T00:00:00Z")
            feed.save(update_fields=["live_since", "updated_at"])
            hidden_raw = {**graph["old_comments"]["data"][0], "id": "700001_800008", "is_hidden": True}
            transport(monkeypatch, lambda method, path, *args: (
                graph["posts_last"] if path.endswith("/posts")
                else {"data": [graph["old_comments"]["data"][0], hidden_raw]}
            ))
            first = advance_stream(stream(feed), FacebookFeedBackend(feed))
            second = advance_stream(first.stream, FacebookFeedBackend(feed))
            assert second.exhausted
            assert events == ["700002_800005"]
            message = apps.get_model("messaging", "Message").objects.get(channel=feed, external_id="700001_800008")
            assert message.is_trashed
    finally:
        message_ingested.disconnect(observe)


def test_reply_returns_the_delivery_contract_and_form_payload(feed, replier, graph, monkeypatch):
    calls = transport(monkeypatch, lambda method, *args: graph["insert"] if method == "POST" else {"data": []})
    with system_context(reason="test.facebook.publish"):
        outcome = FacebookFeedBackend(feed).deliver(reply(feed, graph, actor=replier, text="Answer & follow-up"))
    assert outcome == DeliveryOutcome(accepted=True, provider_id="700001_800006")
    assert [call[0] for call in calls] == ["POST"]
    assert calls[0][1] == "700001_800001/comments" and calls[0][2]["appsecret_proof"]
    assert parse_qs(calls[0][3]["body"].decode()) == {"message": ["Answer & follow-up"]}


def test_reply_for_another_feed_is_declined_before_transport(feed, replier, graph, monkeypatch):
    calls = transport(monkeypatch, lambda *args: pytest.fail("A foreign reply reached Graph"))
    with system_context(reason="test.facebook.reply_scope"):
        message = reply(feed, graph, actor=replier)
        message.channel_id = feed.pk + 1
        assert FacebookFeedBackend(feed).deliver(message) == DeliveryOutcome(accepted=False)
    assert calls == []


def test_matching_visitor_text_is_not_mistaken_for_a_page_reply(feed, replier, graph, monkeypatch):
    visitor = deepcopy(graph["existing_reply"])
    visitor["data"][0]["from"]["id"] = "600001"
    calls = transport(monkeypatch, lambda method, *args: graph["insert"] if method == "POST" else visitor)
    with system_context(reason="test.facebook.reply_author"):
        assert FacebookFeedBackend(feed).deliver(reply(feed, graph, actor=replier)) == DeliveryOutcome(
            True, "700001_800006",
        )
    assert [call[0] for call in calls] == ["POST"]


@pytest.mark.parametrize("error_type", [httpx2.ReadTimeout, httpx2.RemoteProtocolError, OSError])
def test_lost_ack_is_settled_before_reconciling_the_existing_page_reply(
    feed, replier, graph, monkeypatch, error_type,
):
    graph["existing_reply"]["data"][0]["created_time"] = timezone.now().isoformat()
    calls = transport(monkeypatch, lambda method, *args: (
        error_type("synthetic-private-transport-message") if method == "POST" else graph["existing_reply"]
    ))
    with system_context(reason="test.facebook.acknowledgement"):
        message = claim_feed_reply(reply(feed, graph, actor=replier), actor=feed.owner)
        backend = FacebookFeedBackend(feed)
        with pytest.raises(DeliveryUncertain) as raised:
            backend.deliver(message)
        recovered = settle_uncertain_reply(message, raised.value, monkeypatch=monkeypatch)
        assert backend.deliver(recovered) == DeliveryOutcome(True, "700001_800006")
    assert [call[0] for call in calls] == ["POST", "GET"]
    assert calls[1][2]["order"] == "reverse_chronological"
    assert calls[1][2]["fields"] == "id,from,message,created_time,parent"


def test_uncertain_publish_does_not_search_before_settlement(feed, replier, graph, monkeypatch):
    calls = transport(monkeypatch, lambda *args: httpx2.ReadTimeout("private"))
    with system_context(reason="test.facebook.uncertain"):
        with pytest.raises(DeliveryUncertain):
            FacebookFeedBackend(feed).deliver(reply(feed, graph, actor=replier))
    assert [call[0] for call in calls] == ["POST"]


def test_page_throttle_uses_business_usage_recovery_for_streams_and_delivery(feed, replier, graph, monkeypatch):
    throttle = graph["page_throttle"]
    transport(monkeypatch, lambda *args: (throttle["status"], throttle["body"], throttle["headers"]))
    with system_context(reason="test.facebook.throttle"):
        with pytest.raises(IntegrationError) as extracted:
            FacebookFeedBackend(feed).extract(stream(feed), 100)
        with pytest.raises(IntegrationError) as delivered:
            FacebookFeedBackend(feed).deliver(reply(feed, graph, actor=replier))
    assert extracted.value.transient and extracted.value.retry_after == timedelta(minutes=37)
    assert delivered.value.retry_after == timedelta(minutes=37)
    assert "synthetic-private-provider-message" not in extracted.value.public_message


def test_connect_failure_before_publish_is_retryable(feed, replier, graph, monkeypatch):
    transport(monkeypatch, lambda method, *args: RequestNotSentError() if method == "POST" else {"data": []})
    with system_context(reason="test.facebook.pre_send"):
        with pytest.raises(RequestNotSentError):
            FacebookFeedBackend(feed).deliver(reply(feed, graph, actor=replier))


def test_quota_refusal_uses_the_ledger_period_end_and_skips_transport(feed, monkeypatch):
    moment = timezone.now()
    monkeypatch.setattr(timezone, "now", lambda: moment)
    calls = transport(monkeypatch, lambda *args: pytest.fail("Exhausted quota reached Graph"))
    with system_context(reason="test.facebook.quota"):
        quota = apps.get_model("posts", "Quota").objects
        period = quota.open_period(
            integration=feed, scope=FacebookFeedBackend(feed).quota_scope(), now=moment,
        )
        period.quota_used = FacebookFeedBackend(feed).policy.quota_limit
        period.period_end = moment + timedelta(seconds=97)
        period.save(update_fields=["quota_used", "period_end", "updated_at"])
        with pytest.raises(IntegrationError) as raised:
            FacebookFeedBackend(feed).extract(stream(feed), 100)
    assert raised.value.transient and raised.value.retry_after == timedelta(seconds=97)
    assert calls == []


@pytest.mark.parametrize("fixture", ["revoked", "missing", "permission_denied"])
def test_grant_and_permission_refusals_do_not_skip_pages_or_write_credentials(feed, graph, monkeypatch, fixture):
    transport(monkeypatch, lambda *args: (400, graph[fixture], {}))
    with system_context(reason="test.facebook.refusal"):
        original_status = feed.credential.status
        with pytest.raises(IntegrationError) as raised:
            FacebookFeedBackend(feed).extract(stream(feed), 100)
        feed.credential.refresh_from_db()
        assert feed.credential.status == original_status
    assert not raised.value.transient and "synthetic-private-provider-message" not in raised.value.public_message


@pytest.mark.parametrize("live_since", ["2026-10-07T00:00:00Z", "2026-08-01T00:00:00Z"])
def test_activity_first_scan_is_bounded_and_keeps_a_durable_newest_watermark(feed, graph, monkeypatch, live_since):
    moment = timestamp("2026-10-10T12:00:00Z")
    live = timestamp(live_since)
    floor = max(live, moment - timedelta(days=30))
    monkeypatch.setattr(timezone, "now", lambda: moment)
    roots = []
    for index, day in enumerate((9, 8, 6)):
        raw = deepcopy(graph["posts_first"]["data"][0])
        raw.update(id=f"900001_70000{index + 1}", created_time=f"2026-10-{day:02}T09:00:00Z")
        raw["comments"]["summary"]["total_count"] = 0
        roots.append(raw)
    roots[-1]["created_time"] = (floor - timedelta(days=1)).isoformat()

    def respond(method, path, query, kwargs):
        if path.endswith("/comments"):
            return {"data": []}
        index = int(query.get("after", "0"))
        return {
            "data": [roots[index]],
            "paging": {"cursors": {"after": str(index + 1)}, "next": "https://untrusted.invalid/"},
        }

    calls = transport(monkeypatch, respond)
    with system_context(reason="test.facebook.activity_window"):
        feed.live_since = live
        feed.save(update_fields=["live_since", "updated_at"])
        state = stream(feed, "activity")
        for _ in range(5):
            result = advance_stream(state, FacebookFeedBackend(feed), page_bound=1)
            state = result.stream
        assert result.exhausted
        assert state.cursor == {
            "newest_published_at": "2026-10-09T09:00:00+00:00",
        }
        messages = apps.get_model("messaging", "Message").objects.filter(channel=feed)
        assert set(messages.values_list("external_id", flat=True)) == {"900001_700001", "900001_700002"}
        for _ in range(3):
            result = advance_stream(state, FacebookFeedBackend(feed), page_bound=1)
            state = result.stream
        assert result.exhausted and state.cursor["newest_published_at"] == "2026-10-09T09:00:00+00:00"
    assert len(calls) == 8 and all(call[1].endswith("/posts") for call in calls[5:])
    assert all(call[2]["since"] == str(int(floor.timestamp()))
               for call in calls if call[1].endswith("/posts"))
    assert "Workshop" not in json.dumps(state.cursor) and "message" not in json.dumps(state.cursor)


def test_deadline_keeps_fixed_http_timeout_and_returns_committable_partial_progress(feed, graph, monkeypatch):
    deadline = monotonic() + 60

    def respond(*args):
        monkeypatch.setattr("angee.posts_integrate_facebook.backend.monotonic", lambda: deadline + 1)
        return graph["posts_first"]

    calls = transport(monkeypatch, respond)
    with system_context(reason="test.facebook.deadline"):
        first = advance_stream(stream(feed), FacebookFeedBackend(feed), deadline=deadline)
        saved = deepcopy(first.stream.cursor)
        second = advance_stream(first.stream, FacebookFeedBackend(feed), deadline=deadline)
        assert not first.exhausted and first.count == 1
        assert not second.exhausted and second.count == 0 and second.stream.cursor == saved
    assert len(calls) == 1 and calls[0][3]["timeout"] == 10.0


@pytest.mark.parametrize("key", ["activity", "history", "reobserve"])
def test_malformed_records_and_permanent_post_reads_are_quarantined_without_wedging(
    feed, graph, monkeypatch, key,
):
    moment = timestamp("2026-10-10T12:00:00Z")
    monkeypatch.setattr(timezone, "now", lambda: moment)
    later = deepcopy(graph["posts_last"]["data"][0])
    later["created_time"] = "2026-10-02T09:00:00Z"

    def respond(method, path, *args):
        if path.endswith("/posts"):
            return {"data": [graph["posts_first"]["data"][0], graph["malformed_post"], later]}
        if path.startswith("900001_700001/"):
            return 400, graph["missing"], {}
        return {"data": [graph["malformed_comment"], None, graph["old_comments"]["data"][0]]}

    calls = transport(monkeypatch, respond)
    with system_context(reason="test.facebook.quarantine"):
        feed.live_since = timestamp("2026-10-01T00:00:00Z")
        feed.save(update_fields=["live_since", "updated_at"])
        first = advance_stream(stream(feed, key), FacebookFeedBackend(feed))
        second = advance_stream(first.stream, FacebookFeedBackend(feed))
        third = advance_stream(second.stream, FacebookFeedBackend(feed))
        assert first.count == 2 and second.count == 0 and third.count == 1 and third.exhausted
        assert len(first.failure_ids) == 1 and len(second.failure_ids) == 1
        assert len(set(third.failure_ids)) == 2
        failures = _failures(third.stream)
        refused = [row for row in failures if row.id in third.failure_ids]
        assert {row.details["external_key"] for row in refused} == {
            "700001_800099", "900001_700002:comment:1",
        }
        assert {row.code for row in failures} == {"invalid_record", "comment_read_refused"}
        assert "synthetic-private" not in json.dumps([row.details for row in failures])
    assert len(calls) == 3 and "synthetic-private" not in json.dumps(third.stream.cursor)


def test_invalid_comment_cursor_resets_only_that_position_and_preserves_watermarks(feed, graph, monkeypatch):
    seed = {
        "posts_after": "valid-post-position", "pending": ["900001_700001"],
        "comments_after": "expired-comment-position", "comments_seen": ["expired-comment-position"],
        "newest_published_at": "2026-10-09T09:00:00Z", "scan_newest": "2026-10-10T09:00:00Z",
    }
    calls = transport(monkeypatch, lambda method, path, query, kwargs: (
        (400, graph["invalid_cursor"], {}) if query.get("after") else graph["comments_last"]
    ))
    with system_context(reason="test.facebook.position_reset"):
        reset = advance_stream(stream(feed, cursor=seed), FacebookFeedBackend(feed))
        saved = reset.stream.cursor
        assert reset.reset and saved["posts_after"] == seed["posts_after"] and saved["pending"] == seed["pending"]
        assert saved["newest_published_at"] == seed["newest_published_at"]
        assert saved["scan_newest"] == seed["scan_newest"] and not saved.get("comments_after")
        resumed = advance_stream(reset.stream, FacebookFeedBackend(feed))
        assert resumed.count == 1 and resumed.stream.cursor["posts_after"] == seed["posts_after"]
    assert len(calls) == 2 and "after" not in calls[1][2]


def test_invalid_posts_cursor_keeps_the_activity_floor_and_newest_seen_watermarks(feed, graph, monkeypatch):
    seed = {
        "posts_after": "expired-post-position", "posts_seen": ["expired-post-position"],
        "scan_floor": "2026-10-01T00:00:00Z", "newest_published_at": "2026-10-09T09:00:00Z",
        "scan_newest": "2026-10-10T09:00:00Z",
    }
    calls = transport(monkeypatch, lambda method, path, query, kwargs: (
        (400, graph["invalid_cursor"], {}) if query.get("after") else {"data": []}
    ))
    with system_context(reason="test.facebook.posts_reset"):
        reset = advance_stream(stream(feed, "activity", seed), FacebookFeedBackend(feed))
        assert reset.reset and not reset.stream.cursor.get("posts_after")
        assert all(reset.stream.cursor[key] == seed[key] for key in (
            "scan_floor", "newest_published_at", "scan_newest",
        ))
        resumed = advance_stream(reset.stream, FacebookFeedBackend(feed))
        assert resumed.exhausted and resumed.stream.cursor["newest_published_at"] == "2026-10-10T09:00:00+00:00"
    assert len(calls) == 2 and calls[0][2]["since"] == calls[1][2]["since"] and "after" not in calls[1][2]


def test_revoked_grant_on_a_comment_read_is_not_quarantined_or_skipped(feed, graph, monkeypatch):
    transport(monkeypatch, lambda *args: (400, graph["revoked"], {}))
    with system_context(reason="test.facebook.revoked_comments"):
        state = stream(feed, cursor={"pending": ["900001_700001"], "posts_done": True})
        saved = deepcopy(state.cursor)
        with pytest.raises(IntegrationError, match="reconnect") as raised:
            advance_stream(state, FacebookFeedBackend(feed))
        state.refresh_from_db()
        assert state.cursor == saved
        assert not _failures(state)
    assert not raised.value.transient and type(raised.value) is IntegrationError


def test_repeated_bad_comment_cursor_records_discrepancy_and_moves_to_the_next_post(feed, monkeypatch):
    transport(monkeypatch, lambda *args: {"data": [], "paging": {"next": "https://untrusted.invalid/"}})
    with system_context(reason="test.facebook.repeated_position"):
        seed = {"pending": ["900001_700001", "900001_700002"], "posts_done": True}
        first = advance_stream(stream(feed, cursor=seed), FacebookFeedBackend(feed))
        second = advance_stream(first.stream, FacebookFeedBackend(feed))
        assert first.reset and len(second.failure_ids) == 1
        assert second.stream.cursor["pending"] == ["900001_700002"]
        assert not second.exhausted


def test_malformed_cursor_repair_keeps_other_valid_positions_and_removes_payloads(feed):
    seed = {
        "pending": ["900001_700001"], "comments_after": 9, "posts_after": "valid-post-position",
        "newest_published_at": "2026-10-09T09:00:00Z", "message": "synthetic-private-cursor-payload",
    }
    with system_context(reason="test.facebook.cursor_shape"):
        with pytest.raises(CursorInvalid) as raised:
            FacebookFeedBackend(feed).extract(stream(feed, cursor=seed), 100)
    assert raised.value.cursor["pending"] == seed["pending"]
    assert raised.value.cursor["posts_after"] == seed["posts_after"]
    assert raised.value.cursor["newest_published_at"] == seed["newest_published_at"]
    assert "comments_after" not in raised.value.cursor and "message" not in raised.value.cursor


def test_polling_leaves_the_reply_reserve_available_to_delivery(feed, replier, graph, monkeypatch):
    calls = transport(monkeypatch, lambda method, *args: graph["insert"] if method == "POST" else {"data": []})
    with system_context(reason="test.facebook.reply_reserve"):
        feed.config = {"quota_limit": 5, "reply_reserve": 2}
        feed.save(update_fields=["config", "updated_at"])
        backend = FacebookFeedBackend(feed)
        period = backend.consume_quota(3)
        with pytest.raises(IntegrationError) as refusal:
            backend.extract(stream(feed), 100)
        assert refusal.value.transient and not calls
        assert not backend.consume_reobserve_quota(1)
        assert backend._reobserve_units_used == 0
        period.refresh_from_db()
        assert period.quota_used == 3
        assert backend.deliver(reply(feed, graph, actor=replier)) == DeliveryOutcome(
            True, "700001_800006",
        )
        period.refresh_from_db()
        assert period.quota_used == 4
        assert backend.consume_quota(1, delivery=True).pk == period.pk
        with pytest.raises(IntegrationError) as exhausted:
            backend.consume_quota(1, delivery=True)
        assert exhausted.value.transient
        period.refresh_from_db()
        assert period.quota_used == 5
    assert [call[0] for call in calls] == ["POST"]


@pytest.mark.parametrize("refused", [False, True])
def test_invalid_lookup_after_uncertain_settlement_never_sends(feed, replier, graph, monkeypatch, refused):
    calls = transport(monkeypatch, lambda method, path, query, kwargs: (
        (400, graph["invalid_cursor"], {}) if refused and query.get("after")
        else {"data": [], "paging": graph["comments_first"]["paging"]}
    ))
    with system_context(reason="test.facebook.lookup_cursor"):
        message = claim_feed_reply(reply(feed, graph, actor=replier), actor=feed.owner)
        recovered = settle_uncertain_reply(
            message, DeliveryUncertain("Acknowledgement lost."), monkeypatch=monkeypatch,
        )
        with pytest.raises(DeliveryUncertain):
            FacebookFeedBackend(feed).deliver(recovered)
    assert [call[0] for call in calls] == ["GET", "GET"]


@pytest.mark.parametrize("reserve", [5, 6])
def test_reply_reserve_must_leave_a_positive_polling_allowance(feed, monkeypatch, reserve):
    calls = transport(monkeypatch, lambda *args: pytest.fail("Invalid policy reached Graph"))
    with system_context(reason="test.facebook.reserve_policy"):
        feed.config = {"quota_limit": 5, "reply_reserve": reserve}
        with pytest.raises(ValidationError, match="reply_reserve must be smaller"):
            FacebookFeedBackend(feed).extract(stream(feed), 100)
    assert not calls


def test_identical_text_for_two_comments_posts_twice_without_lookup(feed, replier, graph, monkeypatch):
    calls = transport(monkeypatch, lambda *args: graph["insert"])
    with system_context(reason="test.facebook.distinct_anchors"):
        first = reply(feed, graph, actor=replier, text="Thanks!")
        raw = deepcopy(graph["comments_first"]["data"][0])
        raw["id"] = "700001_800007"
        land_posts(feed, [comment_post(raw, post_id="900001_700001", page_id=feed.external_id)],
                   owner_id=feed.owner_id, historical=True)
        parent = apps.get_model("messaging", "Message").objects.get(channel=feed, external_id="700001_800007")
        second = parent.reply_to_comment(body="Thanks!", actor=replier)
        backend = FacebookFeedBackend(feed)
        assert backend.deliver(first).accepted and backend.deliver(second).accepted
    assert [call[0] for call in calls] == ["POST", "POST"]
    assert [call[1] for call in calls] == ["700001_800001/comments", "700001_800007/comments"]


def test_reconciliation_matches_author_parent_and_inclusive_time_and_stops_at_old_reply(
    feed, replier, graph, monkeypatch,
):
    since = timestamp("2026-10-10T12:00:00Z")
    match = {**graph["existing_reply"]["data"][0], "created_time": since.isoformat()}
    rows = [
        {**match, "id": "700001_800010", "from": {"id": "600001"}},
        {**match, "id": "700001_800011", "parent": {"id": "700001_800007"}},
        match,
    ]
    calls = transport(monkeypatch, lambda *args: {"data": rows})
    with system_context(reason="test.facebook.reconciliation"):
        message = reply(feed, graph, actor=replier)
        backend = FacebookFeedBackend(feed)
        assert backend.find_published_reply(message, since=since) == "700001_800006"
        rows[:] = [{**match, "created_time": (since - timedelta(seconds=1)).isoformat()}, match]
        assert backend.find_published_reply(message, since=since) is None
    assert len(calls) == 2


@pytest.mark.parametrize("status, error", [
    (500, {"code": 2, "is_transient": True}),
    (503, {"code": 1}),
    (400, {"code": 2}),
    (400, {"code": 99, "is_transient": True}),
])
def test_transient_comment_reads_keep_pending_and_reply_posts_become_uncertain(
    feed, replier, graph, monkeypatch, status, error,
):
    envelope = deepcopy(graph["server_transient"]["body"])
    envelope["error"].update(error)
    calls = transport(monkeypatch, lambda *args: (status, envelope, {}))
    with system_context(reason="test.facebook.transient"):
        state = stream(feed, cursor={"pending": ["900001_700001"]})
        with pytest.raises(IntegrationError) as raised:
            advance_stream(state, FacebookFeedBackend(feed))
        assert raised.value.transient
        state.refresh_from_db()
        assert state.cursor["pending"] == ["900001_700001"]
        streams = apps.get_model("integrate", "SyncStream").objects.filter(integration_id=feed.pk)
        assert not any(_failures(row) for row in streams)
        with pytest.raises(DeliveryUncertain):
            FacebookFeedBackend(feed).deliver(reply(feed, graph, actor=replier))
    assert [call[0] for call in calls] == ["GET", "POST"]


@pytest.mark.parametrize("change", ["same_count_new_comment", "hide", "unhide", "edit"])
def test_hourly_active_window_refresh_observes_changes_without_count_change(
    feed, graph, monkeypatch, change,
):
    moment = timestamp("2026-10-10T12:00:00Z")
    monkeypatch.setattr(timezone, "now", lambda: moment)
    raw = deepcopy(graph["posts_last"]["data"][0])
    raw["created_time"] = "2026-10-01T09:00:00Z"
    comment = deepcopy(graph["old_comments"]["data"][0])
    before = {**comment, "is_hidden": change == "unhide"}
    if change == "same_count_new_comment":
        comment["id"] = "700002_800777"  # A deletion plus insertion left total_count unchanged.
    elif change == "edit":
        comment["message"] = "Changed text"
    else:
        comment["is_hidden"] = change == "hide"
    calls = transport(monkeypatch, lambda method, path, *args: (
        {"data": [raw]} if path.endswith("/posts") else {"data": [comment]}
    ))
    with system_context(reason="test.facebook.refresh"):
        feed.live_since = moment - timedelta(days=30)
        feed.save(update_fields=["live_since", "updated_at"])
        land_posts(feed, [
            root_post(raw, page_id=feed.external_id, page_name=feed.display_name),
            comment_post(before, post_id=raw["id"], page_id=feed.external_id),
        ], owner_id=feed.owner_id, historical=True)
        state = stream(feed, "reobserve", {
            "recheck_after": moment.isoformat(),
        })
        root_page = advance_stream(state, FacebookFeedBackend(feed))
        result = advance_stream(root_page.stream, FacebookFeedBackend(feed))
        assert result.exhausted and timestamp(result.stream.cursor["recheck_after"]) > moment
        landed = apps.get_model("messaging", "Message").objects.get(channel=feed, external_id=comment["id"])
        assert landed.body_text() == comment["message"]
        assert landed.is_trashed == (change == "hide")
    assert len(calls) == 2


@pytest.mark.parametrize("header", ["X-Page-Usage", "X-App-Usage"])
def test_usage_headers_and_http_date_retry_after_share_the_framework_contract(header):
    moment = timestamp("2026-10-10T12:00:00Z")
    response = httpx2.Response(429, headers={
        header: json.dumps({"call_count": 100, "total_time": 1, "total_cputime": 2}),
        "Retry-After": "Sat, 10 Oct 2026 12:02:00 GMT",
    })
    assert FacebookFeedBackend.retry_after(response, now=moment) == timedelta(hours=1)
    response = httpx2.Response(429, headers={"Retry-After": "Sat, 10 Oct 2026 12:02:00 GMT"})
    assert FacebookFeedBackend.retry_after(response, now=moment) == timedelta(minutes=2)


@pytest.mark.parametrize("address", ["8.8.8.8", "127.0.0.1"])
def test_facebook_pinned_client_connect_error_is_native_not_sent(feed, replier, graph, monkeypatch, address):
    import ipaddress

    import httpcore2

    from angee.integrate import http as integrate_http

    monkeypatch.setattr(integrate_http, "resolved_addresses", lambda *args: (ipaddress.ip_address(address),))
    connections = []

    def fail_connect(*args, **kwargs):
        connections.append(True)
        raise httpcore2.ConnectError("Synthetic connect refusal")

    monkeypatch.setattr(httpcore2.SyncBackend, "connect_tcp", fail_connect)
    with system_context(reason="test.facebook.pinned_connect"):
        with pytest.raises(IntegrationError) as raised:
            FacebookFeedBackend(feed).deliver(reply(feed, graph, actor=replier))
    # A refused connect is retryable as not sent; the SSRF policy refusal is permanent.
    assert isinstance(raised.value, RequestNotSentError) is (address == "8.8.8.8")
    assert raised.value.transient is (address == "8.8.8.8")
    assert len(connections) == (1 if address == "8.8.8.8" else 0)


def test_reconciliation_with_missing_author_cannot_establish_absence(feed, replier, graph, monkeypatch):
    raw = deepcopy(graph["existing_reply"]["data"][0])
    raw.pop("from")
    transport(monkeypatch, lambda *args: {"data": [raw]})
    with system_context(reason="test.facebook.lookup_identity"):
        with pytest.raises(IntegrationError, match="omitted identity"):
            FacebookFeedBackend(feed).find_published_reply(
                reply(feed, graph, actor=replier), since=timestamp(raw["created_time"]),
            )


@pytest.mark.parametrize("cutoff", [False, True])
def test_recent_page_reply_with_normalized_body_keeps_delivery_uncertain(
    feed, replier, graph, monkeypatch, cutoff,
):
    since = timestamp("2026-10-10T12:00:00Z")
    candidate = {**graph["existing_reply"]["data"][0], "message": "A normalized public response"}
    rows = [candidate]
    if cutoff:
        rows.append({**candidate, "created_time": (since - timedelta(seconds=1)).isoformat()})
    calls = transport(monkeypatch, lambda *args: {"data": rows})
    with system_context(reason="test.facebook.normalized_reply"):
        message = reply(feed, graph, actor=replier)
        with pytest.raises(DeliveryUncertain, match="normalized"):
            FacebookFeedBackend(feed).find_published_reply(message, since=since)
    assert [call[0] for call in calls] == ["GET"]


@pytest.mark.parametrize("paginated", [False, True])
def test_reconciliation_finds_older_exact_match_after_newer_differing_page_reply(
    feed, replier, graph, monkeypatch, paginated,
):
    since = timestamp("2026-10-10T12:00:00Z")
    match = {**graph["existing_reply"]["data"][0], "created_time": since.isoformat()}
    newer = {
        **match, "id": "700001_800010", "message": "Another Page response",
        "created_time": (since + timedelta(seconds=1)).isoformat(),
    }

    def respond(method, path, query, kwargs):
        if not paginated:
            return {"data": [newer, match]}
        if query.get("after"):
            return {"data": [match]}
        return {"data": [newer], "paging": graph["comments_first"]["paging"]}

    calls = transport(monkeypatch, respond)
    with system_context(reason="test.facebook.older_exact_reply"):
        message = reply(feed, graph, actor=replier)
        assert FacebookFeedBackend(feed).find_published_reply(message, since=since) == match["id"]
    assert [call[0] for call in calls] == ["GET"] * (2 if paginated else 1)


@pytest.mark.parametrize("budget", [0, 1])
def test_reobservation_budget_retains_cursor_and_live_comments_continue(feed, graph, monkeypatch, budget):
    calls = transport(monkeypatch, lambda method, path, *args: (
        {"data": [graph["posts_first"]["data"][0]]} if path.endswith("/posts")
        else {"data": [graph["comments_first"]["data"][0]]}
    ))
    with system_context(reason="test.facebook.reobserve_budget"):
        feed.config = {"reobserve_units_per_cycle": budget}
        feed.save(update_fields=["config", "updated_at"])
        state = stream(feed, "reobserve")
        backend = FacebookFeedBackend(feed)
        if budget:
            first = advance_stream(state, backend)
            assert first.count == 1 and first.stream.cursor["pending"] == ["900001_700001"]
            state = first.stream
        previous, cursor = len(calls), deepcopy(state.cursor)
        paused = advance_stream(state, backend)
        assert paused.exhausted and paused.stream.cursor == cursor and len(calls) == previous
        live = advance_stream(stream(feed, "activity"), backend)
        landed = advance_stream(live.stream, backend)
        assert landed.count == 1 and landed.exhausted
        assert backend._reobserve_units_used == budget
        assert apps.get_model("messaging", "Message").objects.filter(
            channel=feed, external_id="700001_800001",
        ).exists()
        if budget:
            resumed = advance_stream(paused.stream, FacebookFeedBackend(feed))
            assert resumed.exhausted and resumed.count == 1


def test_daily_polling_exhaustion_retains_reobservation_cursor_without_cycle_debit(feed, monkeypatch):
    calls = transport(monkeypatch, lambda *args: pytest.fail("Exhausted polling reached Graph"))
    with system_context(reason="test.facebook.reobserve_daily"):
        feed.config = {"quota_limit": 2, "reply_reserve": 1}
        feed.save(update_fields=["config", "updated_at"])
        backend = FacebookFeedBackend(feed)
        period = backend.consume_quota(1)
        state = stream(feed, "reobserve", {"pending": ["900001_700001"], "scan_floor": timezone.now().isoformat()})
        paused = advance_stream(state, backend)
        assert paused.exhausted and paused.stream.cursor == state.cursor
        assert backend._reobserve_units_used == 0
        period.refresh_from_db()
        assert period.quota_used == 1 and not calls


@pytest.mark.parametrize("code", [4, 17, 32])
def test_meta_rate_codes_without_hints_keep_fifteen_minute_default(feed, graph, monkeypatch, code):
    error = deepcopy(graph["rate_limited"])
    error["error"]["code"] = code
    transport(monkeypatch, lambda *args: (400, error, {}))
    with system_context(reason="test.facebook.rate_default"):
        with pytest.raises(IntegrationError) as caught:
            FacebookFeedBackend(feed).extract(stream(feed), 100)
    assert caught.value.transient and caught.value.retry_after == timedelta(minutes=15)


@pytest.mark.parametrize("status, payload", [(403, "html"), (403, []), (200, "html"), (503, {})])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_shared_http_classifier_distinguishes_answered_refusals_from_ambiguity(
    feed, graph, replier, monkeypatch, status, payload, method,
):
    response = httpx2.Response(
        status, content=b"<html>refused</html>" if payload == "html" else json.dumps(payload).encode(),
        request=httpx2.Request(method, MetaFacebook.graph_url("me")), headers={"Retry-After": "123"},
    )
    transport(monkeypatch, lambda *args: response)
    with system_context(reason="test.facebook.http_classification"):
        backend = FacebookFeedBackend(feed)
        expected = DeliveryUncertain if method == "POST" and status != 403 else IntegrationError
        with pytest.raises(expected) as caught:
            if method == "POST":
                backend.deliver(reply(feed, graph, actor=replier))
            else:
                backend.extract(stream(feed), 100)
    if expected is IntegrationError:
        assert caught.value.transient == (status != 403)
        assert caught.value.retry_after == (timedelta(seconds=123) if status != 403 else None)


def test_first_reconciliation_waits_for_visibility_and_normalized_body_never_resends(
    feed, graph, replier, monkeypatch,
):
    start = timestamp("2026-10-10T12:00:00Z")
    monkeypatch.setattr(timezone, "now", lambda: start)
    candidate = {**graph["existing_reply"]["data"][0], "message": "Normalized by Graph"}
    calls = transport(monkeypatch, lambda method, *args: (
        httpx2.ReadTimeout("Synthetic lost acknowledgement") if method == "POST" else {"data": [candidate]}
    ))
    with system_context(reason="test.facebook.visibility"):
        message = claim_feed_reply(reply(feed, graph, actor=replier), actor=feed.owner)
        backend = FacebookFeedBackend(feed)
        with pytest.raises(DeliveryUncertain) as uncertain:
            backend.deliver(message)
        assert type(message).objects.record_delivery(
            message.pk, token=message.external_id, claim=message.delivery_claim, error=uncertain.value,
        ) == "uncertain"
        message.refresh_from_db()
        assert message.delivery_lease_until is None
        assert message.scheduled_at == start + backend.delivery_visibility_delay
        period = backend.current_quota_period()
        with pytest.raises(DeliveryUncertain, match="visibility margin"):
            backend.deliver(message)
        period.refresh_from_db()
        assert period.quota_used == 1 and [call[0] for call in calls] == ["POST"]
        moment = message.scheduled_at
        monkeypatch.setattr(timezone, "now", lambda: moment)
        recovered = type(message).objects.claim_delivery(message.pk, token=message.external_id, reconcile=True)
        with pytest.raises(DeliveryUncertain):
            backend.deliver(recovered)
    assert [call[0] for call in calls] == ["POST", "GET"]
