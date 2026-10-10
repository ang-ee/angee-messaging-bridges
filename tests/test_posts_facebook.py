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

from angee.integrate.errors import IntegrationError
from angee.integrate.http import HttpClient
from angee.integrate.streams import CursorInvalid, advance_stream, open_stream, reset_stream
from angee.messaging.backends import DeliveryOutcome
from angee.messaging.delivery import TransientDeliveryError
from angee.messaging.events import message_ingested
from angee.posts.ingest import land_posts
from angee.posts_integrate_facebook.backend import FacebookFeedBackend
from angee.posts_integrate_facebook.oauth import MetaFacebook
from angee.posts_integrate_facebook.parsing import comment_post, root_post, timestamp
from tests.conftest import make_integration

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def graph():
    """Invented wire data; no provider calls or captures."""

    return json.loads((Path(__file__).parent / "fixtures/facebook/graph.json").read_text())


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
        graph["comments_first"]["data"][1], post_id=root.message.external_id, page_id=feed.external_id,
    )
    land_posts(feed, [root, comment], owner_id=feed.owner_id, historical=True)
    parent = apps.get_model("messaging", "Message").objects.get(channel=feed, external_id=comment.message.external_id)
    assert parent.with_actor(actor).has_access("read")
    return parent.reply_to_comment(body=text, actor=actor)


def test_parser_uses_provider_ids_and_hidden_flag_without_anonymous_handles(graph):
    root = root_post(graph["posts_first"]["data"][0], page_id="900001", page_name="Example Workshop")
    hidden = comment_post(graph["comments_first"]["data"][2], post_id=root.message.external_id, page_id="900001")
    child = comment_post(graph["comments_first"]["data"][0], post_id=root.message.external_id, page_id="900001")
    assert root.message.external_id == root.message.thread.external_id == "900001_700001"
    assert root.metrics.reply_count == 4
    assert hidden.hidden and hidden.message.sender is None
    assert child.message.external_id == "800002" and child.message.in_reply_to == "800001"


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
        assert [item.key for item in FacebookFeedBackend(feed).streams()] == ["activity", "history"]
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
    assert identities == {"900001_700001", "900001_700002", "800001", "800002", "800003", "800004", "800005"}
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
        state = stream(feed, "activity", {"newest_published_at": raw["created_time"]})
        unchanged = advance_stream(state, FacebookFeedBackend(feed))
        assert unchanged.exhausted and len(calls) == 1
        raw["comments"]["summary"]["total_count"] = 2
        changed = advance_stream(unchanged.stream, FacebookFeedBackend(feed))
        assert not changed.exhausted
        resumed = advance_stream(changed.stream, FacebookFeedBackend(feed))
        assert resumed.exhausted
        assert apps.get_model("messaging", "Message").objects.filter(channel=feed, external_id="800005").exists()
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
            hidden_raw = {**graph["old_comments"]["data"][0], "id": "800008", "is_hidden": True}
            transport(monkeypatch, lambda method, path, *args: (
                graph["posts_last"] if path.endswith("/posts")
                else {"data": [graph["old_comments"]["data"][0], hidden_raw]}
            ))
            first = advance_stream(stream(feed), FacebookFeedBackend(feed))
            second = advance_stream(first.stream, FacebookFeedBackend(feed))
            assert second.exhausted
            assert events == ["800005"]
            message = apps.get_model("messaging", "Message").objects.get(channel=feed, external_id="800008")
            assert message.is_trashed
    finally:
        message_ingested.disconnect(observe)


def test_reply_returns_the_delivery_contract_and_form_payload(feed, replier, graph, monkeypatch):
    calls = transport(monkeypatch, lambda method, *args: graph["insert"] if method == "POST" else {"data": []})
    with system_context(reason="test.facebook.publish"):
        outcome = FacebookFeedBackend(feed).deliver(reply(feed, graph, actor=replier, text="Answer & follow-up"))
    assert outcome == DeliveryOutcome(accepted=True, provider_id="800006")
    assert [call[0] for call in calls] == ["GET", "POST"]
    assert calls[1][1] == "800001/comments" and not calls[1][2]
    assert parse_qs(calls[1][3]["body"].decode()) == {"message": ["Answer & follow-up"]}


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
        assert FacebookFeedBackend(feed).deliver(reply(feed, graph, actor=replier)) == DeliveryOutcome(True, "800006")
    assert [call[0] for call in calls] == ["GET", "POST"]


@pytest.mark.parametrize("error_type", [httpx2.ReadTimeout, httpx2.RemoteProtocolError, OSError])
def test_lost_publish_acknowledgement_finds_the_existing_page_reply_without_resending(
    feed, replier, graph, monkeypatch, error_type,
):
    published = False

    def respond(method, *args):
        nonlocal published
        if method == "POST":
            published = True
            return error_type("synthetic-private-transport-message")
        return graph["existing_reply"] if published else {"data": []}

    calls = transport(monkeypatch, respond)
    with system_context(reason="test.facebook.acknowledgement"):
        message = reply(feed, graph, actor=replier)
        backend = FacebookFeedBackend(feed)
        assert backend.deliver(message) == DeliveryOutcome(True, "800006")
        assert backend.deliver(message) == DeliveryOutcome(True, "800006")
    assert [call[0] for call in calls] == ["GET", "POST", "GET", "GET"]
    assert calls[2][2]["order"] == "reverse_chronological"
    assert calls[2][2]["fields"] == "id,from,message,created_time"


def test_uncertain_publish_without_a_matching_reply_is_not_retryable(feed, replier, graph, monkeypatch):
    transport(monkeypatch, lambda method, *args: httpx2.ReadTimeout("private") if method == "POST" else {"data": []})
    with system_context(reason="test.facebook.uncertain"):
        with pytest.raises(IntegrationError, match="may have been sent") as raised:
            FacebookFeedBackend(feed).deliver(reply(feed, graph, actor=replier))
    assert not raised.value.transient


def test_page_throttle_uses_business_usage_recovery_for_streams_and_delivery(feed, replier, graph, monkeypatch):
    throttle = graph["page_throttle"]
    transport(monkeypatch, lambda *args: (throttle["status"], throttle["body"], throttle["headers"]))
    with system_context(reason="test.facebook.throttle"):
        with pytest.raises(IntegrationError) as extracted:
            FacebookFeedBackend(feed).extract(stream(feed), 100)
        with pytest.raises(TransientDeliveryError) as delivered:
            FacebookFeedBackend(feed).deliver(reply(feed, graph, actor=replier))
    assert extracted.value.transient and extracted.value.retry_after == timedelta(minutes=37)
    assert delivered.value.retry_after == timedelta(minutes=37)
    assert "synthetic-private-provider-message" not in extracted.value.public_message


def test_connect_failure_before_publish_is_retryable(feed, replier, graph, monkeypatch):
    transport(monkeypatch, lambda method, *args: httpx2.ConnectError("private") if method == "POST" else {"data": []})
    with system_context(reason="test.facebook.pre_send"):
        with pytest.raises(TransientDeliveryError):
            FacebookFeedBackend(feed).deliver(reply(feed, graph, actor=replier))


def test_quota_refusal_uses_the_ledger_period_end_and_skips_transport(feed, monkeypatch):
    moment = timezone.now()
    monkeypatch.setattr(timezone, "now", lambda: moment)
    calls = transport(monkeypatch, lambda *args: pytest.fail("Exhausted quota reached Graph"))
    with system_context(reason="test.facebook.quota"):
        quota = apps.get_model("posts", "Quota").objects
        period = quota.open_period(integration=feed, limit=10000, now=moment)
        period.quota_used = period.quota_limit
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
        assert state.cursor == {"newest_published_at": "2026-10-09T09:00:00+00:00"}
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


@pytest.mark.parametrize("key", ["activity", "history"])
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
        assert len(first.discrepancy_ids) == 1 and len(second.discrepancy_ids) == 1
        assert len(third.discrepancy_ids) == 2
        discrepancies = apps.get_model("integrate", "SyncDiscrepancy").objects.filter(stream=third.stream)
        assert set(discrepancies.values_list("code", flat=True)) == {"invalid_record", "comment_read_refused"}
        assert "synthetic-private" not in json.dumps(list(discrepancies.values_list("details", flat=True)))
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
        assert not apps.get_model("integrate", "SyncDiscrepancy").objects.filter(stream=state).exists()
    assert not raised.value.transient and type(raised.value) is IntegrationError


def test_repeated_bad_comment_cursor_records_discrepancy_and_moves_to_the_next_post(feed, monkeypatch):
    transport(monkeypatch, lambda *args: {"data": [], "paging": {"next": "https://untrusted.invalid/"}})
    with system_context(reason="test.facebook.repeated_position"):
        seed = {"pending": ["900001_700001", "900001_700002"], "posts_done": True}
        first = advance_stream(stream(feed, cursor=seed), FacebookFeedBackend(feed))
        second = advance_stream(first.stream, FacebookFeedBackend(feed))
        assert first.reset and len(second.discrepancy_ids) == 1
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
        quota = apps.get_model("posts", "Quota").objects
        period = quota.open_period(integration=feed, limit=5)
        assert quota.consume(integration=feed, units=3, limit=5)
        with pytest.raises(IntegrationError) as refusal:
            FacebookFeedBackend(feed).extract(stream(feed), 100)
        assert refusal.value.transient and not calls
        assert FacebookFeedBackend(feed).deliver(reply(feed, graph, actor=replier)) == DeliveryOutcome(True, "800006")
        period.refresh_from_db()
        assert period.quota_used == period.quota_limit == 5
    assert [call[0] for call in calls] == ["GET", "POST"]


@pytest.mark.parametrize("refused", [False, True])
def test_invalid_reply_lookup_cursor_is_permanent_and_never_sends(feed, replier, graph, monkeypatch, refused):
    calls = transport(monkeypatch, lambda method, path, query, kwargs: (
        (400, graph["invalid_cursor"], {}) if refused and query.get("after")
        else {"data": [], "paging": graph["comments_first"]["paging"]}
    ))
    with system_context(reason="test.facebook.lookup_cursor"):
        with pytest.raises(IntegrationError, match="reply lookup cursor") as raised:
            FacebookFeedBackend(feed).deliver(reply(feed, graph, actor=replier))
    assert not raised.value.transient and [call[0] for call in calls] == ["GET", "GET"]


@pytest.mark.parametrize("reserve", [5, 6])
def test_reply_reserve_must_leave_a_positive_polling_allowance(feed, monkeypatch, reserve):
    calls = transport(monkeypatch, lambda *args: pytest.fail("Invalid policy reached Graph"))
    with system_context(reason="test.facebook.reserve_policy"):
        feed.config = {"quota_limit": 5, "reply_reserve": reserve}
        with pytest.raises(ValidationError, match="reply_reserve must be smaller"):
            FacebookFeedBackend(feed).extract(stream(feed), 100)
    assert not calls
