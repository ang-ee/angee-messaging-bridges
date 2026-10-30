"""YouTube contracts exercised through the real integrate stream engine."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx2
import pytest
from django.apps import apps
from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
from rebac import system_context

from angee.integrate import streams as stream_engine
from angee.integrate.credentials import CredentialKind
from angee.integrate.discovery import ConnectionDiscovery
from angee.integrate.errors import IntegrationError, RequestNotSentError
from angee.integrate.http import HttpClient
from angee.integrate.streams import advance_stream, open_stream, sync_bridge
from angee.integrate.testing.models import SyncStream
from angee.jobs.enqueue import celery_app
from angee.messaging.backends import DeliveryOutcome, DeliveryUncertain
from angee.messaging.delivery import RECONCILE_BACKOFF, run_message_delivery
from angee.messaging.events import message_ingested
from angee.posts.ingest import PROVIDER_TRASH_REASON, land_posts
from angee.posts_integrate_youtube import backend as youtube
from angee.posts_integrate_youtube.backend import CALL_COSTS, YouTubeFeedBackend
from angee.posts_integrate_youtube.cursors import (
    ActivityCursor,
    HistoryCursor,
    PagePosition,
    ReobserveCursor,
    ReplyPosition,
    ThreadIdentity,
)
from angee.posts_integrate_youtube.oauth import GoogleYouTube
from angee.posts_integrate_youtube.parsing import comment_post, timestamp, video_post
from angee.resources.entries import ResourceEntry, resource_manifest_for
from tests.conftest import claim_feed_reply, make_integration

pytestmark = pytest.mark.django_db(transaction=True)
AT = datetime(2026, 10, 10, 12, tzinfo=UTC)
"""A fixed instant for synthetic publication, watermark and quota assertions."""



def _failures(feed, code=None):
    """Retained failures across the feed's streams (the lane's SyncFailure value objects), by code."""

    with system_context(reason="test.youtube.failures"):
        rows = [
            failure
            for stream in SyncStream.objects.filter(integration_id=feed.pk).order_by("pk")
            for failure in stream.sync_failures(open_only=False)
        ]
    return [failure for failure in rows if code is None or failure.code == code]


def _one_failure(feed, code):
    (failure,) = _failures(feed, code)
    return failure

@pytest.fixture(autouse=True)
def context(monkeypatch):
    """Keep provider tests deterministic and authorized without external calls."""

    monkeypatch.setattr(timezone, "now", lambda: AT)
    with system_context(reason="test.youtube.contract"):
        yield


@pytest.fixture
def wire():
    """Reference-derived payloads with synthetic identities, never captures."""

    return json.loads((Path(__file__).parent / "fixtures/youtube/contract.json").read_text())


@pytest.fixture
def feed(composed_tables):
    """Compose the real Feed, quota ledger and credential owners."""

    del composed_tables
    row = make_integration(
        "youtube", model=apps.get_model("posts", "Feed"), kind=CredentialKind.OAUTH, backend_class="feed",
        feed_backend_class="youtube",
        material={"access_token": "synthetic-access", "refresh_token": "synthetic-refresh"},
        external_id="channel-demo", youtube_uploads_playlist_id="uploads-demo",
        live_since=AT - timedelta(days=2),
    )
    row.handle = apps.get_model("parties", "Handle").objects.upsert(
        platform="youtube", value=row.external_id, external_id=row.external_id, created_by_id=row.owner_id,
    )
    row.save(update_fields=["handle", "updated_at"])
    return row


def transport(monkeypatch, handler):
    """Replace only the shared HTTP boundary; preserve auth, URLs, units and timeouts."""

    calls = []

    def request(client, method, url, **kwargs):
        del client
        assert not connection.in_atomic_block
        parts = urlsplit(url)
        assert (parts.scheme, parts.netloc) == ("https", "www.googleapis.com")
        assert "access_token" not in parts.query
        assert kwargs["headers"]["Authorization"] == "Bearer synthetic-access"
        assert kwargs["timeout"] == 10.0
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        resource = parts.path.rsplit("/", 1)[-1]
        calls.append((method, resource, query, kwargs))
        result = handler(method, resource, query, kwargs)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, httpx2.Response):
            return result
        status, payload, headers = result if isinstance(result, tuple) else (200, result, {})
        return httpx2.Response(status, json=deepcopy(payload), headers=headers, request=httpx2.Request(method, url))

    monkeypatch.setattr(HttpClient, "request", request)
    return calls


def source(feed, key="history", cursor=None):
    """Open a genuine durable stream through its public declaration contract."""

    adapter = feed.feed_backend
    try:
        definition = next(item for item in adapter.streams() if item.key == key)
        row = open_stream(feed, key, definition.partition, adapter, definition=definition)
        if cursor is not None:
            with transaction.atomic():
                SyncStream.objects.advance(row, cursor)
        return row
    finally:
        adapter.close()


def step(feed, stream, *, bound=10, deadline=None):
    """Let the real driver land records, quarantine refusals and commit or reset cursors."""

    adapter = feed.feed_backend
    try:
        return advance_stream(stream, adapter, page_bound=bound, deadline=deadline)
    finally:
        adapter.close()


def drain(feed, stream, *, bound=10):
    """Resume the returned epoch, including the driver's repeated-page checks."""

    adapter = feed.feed_backend
    try:
        previous, resets = None, 0
        for _ in range(200):
            result = advance_stream(stream, adapter, page_bound=bound)
            previous, resets = result.check_continuation(previous=previous, resets=resets)
            stream = result.stream
            if result.exhausted:
                return stream
        pytest.fail("The stream did not become exhausted.")
    finally:
        adapter.close()


def messages(feed):
    """Read the records actually landed by the engine."""

    return apps.get_model("messaging", "Message").objects.filter(channel_id=feed.pk, platform="youtube")


def seed(feed, wire, *, count=2, hidden=False):
    """Prepare delivery context through posts' public landing owner."""

    raw = deepcopy(wire["threads_first"]["items"][0])
    if hidden:
        raw["snippet"]["topLevelComment"]["snippet"]["moderationStatus"] = "heldForReview"
    land_posts(feed, [
        video_post(wire["video_fresh"]["items"][0], channel_id=feed.external_id),
        comment_post(
            raw["snippet"]["topLevelComment"], video_id="video-fresh", channel_id=feed.external_id,
            reply_count=count, thread_id=raw["id"],
        ),
    ], owner_id=feed.owner_id, historical=True)
    return messages(feed).get(external_id="comment-a")


def reply(feed, wire, *, actor):
    """Prepare a held reply through the Message owner."""

    parent = seed(feed, wire)
    assert parent.with_actor(actor).has_access("read")
    return parent.reply_to_comment(body="Here is an example answer.", actor=actor)


def settle_uncertain_reply(message, error, *, monkeypatch):
    """Settle ambiguity, then claim reconciliation at the idle horizon."""

    moment = timezone.now()
    monkeypatch.setattr(timezone, "now", lambda: moment)
    model = type(message)
    with system_context(reason="test.youtube.delivery.uncertain"):
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
            moment + backoff, message.delivery_started_at + YouTubeFeedBackend.delivery_visibility_delay,
        )
        horizon = message.scheduled_at
        monkeypatch.setattr(timezone, "now", lambda: horizon)
        claimed = model.objects.claim_delivery(message.pk, token=message.external_id, reconcile=True)
        assert claimed is not None
        assert claimed.scheduled_at is None and claimed.delivery_lease_until > horizon
        return claimed


def history(wire):
    """Serve separate uploads, owner-moderation, top-comment and reply pages."""

    def respond(method, resource, query, kwargs):
        del method, kwargs
        if resource == "playlistItems":
            page = deepcopy(wire["uploads_last" if query.get("pageToken") else "uploads_first"])
            page["items"] = page["items"][:int(query["maxResults"])]
            return page
        if resource == "videos":
            return {"items": [
                raw for key in ("video_fresh", "video_archive") for raw in wire[key]["items"]
                if raw["id"] in query["id"].split(",")
            ]}
        if resource == "commentThreads":
            if query.get("moderationStatus", "published") != "published":
                return {"items": []}
            if query.get("videoId") == "video-fresh" or query.get("id") == "comment-a":
                return wire["threads_last" if query.get("pageToken") else "threads_first"]
        if resource == "comments":
            return wire["replies_last" if query.get("pageToken") else "replies_first"]
        return {"items": []}

    return respond


def activity(wire):
    """A fresh multi-page tail reaches an old thread before exhausting the channel."""

    def respond(method, resource, query, kwargs):
        del method, kwargs
        if resource == "videos":
            return wire["video_fresh"]
        if resource == "commentThreads":
            if query.get("id"):
                return {"items": [
                    raw for key in ("threads_first", "threads_last") for raw in wire[key]["items"]
                    if raw["id"] in query["id"].split(",")
                ]}
            if query["moderationStatus"] != "published":
                return {"items": []}
            token = query.get("pageToken")
            if token == "activity-old":
                return wire["threads_old"]
            if token == "activity-second":
                return {"items": wire["threads_last"]["items"], "nextPageToken": "activity-old"}
            return {"items": wire["threads_first"]["items"], "nextPageToken": "activity-second"}
        if resource == "comments":
            if query.get("parentId"):
                return wire["replies_last" if query.get("pageToken") else "replies_first"]
            resources = [
                raw["snippet"]["topLevelComment"] for key in ("threads_first", "threads_last")
                for raw in wire[key]["items"]
            ] + wire["replies_first"]["items"] + wire["replies_last"]["items"]
            return {"items": [raw for raw in resources if raw["id"] in query["id"].split(",")]}
        raise AssertionError(resource)

    return respond


def test_youtube_preset_seed_and_read_only_discovery_field(feed):
    config = apps.get_app_config("posts_integrate_youtube")
    resource = resource_manifest_for(config)["install"][0]
    entry = ResourceEntry.from_declaration(config, "install", resource)
    [group] = entry.read_groups()
    [client] = group.dataset.dict
    assert entry.adopt == ("slug", "environment")
    assert client["_xref"] == "oauth_youtube" and client["slug"] == YouTubeFeedBackend.oauth_client
    assert client["provider_type"] == GoogleYouTube.key
    assert "client_id" not in client and "client_secret" not in client
    assert settings.ANGEE_OAUTH_PROVIDER_TYPE_CLASSES[GoogleYouTube.key].endswith(".GoogleYouTube")
    assert GoogleYouTube.defaults["supports_refresh"] and GoogleYouTube.defaults["supports_pkce"]
    assert "uploads_playlist_id" not in YouTubeFeedBackend.Config.model_fields
    assert not feed._meta.get_field("youtube_uploads_playlist_id").editable
    assert [(item.key, item.partition) for item in feed.feed_backend.streams()] == [
        ("activity", "channel-demo"), ("history", "channel-demo"), ("reobserve", "channel-demo"),
    ]


def test_youtube_discovery_separates_network_facts_from_database_apply(feed, monkeypatch, wire):
    calls = transport(monkeypatch, lambda *args: wire["channel"])
    before = (feed.external_id, feed.handle_id, feed.youtube_uploads_playlist_id, feed.credential.updated_at)
    discovery = feed.feed_backend.discover_connection(feed.credential)
    feed.refresh_from_db()
    assert isinstance(discovery, ConnectionDiscovery) and discovery.credential is None
    assert set(discovery.data) == {"external_id", "display_name", "handle", "uploads_playlist_id"}
    assert before == (feed.external_id, feed.handle_id, feed.youtube_uploads_playlist_id, feed.credential.updated_at)
    assert discovery.data["handle"].external_id == discovery.data["external_id"] == "channel-demo"
    assert calls[0][2] == {"part": "id,snippet,contentDetails", "mine": "true"}
    feed.youtube_uploads_playlist_id = ""
    feed.save(update_fields=["youtube_uploads_playlist_id", "updated_at"])
    with transaction.atomic():
        feed.feed_backend.apply_discovery(discovery)
    feed.refresh_from_db()
    assert feed.youtube_uploads_playlist_id == "uploads-demo" and feed.display_name == "Example Channel"
    assert feed.handle.external_id == "channel-demo"


@pytest.mark.parametrize("selection", ["empty", "multiple", "other", "no_uploads"])
def test_youtube_discovery_refuses_unusable_channel(feed, monkeypatch, wire, selection):
    payload = deepcopy(wire["channel"])
    if selection == "empty":
        payload["items"] = []
    elif selection == "multiple":
        payload["items"] *= 2
    elif selection == "other":
        payload["items"][0]["id"] = "channel-other"
    else:
        payload["items"][0]["contentDetails"] = {}
    transport(monkeypatch, lambda *args: payload)
    with pytest.raises(IntegrationError):
        feed.feed_backend.discover_connection(feed.credential)


def test_youtube_history_engine_resumes_all_page_types_and_skips_zero_comments(feed, monkeypatch, wire):
    calls = transport(monkeypatch, history(wire))
    stream = drain(feed, source(feed))
    assert set(messages(feed).values_list("external_id", flat=True)) == {
        "video-fresh", "video-archive", "comment-a", "comment-b", "reply-a", "reply-b",
    }
    assert stream.cursor["stage"] == "complete"
    assert {query["pageToken"] for _, _, query, _ in calls if query.get("pageToken")} == {
        "uploads-p2", "threads-p2", "replies-p2",
    }
    assert not any(query.get("videoId") == "video-archive" for _, _, query, _ in calls)
    assert not any("allThreadsRelatedToChannelId" in query for _, _, query, _ in calls)
    assert messages(feed).get(external_id="reply-b").parent.external_id == "comment-a"


def test_youtube_history_batches_fifty_video_ids(feed, monkeypatch, wire):
    uploads = {"items": [{"contentDetails": {"videoId": f"video-batch-{index}"}} for index in range(50)]}

    def respond(method, resource, query, kwargs):
        del method, kwargs
        if resource == "playlistItems":
            return uploads
        assert resource == "videos"
        assert len(query["id"].split(",")) == 50
        return {"items": [
            {**deepcopy(wire["video_archive"]["items"][0]), "id": identity}
            for identity in query["id"].split(",")
        ]}

    calls = transport(monkeypatch, respond)
    stream = drain(feed, source(feed), bound=100)
    assert stream.cursor["stage"] == "complete" and messages(feed).count() == 50
    assert [resource for _, resource, _, _ in calls] == ["playlistItems", "videos"]


@pytest.mark.parametrize("live_days, active_days, expected", [(2, 30, 2), (90, 30, 30)])
def test_youtube_activity_first_pass_stops_at_live_or_age_bound(
    feed, monkeypatch, wire, live_days, active_days, expected,
):
    feed.live_since = AT - timedelta(days=live_days)
    feed.config = {"active_thread_days": active_days}
    feed.save(update_fields=["live_since", "config", "updated_at"])
    calls = transport(monkeypatch, activity(wire))
    row = source(feed, "activity")
    first = step(feed, row)
    assert timestamp(first.stream.cursor["floor"]) == AT - timedelta(days=expected)
    row = drain(feed, first.stream)
    assert row.cursor["newest_published_at"] == "2026-10-09T09:00:00Z"
    assert row.cursor["stage"] == "idle"
    assert not messages(feed).filter(external_id="comment-old").exists()
    assert not any(query.get("pageToken") == "activity-unneeded" for _, _, query, _ in calls)
    previous = len(calls)
    row = drain(feed, row)
    assert row.cursor["newest_published_at"] == "2026-10-09T09:00:00Z"
    assert len(calls) == previous + 2  # Re-read the watermark second, then stop at the older page.


def test_youtube_activity_rechecks_changed_reply_counts_hourly(feed, monkeypatch, wire):
    seed(feed, wire, count=1)

    def respond(method, resource, query, kwargs):
        del method, kwargs
        if resource == "commentThreads":
            return {"items": wire["threads_first"]["items"]} if query.get("id") else {"items": []}
        if query.get("parentId"):
            return wire["replies_last" if query.get("pageToken") else "replies_first"]
        return {"items": [
            wire["threads_first"]["items"][0]["snippet"]["topLevelComment"],
            *wire["replies_first"]["items"], *wire["replies_last"]["items"],
        ]}

    calls = transport(monkeypatch, respond)
    row = drain(feed, source(feed, "reobserve"))
    assert messages(feed).filter(external_id__in=["reply-a", "reply-b"]).count() == 2
    assert sum(bool(query.get("parentId")) for _, _, query, _ in calls) == 2
    assert any(
        query.get("id") == "comment-a"
        for _, resource, query, _ in calls if resource == "commentThreads"
    )
    previous = len(calls)
    drain(feed, row)
    assert len(calls) == previous


def test_youtube_first_activity_scan_includes_the_exact_live_since_boundary(feed, monkeypatch, wire):
    payload = deepcopy(wire["threads_last"])
    payload["items"][0]["snippet"]["topLevelComment"]["snippet"]["publishedAt"] = feed.live_since.isoformat()
    cursor = ActivityCursor().model_dump(mode="json")

    def respond(method, resource, query, kwargs):
        del method, query, kwargs
        return wire["video_fresh"] if resource == "videos" else payload

    transport(monkeypatch, respond)
    drain(feed, source(feed, "activity", cursor))
    assert messages(feed).filter(external_id="comment-b").exists()


def test_youtube_recheck_id_filter_caps_batches_at_fifty(feed, monkeypatch, wire):
    root = video_post(wire["video_fresh"]["items"][0], channel_id=feed.external_id)
    tops = []
    for index in range(51):
        raw = deepcopy(wire["threads_last"]["items"][0]["snippet"]["topLevelComment"])
        raw["id"] = f"comment-batch-{index}"
        tops.append(comment_post(
            raw, video_id="video-fresh", channel_id=feed.external_id, thread_id=f"thread-batch-{index}",
        ))
    land_posts(feed, [root, *tops], owner_id=feed.owner_id, historical=True)
    calls = transport(monkeypatch, lambda *args: {"items": []})
    row = source(feed, "reobserve", ReobserveCursor(stage="threads").model_dump(mode="json"))
    drain(feed, row, bound=100)
    batches = [query["id"].split(",") for _, resource, query, _ in calls if resource == "commentThreads"]
    assert list(map(len, batches)) == [50, 1]


def test_youtube_deadline_between_requests_preserves_only_pending_identities(feed, monkeypatch, wire):
    clock = [0.0]
    monkeypatch.setattr(youtube, "monotonic", lambda: clock[0])

    def respond(method, resource, query, kwargs):
        if resource == "commentThreads" and query.get("allThreadsRelatedToChannelId"):
            clock[0] = 20.0
            return wire["threads_last"]
        return activity(wire)(method, resource, query, kwargs)

    calls = transport(monkeypatch, respond)
    result = step(feed, source(feed, "activity"), deadline=10.0)
    assert not result.exhausted and result.count == 0 and len(calls) == 1
    assert result.stream.cursor["pending"] == [{
        "thread_id": "comment-b", "video_id": "video-fresh", "moderation": "published",
    }]
    serialized = json.dumps(result.stream.cursor)
    assert "Thanks" not in serialized and "author-other" not in serialized
    step(feed, result.stream)
    assert messages(feed).filter(external_id="comment-b").exists()


def test_youtube_expired_deadline_is_a_partial_page_without_http_failure(feed, monkeypatch):
    monkeypatch.setattr(youtube, "monotonic", lambda: 20.0)
    calls = transport(monkeypatch, lambda *args: pytest.fail("An expired budget must not start HTTP."))
    result = step(feed, source(feed, "activity"), deadline=10.0)
    assert not result.exhausted and not result.reset and result.count == 0 and calls == []
    assert result.stream.cursor["floor"]


def test_youtube_sync_engine_settles_a_deadline_with_durable_pending_work(feed, monkeypatch, wire):
    clock = [0.0]
    monkeypatch.setattr(youtube, "monotonic", lambda: clock[0])
    monkeypatch.setattr(stream_engine, "monotonic", lambda: clock[0])
    feed.config = {"sync_time_budget": 10}
    feed.discovery_retry_at = None
    feed.discovery_completed_at = AT
    feed.save(update_fields=["config", "discovery_retry_at", "discovery_completed_at", "updated_at"])

    def respond(method, resource, query, kwargs):
        del method, resource, query, kwargs
        clock[0] = 20.0
        return wire["threads_last"]

    calls = transport(monkeypatch, respond)
    assert sync_bridge(feed) == 0
    row = SyncStream.objects.current_for_bridge(feed, "activity").get()
    assert row.cursor["pending"][0]["thread_id"] == "comment-b" and len(calls) == 1


def test_youtube_sync_engine_accepts_an_unchanged_partial_page_at_the_deadline(feed, monkeypatch):
    clock, extracts = [0.0], [0]
    extract = YouTubeFeedBackend.extract

    def extract_at_deadline(backend, stream, page_bound, *, deadline=None):
        extracts[0] += 1
        if extracts[0] == 2:
            clock[0] = 20.0
        return extract(backend, stream, page_bound, deadline=deadline)

    monkeypatch.setattr(YouTubeFeedBackend, "extract", extract_at_deadline)
    monkeypatch.setattr(youtube, "monotonic", lambda: clock[0])
    monkeypatch.setattr(stream_engine, "monotonic", lambda: clock[0])
    feed.config = {"sync_time_budget": 10}
    feed.discovery_retry_at = None
    feed.discovery_completed_at = AT
    feed.save(update_fields=["config", "discovery_retry_at", "discovery_completed_at", "updated_at"])
    source(feed, "activity", ActivityCursor(
        floor=AT, page=PagePosition(token=None),
    ).model_dump(mode="json"))
    source(feed, "reobserve", ReobserveCursor(stage="threads").model_dump(mode="json"))
    calls = transport(monkeypatch, lambda *args: pytest.fail("There is no provider work on this page."))
    assert sync_bridge(feed) == 0 and calls == []
    assert extracts[0] == 2
    assert SyncStream.objects.current_for_bridge(feed, "reobserve").get().cursor["stage"] == "threads"


@pytest.mark.parametrize("key, cursor, field", [
    ("activity", ActivityCursor(
        newest_published_at=AT - timedelta(days=1), floor=AT - timedelta(days=1),
        page=PagePosition(token="expired"),
    ).model_dump(mode="json"), "page"),
    ("history", HistoryCursor(uploads=PagePosition(token="expired")).model_dump(mode="json"), "uploads"),
    ("history", HistoryCursor(
        stage="threads", videos=["video-fresh"], uploads=PagePosition(token="uploads-later"),
        threads=PagePosition(token="expired"),
    ).model_dump(mode="json"), "threads"),
])
def test_youtube_engine_resets_only_the_affected_page_position(feed, monkeypatch, wire, key, cursor, field):
    transport(monkeypatch, lambda *args: (400, wire["invalid_page_token"], {}))
    row = source(feed, key, cursor)
    unaffected = deepcopy(row.cursor)
    result = step(feed, row)
    assert result.reset and result.stream.generation == row.generation + 1
    affected = result.stream.cursor
    assert affected[field] == {"token": "", "seen": [], "resets": 1}
    assert affected["force"]
    for name, value in unaffected.items():
        if name not in {field, "force", "cycle_newest"}:
            assert affected[name] == value


def test_youtube_reply_cursor_reset_preserves_uploads_and_other_reply_work(feed, monkeypatch, wire):
    cursor = HistoryCursor(
        stage="threads", videos=["video-fresh"], uploads=PagePosition(token="uploads-later"),
        replies=[
            ReplyPosition(parent_id="comment-a", video_id="video-fresh", page=PagePosition(token="expired")),
            ReplyPosition(parent_id="comment-b", video_id="video-fresh"),
        ],
    ).model_dump(mode="json")
    transport(monkeypatch, lambda *args: (400, wire["invalid_page_token"], {}))
    result = step(feed, source(feed, cursor=cursor))
    assert result.reset and result.stream.cursor["uploads"]["token"] == "uploads-later"
    assert result.stream.cursor["videos"] == ["video-fresh"]
    assert result.stream.cursor["replies"][0]["page"]["resets"] == 1
    assert result.stream.cursor["replies"][1] == cursor["replies"][1]


def test_youtube_repeated_token_quarantines_the_walk_after_one_reset(feed, monkeypatch):
    transport(monkeypatch, lambda *args: {"items": [], "nextPageToken": "repeat"})
    cursor = HistoryCursor(
        stage="threads", videos=["video-fresh"], uploads=PagePosition(token="uploads-later"),
        threads=PagePosition(token="repeat"),
    ).model_dump(mode="json")
    reset = step(feed, source(feed, cursor=cursor))
    assert reset.reset
    row = step(feed, reset.stream).stream
    result = step(feed, row)
    assert not result.reset and result.stream.cursor["threads"]["token"] is None
    assert result.stream.cursor["uploads"]["token"] == "uploads-later"
    assert bool(_failures(feed, "youtube_page_token_loop"))


@pytest.mark.parametrize("key", ["activity", "history"])
def test_youtube_deleted_reply_parent_cannot_wedge_the_stream(feed, monkeypatch, key):
    model = ActivityCursor if key == "activity" else HistoryCursor
    cursor = model(replies=[ReplyPosition(parent_id="comment-gone", video_id="video-fresh")]).model_dump(mode="json")
    refusal = {"error": {"errors": [{"reason": "parentCommentNotFound"}]}}
    calls = transport(monkeypatch, lambda *args: (404, refusal, {}))
    row = step(feed, source(feed, key, cursor)).stream
    assert row.cursor["replies"] == [] and len(calls) == 1
    assert _one_failure(feed, "youtube_replies_unavailable").details == {"external_key": "comment-gone"}


@pytest.mark.parametrize("key", ["activity", "history"])
def test_youtube_refused_deferred_thread_is_removed_from_the_cursor(feed, monkeypatch, key):
    model = ActivityCursor if key == "activity" else HistoryCursor
    cursor = model(pending=[ThreadIdentity(
        thread_id="thread-unavailable", video_id="video-fresh",
    )]).model_dump(mode="json")
    refusal = {"error": {"errors": [{"reason": "forbidden"}]}}
    transport(monkeypatch, lambda *args: (403, refusal, {}))
    result = step(feed, source(feed, key, cursor))
    assert result.stream.cursor["pending"] == []
    assert _one_failure(feed, "youtube_thread_unavailable").details == {
        "external_key": "thread-unavailable",
    }


def test_youtube_disabled_video_does_not_block_later_history_videos(feed, monkeypatch):
    cursor = HistoryCursor(
        stage="threads", videos=["video-disabled", "video-fresh"], uploads=PagePosition(token=None),
    ).model_dump(mode="json")

    def respond(method, resource, query, kwargs):
        del method, resource, kwargs
        if query.get("videoId") == "video-disabled":
            return 403, {"error": {"errors": [{"reason": "commentsDisabled"}]}}, {}
        return {"items": []}

    calls = transport(monkeypatch, respond)
    row = step(feed, source(feed, cursor=cursor)).stream
    assert row.cursor["videos"] == ["video-fresh"]
    step(feed, row)
    assert [query["videoId"] for _, _, query, _ in calls] == ["video-disabled", "video-fresh"]
    assert bool(_failures(feed, "youtube_video_comments_unavailable"))


def test_youtube_refused_recheck_advances_beyond_the_thread(feed, monkeypatch, wire):
    seed(feed, wire)
    cursor = ReobserveCursor(stage="threads").model_dump(mode="json")
    refusal = {"error": {"errors": [{"reason": "commentNotFound"}]}}
    calls = transport(monkeypatch, lambda *args: (404, refusal, {}))
    row = step(feed, source(feed, "reobserve", cursor)).stream
    assert row.cursor["active_after"] > 0
    result = step(feed, row)
    assert result.stream.cursor["stage"] == "comments" and len(calls) == 1
    assert bool(_failures(feed, "youtube_thread_unavailable"))


def test_youtube_malformed_reply_is_recorded_and_does_not_hold_the_parent_cursor(feed, monkeypatch):
    cursor = HistoryCursor(replies=[ReplyPosition(
        parent_id="comment-a", video_id="video-fresh",
    )]).model_dump(mode="json")
    transport(monkeypatch, lambda *args: {"items": [{"id": "reply-malformed", "snippet": None}]})
    result = step(feed, source(feed, cursor=cursor))
    assert result.count == 0 and result.stream.cursor["replies"] == []
    assert bool(_failures(feed, "youtube_malformed_reply"))


@pytest.mark.parametrize("key", ["activity", "history"])
def test_youtube_malformed_thread_is_skipped_and_progress_is_committed(feed, monkeypatch, key):
    cursor = None if key == "activity" else HistoryCursor(
        stage="threads", videos=["video-fresh"],
    ).model_dump(mode="json")
    transport(monkeypatch, lambda *args: {"items": [{"id": "thread-malformed"}, None]})
    result = step(feed, source(feed, key, cursor))
    assert result.count == 0 and result.stream.cursor != (cursor or {})
    discrepancies = _failures(feed, "youtube_malformed_thread")
    assert sum(discrepancy.attempts for discrepancy in discrepancies) == 2
    assert len(discrepancies) == 2
    assert {discrepancy.details["external_key"] for discrepancy in discrepancies} == {
        "thread-malformed", "channel-demo:malformed_thread:1",
    }


def test_youtube_single_record_pages_land_roots_before_deferred_comments(feed, monkeypatch, wire):
    calls = transport(monkeypatch, history(wire))
    row = drain(feed, source(feed), bound=1)
    assert row.cursor["stage"] == "complete"
    identities = ["video-fresh", "comment-a", "comment-b", "reply-a", "reply-b"]
    assert messages(feed).filter(external_id__in=identities).count() == 5
    assert all(len(query.get("id", "").split(",")) <= 50 for _, _, query, _ in calls)


@pytest.mark.parametrize("moderation", ["heldForReview", "likelySpam"])
def test_youtube_owner_moderation_scan_emits_hidden_posts_and_suppresses_triggers(feed, monkeypatch, wire, moderation):
    held = deepcopy(wire["threads_held"])
    held["items"][0]["snippet"]["topLevelComment"]["snippet"].pop("moderationStatus")
    seen = []

    def respond(method, resource, query, kwargs):
        del method, kwargs
        if resource == "videos":
            return wire["video_fresh"]
        if resource == "commentThreads":
            return held if query.get("moderationStatus") == moderation else {"items": []}
        raw = deepcopy(held["items"][0]["snippet"]["topLevelComment"])
        raw["snippet"]["moderationStatus"] = moderation
        return {"items": [raw]}

    def ingested(sender, instance, **kwargs):
        del sender, kwargs
        seen.append(instance.external_id)

    calls = transport(monkeypatch, respond)
    message_ingested.connect(ingested, weak=False)
    try:
        drain(feed, source(feed, "reobserve"))
    finally:
        message_ingested.disconnect(ingested)
    held = messages(feed).get(external_id="comment-held")
    assert held.is_trashed and held.local_metadata["provider_hidden"] == held.trashed_at.isoformat()
    assert "comment-held" not in seen
    assert any(query.get("moderationStatus") == moderation for _, _, query, _ in calls)


@pytest.mark.parametrize("moderation", ["heldForReview", "likelySpam"])
def test_youtube_owner_scan_hides_a_previously_landed_comment(feed, monkeypatch, wire, moderation):
    seed(feed, wire, count=0)
    raw = deepcopy(wire["threads_first"]["items"][0])
    raw["snippet"]["totalReplyCount"] = 0
    raw["snippet"]["topLevelComment"]["snippet"].pop("moderationStatus", None)

    def respond(method, resource, query, kwargs):
        if resource == "commentThreads":
            if query.get("id") or query.get("moderationStatus") == moderation:
                return {"items": [raw]}
            return {"items": []}
        return {"items": [raw["snippet"]["topLevelComment"]]}

    transport(monkeypatch, respond)
    drain(feed, source(feed, "reobserve"))
    held = messages(feed).get(external_id="comment-a")
    assert held.is_trashed and held.local_metadata["provider_hidden"] == held.trashed_at.isoformat()


def test_youtube_missing_reobserved_comment_records_absence_without_claiming_deletion(feed, monkeypatch, wire):
    seed(feed, wire)
    transport(monkeypatch, lambda *args: {"items": []})
    cursor = ReobserveCursor(stage="comments").model_dump(mode="json")
    drain(feed, source(feed, "reobserve", cursor))
    assert not messages(feed).get(external_id="comment-a").is_trashed
    assert _one_failure(feed, "youtube_comment_unavailable").details == {"external_key": "comment-a"}


def test_youtube_polling_limit_protects_reply_units_on_the_real_ledger(feed, replier, monkeypatch, wire):
    message = reply(feed, wire, actor=replier)
    feed.config = {"quota_limit": 100, "reply_reserve": 50}
    feed.save(update_fields=["config", "updated_at"])
    quota = apps.get_model("posts", "Quota").objects
    now = AT.astimezone(ZoneInfo(YouTubeFeedBackend.quota_reset_timezone))
    period = quota.open_period(
        integration=feed, scope=feed.feed_backend.quota_scope(), reset_timezone="America/Los_Angeles", now=now,
    )
    quota.filter(pk=period.pk).update(quota_used=50)
    calls = transport(monkeypatch, lambda *args: wire["inserted_reply"])
    with pytest.raises(IntegrationError) as caught:
        step(feed, source(feed, "activity"))
    assert caught.value.transient and caught.value.retry_after == period.period_end - now and calls == []
    assert feed.feed_backend.deliver(message) == DeliveryOutcome(True, "reply-published")
    period.refresh_from_db()
    assert period.quota_used == 100
    assert period.period_start.astimezone(ZoneInfo(YouTubeFeedBackend.quota_reset_timezone)).hour == 0


def test_youtube_provider_quota_refusal_retains_the_period_retry_hint(feed, monkeypatch, wire):
    transport(monkeypatch, lambda *args: (403, wire["quota_exceeded"], {}))
    with pytest.raises(IntegrationError) as caught:
        step(feed, source(feed, "activity"))
    period = apps.get_model("posts", "Quota").objects.get(integration=feed)
    assert caught.value.transient and caught.value.retry_after == period.period_end - AT


def test_youtube_revoked_grant_uses_the_integrate_refusal_without_credential_writes(feed, monkeypatch, wire):
    credential = feed.credential
    before = (credential.status, credential.updated_at)
    transport(monkeypatch, lambda *args: (401, wire["revoked_grant"], {}))
    with pytest.raises(IntegrationError) as caught:
        step(feed, source(feed, "activity"))
    credential.refresh_from_db()
    assert not caught.value.transient and "reconnect" in caught.value.public_message
    assert before == (credential.status, credential.updated_at)


def test_youtube_delivery_inserts_once_without_identical_reply_coalescing(feed, replier, monkeypatch, wire):
    message = reply(feed, wire, actor=replier)

    def respond(method, resource, query, kwargs):
        del query
        assert method == "POST" and resource == "comments"
        assert json.loads(kwargs["body"]) == {
            "snippet": {"parentId": "comment-a", "textOriginal": "Here is an example answer."},
        }
        return wire["inserted_reply"]

    calls = transport(monkeypatch, respond)
    assert feed.feed_backend.deliver(message) == DeliveryOutcome(True, "reply-published")
    assert len(calls) == 1
    assert apps.get_model("posts", "Quota").objects.get(integration=feed).quota_used == CALL_COSTS["comments.insert"]


def test_youtube_ambiguous_send_reconciles_only_after_settlement(feed, replier, monkeypatch, wire):
    message = claim_feed_reply(reply(feed, wire, actor=replier), actor=feed.owner)
    published = deepcopy(wire["replies_last"])
    published["items"][0]["snippet"]["publishedAt"] = AT.isoformat()
    calls = transport(monkeypatch, lambda method, *args: (
        httpx2.ReadTimeout("Synthetic acknowledgement loss") if method == "POST" else published
    ))
    with pytest.raises(DeliveryUncertain) as raised:
        feed.feed_backend.deliver(message)
    recovered = settle_uncertain_reply(message, raised.value, monkeypatch=monkeypatch)
    assert feed.feed_backend.deliver(recovered) == DeliveryOutcome(True, "reply-b")
    assert [method for method, _, _, _ in calls] == ["POST", "GET"]


@pytest.mark.parametrize("lookup", ["throttle", "expired", "loop", "not_indexed"])
def test_youtube_ambiguous_lookup_failure_never_authorizes_resend(feed, replier, monkeypatch, wire, lookup):
    message = claim_feed_reply(reply(feed, wire, actor=replier), actor=feed.owner)

    def respond(method, resource, query, kwargs):
        if method == "POST":
            return httpx2.RemoteProtocolError("Synthetic acknowledgement loss")
        if lookup == "throttle":
            return 429, wire["rate_limited"], {"Retry-After": "60"}
        if lookup == "expired":
            if not query.get("pageToken"):
                return {"items": [], "nextPageToken": "expired"}
            return 400, wire["invalid_page_token"], {}
        if lookup == "loop":
            return {"items": [], "nextPageToken": "repeat"}
        return {"items": []}

    calls = transport(monkeypatch, respond)
    with pytest.raises(DeliveryUncertain) as raised:
        feed.feed_backend.deliver(message)
    recovered = settle_uncertain_reply(message, raised.value, monkeypatch=monkeypatch)
    with pytest.raises(DeliveryUncertain):
        feed.feed_backend.deliver(recovered)
    assert sum(method == "POST" for method, _, _, _ in calls) == 1


@pytest.mark.parametrize("reason, phrase", [
    ("commentTextTooLong", "length"), ("parentCommentNotFound", "parent"), ("commentsDisabled", "disabled"),
])
def test_youtube_delivery_reports_specific_permanent_refusals(feed, replier, monkeypatch, wire, reason, phrase):
    message = reply(feed, wire, actor=replier)
    transport(monkeypatch, lambda *args: (400, {"error": {"errors": [{"reason": reason}]}}, {}))
    with pytest.raises(IntegrationError) as caught:
        feed.feed_backend.deliver(message)
    assert not caught.value.transient and phrase in caught.value.public_message


def test_youtube_certain_connect_failure_is_retryable(feed, replier, monkeypatch, wire):
    message = reply(feed, wire, actor=replier)
    transport(monkeypatch, lambda *args: RequestNotSentError())
    with pytest.raises(RequestNotSentError):
        feed.feed_backend.deliver(message)


@pytest.mark.parametrize("status, fixture", [(429, "rate_limited"), (403, "rate_limited_403")])
def test_youtube_delivery_and_polling_throttle_keep_retry_hints(
    feed, replier, monkeypatch, wire, status, fixture,
):
    message = reply(feed, wire, actor=replier)
    transport(monkeypatch, lambda *args: (status, wire[fixture], {"Retry-After": "120"}))
    with pytest.raises(IntegrationError) as delivery:
        feed.feed_backend.deliver(message)
    with pytest.raises(IntegrationError) as polling:
        step(feed, source(feed, "activity"))
    assert delivery.value.retry_after == polling.value.retry_after == timedelta(seconds=120)


def test_youtube_public_mapping_uses_resource_ids_and_excludes_seo_keywords(wire):
    root = video_post(wire["video_fresh"]["items"][0], channel_id="channel-demo")
    raw = wire["threads_first"]["items"][0]
    top = comment_post(raw["snippet"]["topLevelComment"], video_id="video-fresh", channel_id="channel-demo")
    child = comment_post(wire["replies_last"]["items"][0], video_id="video-fresh", channel_id="channel-demo")
    assert root.message.external_id == root.message.thread.external_id == "video-fresh"
    assert root.tags == () and root.message.metadata == {}
    assert top.message.external_id == raw["id"] == "comment-a"
    assert top.message.in_reply_to == "video-fresh" and child.message.in_reply_to == "comment-a"
    assert child.message.direction == "outbound" and top.message.direction == "inbound"
    assert "author_display_name" not in top.message.metadata["youtube"]


@pytest.mark.parametrize("value", [None, "invalid", "2026-19-01T09:00:00Z", "2026-10-01T09:00:00"])
def test_youtube_unusable_publication_time_stays_unknown(value):
    assert timestamp(value) is None


def test_youtube_lost_lease_after_acceptance_reconciles_with_one_post(feed, replier, wire, monkeypatch):
    monkeypatch.setattr(celery_app, "send_task", lambda *args, **kwargs: None)
    message = claim_feed_reply(reply(feed, wire, actor=replier), actor=feed.owner)
    observed = deepcopy(wire["replies_last"])
    observed["items"][0]["id"] = "reply-published"
    observed["items"][0]["snippet"]["publishedAt"] = AT.isoformat()
    calls = transport(monkeypatch, lambda method, *args: wire["inserted_reply"] if method == "POST" else observed)
    accepted = feed.feed_backend.deliver(message)
    model = type(message)
    assert model.objects.claim_delivery(message.pk, token=message.external_id) is None
    model.objects.filter(pk=message.pk).update(delivery_lease_until=AT - timedelta(seconds=1))
    assert model.objects.release_held_messages() == 0
    message.refresh_from_db()
    assert message.status == "uncertain"
    assert message.delivery_lease_until is None
    assert message.scheduled_at == AT + feed.feed_backend.delivery_visibility_delay
    moment = message.scheduled_at
    monkeypatch.setattr(timezone, "now", lambda: moment)
    result = run_message_delivery(message._meta.label_lower, message.pk, message.external_id)
    assert result["delivered"] and result["status"] == "sent"
    assert [call[0] for call in calls] == ["POST", "GET"]
    message.refresh_from_db()
    assert message.external_id == accepted.provider_id


def test_youtube_two_channels_on_one_oauth_client_share_the_budget(feed, monkeypatch):
    from django.contrib.auth import get_user_model

    other_owner = get_user_model().objects.create_user(username="second-channel-owner")
    credential = apps.get_model("integrate", "Credential").objects.upsert_for_user(
        other_owner, feed.credential.oauth_client, CredentialKind.OAUTH, {"access_token": "synthetic-second-access"},
    )
    other = apps.get_model("posts", "Feed").objects.create(
        owner=other_owner, vendor=feed.vendor, credential=credential,
        backend_class="feed", feed_backend_class="youtube", external_id="channel-two",
        youtube_uploads_playlist_id="uploads-two", lifecycle="connected",
        config={"quota_limit": 101, "reply_reserve": 1},
    )
    feed.config = {"quota_limit": 101, "reply_reserve": 1}
    feed.save(update_fields=["config", "updated_at"])
    first = feed.feed_backend.consume_quota(60)
    second = other.feed_backend.consume_quota(40)
    assert first.pk == second.pk
    assert first.scope == f"youtube:oauth-client:{feed.credential.oauth_client_id}"
    with pytest.raises(IntegrationError) as raised:
        other.feed_backend.consume_quota(1)
    assert raised.value.transient
    first.refresh_from_db()
    assert first.quota_used == 100
    assert first.period_start.astimezone(ZoneInfo(YouTubeFeedBackend.quota_reset_timezone)).hour == 0


def test_youtube_approved_held_comment_restores_after_both_owner_scans(feed, wire, monkeypatch):
    held = seed(feed, wire, count=0, hidden=True)
    assert held.local_metadata["provider_hidden"] == held.trashed_at.isoformat()
    raw = deepcopy(wire["threads_first"]["items"][0])
    raw["snippet"]["totalReplyCount"] = 0
    raw["snippet"]["topLevelComment"]["snippet"].pop("moderationStatus", None)

    def respond(method, resource, query, kwargs):
        if resource == "commentThreads":
            if query.get("id") or query.get("moderationStatus") == "published":
                return {"items": [raw]}
            return {"items": []}
        return {"items": [raw["snippet"]["topLevelComment"]]}

    calls = transport(monkeypatch, respond)
    cursor = ReobserveCursor().model_dump(mode="json")
    drain(feed, source(feed, "reobserve", cursor))
    restored = messages(feed).get(external_id="comment-a")
    assert not restored.is_trashed and "provider_hidden" not in restored.local_metadata
    assert {query.get("moderationStatus") for _, resource, query, _ in calls if resource == "commentThreads"} >= {
        "heldForReview", "likelySpam",
    }


def test_youtube_incomplete_moderation_evidence_preserves_a_prior_hold(feed, wire, monkeypatch):
    held = seed(feed, wire, hidden=True)
    provenance = held.local_metadata["provider_hidden"]
    raw = deepcopy(wire["threads_first"]["items"][0]["snippet"]["topLevelComment"])
    raw["snippet"].pop("moderationStatus", None)
    transport(monkeypatch, lambda *args: {"items": [raw]})
    cursor = ReobserveCursor(stage="comments", moderation_complete=False).model_dump(mode="json")
    drain(feed, source(feed, "reobserve", cursor))
    held.refresh_from_db()
    assert held.is_trashed and held.local_metadata["provider_hidden"] == provenance == held.trashed_at.isoformat()


@pytest.mark.parametrize("read_path", ["comments", "threads", "replies", "parent"])
@pytest.mark.parametrize("moderation", ["provider", "operator", "stale"])
def test_youtube_hide_detection_uses_current_provider_provenance(
    feed, wire, monkeypatch, read_path, moderation,
):
    parent = seed(feed, wire, count=0, hidden=moderation != "operator" and read_path != "replies")
    thread = deepcopy(wire["threads_first"]["items"][0])
    thread["snippet"]["totalReplyCount"] = 0
    top = thread["snippet"]["topLevelComment"]
    top["snippet"].pop("moderationStatus", None)
    raw_reply = deepcopy(wire["replies_first"]["items"][0])
    raw_reply["snippet"].pop("moderationStatus", None)
    subject, identity = parent, parent.external_id
    if read_path in {"replies", "parent"}:
        land_posts(feed, [comment_post(
            raw_reply, video_id="video-fresh", channel_id=feed.external_id,
            moderation_status="heldForReview" if read_path == "replies" and moderation != "operator" else "published",
        )], owner_id=feed.owner_id, historical=True)
        identity = raw_reply["id"]
        if read_path == "replies":
            subject = messages(feed).get(external_id=identity)
    if moderation == "provider":
        assert subject.local_metadata["provider_hidden"] == subject.trashed_at.isoformat()
        subject.trash_reason = "A changed display label"
        subject.save(update_fields=["trash_reason", "updated_at"])
    else:
        if moderation == "stale":
            subject.restore()
            monkeypatch.setattr(timezone, "now", lambda: AT + timedelta(seconds=1))
        subject.trash(reason=PROVIDER_TRASH_REASON)
        if moderation == "operator":
            assert "provider_hidden" not in subject.local_metadata
        else:
            assert subject.local_metadata["provider_hidden"] != subject.trashed_at.isoformat()
    payload = {
        "threads": [thread], "comments": [top], "replies": [raw_reply], "parent": [top, raw_reply],
    }[read_path]
    calls = transport(monkeypatch, lambda *args: {"items": payload})
    cursor = ReobserveCursor(
        stage="comments" if read_path in {"comments", "parent"} else "threads", force=True,
        moderation_complete=read_path == "parent", moderation_floor=AT - timedelta(days=30),
        replies=[ReplyPosition(parent_id=parent.external_id, video_id="video-fresh")] if read_path == "replies" else [],
    )
    state = source(feed, "reobserve", cursor.model_dump(mode="json"))
    adapter = feed.feed_backend
    try:
        page = adapter.extract(state, 10)
    finally:
        adapter.close()
    observed = next(post for post in page.records if post.message.external_id == identity)
    assert observed.hidden is (moderation == "provider")
    assert len(calls) == 1


def test_youtube_same_second_watermark_ingests_the_other_comment(feed, wire, monkeypatch):
    raw = deepcopy(wire["threads_last"]["items"][0])
    raw["snippet"]["topLevelComment"]["snippet"]["publishedAt"] = AT.isoformat()
    calls = transport(monkeypatch, lambda method, resource, *args: (
        wire["video_fresh"] if resource == "videos" else {"items": [raw]}
    ))
    cursor = ActivityCursor(newest_published_at=AT, floor=AT)
    drain(feed, source(feed, "activity", cursor.model_dump(mode="json")))
    assert messages(feed).filter(external_id="comment-b").exists() and len(calls) == 2


def test_youtube_reobservation_budget_pauses_and_resumes_the_unlanded_page(feed, wire, monkeypatch):
    seed(feed, wire, count=0)
    second = wire["threads_last"]["items"][0]["snippet"]["topLevelComment"]
    land_posts(feed, [comment_post(second, video_id="video-fresh", channel_id=feed.external_id)],
               owner_id=feed.owner_id, historical=True)
    raws = [wire["threads_first"]["items"][0]["snippet"]["topLevelComment"], second]
    calls = transport(monkeypatch, lambda method, resource, query, kwargs: {
        "items": [raw for raw in raws if raw["id"] in query["id"].split(",")],
    })
    feed.config = {"reobserve_units_per_cycle": 1}
    feed.save(update_fields=["config", "updated_at"])
    cursor = ReobserveCursor(stage="comments")
    state = source(feed, "reobserve", cursor.model_dump(mode="json"))
    adapter = feed.feed_backend
    first = advance_stream(state, adapter, page_bound=1)
    paused = advance_stream(first.stream, adapter, page_bound=1)
    assert paused.exhausted and paused.stream.cursor == first.stream.cursor
    assert len(calls) == 1 and adapter._reobserve_units_used == 1
    resumed = step(feed, paused.stream, bound=1)
    assert resumed.count == 1 and len(calls) == 2


def test_youtube_quarantine_is_owned_by_the_page_transaction(feed, monkeypatch):
    calls = transport(monkeypatch, lambda *args: {"items": [{"id": "thread-malformed"}]})
    state = source(feed, "activity")
    page = feed.feed_backend.extract(state, 10)
    assert len(page.records) == 1 and calls
    assert not _failures(feed)

    def fail_page(*args):
        raise RuntimeError("Synthetic page failure")

    monkeypatch.setattr(YouTubeFeedBackend, "finish_page", fail_page)
    with pytest.raises(RuntimeError):
        step(feed, state)
    assert not _failures(feed)
    state.refresh_from_db()
    assert not state.cursor


def test_youtube_lookup_does_not_assume_provider_order_and_matches_anchor_and_window(
    feed, replier, wire, monkeypatch,
):
    message = reply(feed, wire, actor=replier)
    raw = deepcopy(wire["replies_last"]["items"][0])
    raw["snippet"]["publishedAt"] = AT.isoformat()
    old = deepcopy(raw)
    old["snippet"]["publishedAt"] = (AT - timedelta(days=1)).isoformat()
    foreign = deepcopy(raw)
    foreign["snippet"]["parentId"] = "comment-b"
    calls = transport(monkeypatch, lambda method, resource, query, kwargs: (
        {"items": [raw]} if query.get("pageToken") else {"items": [old, foreign], "nextPageToken": "next"}
    ))
    assert feed.feed_backend.find_published_reply(message, since=AT) == "reply-b"
    assert len(calls) == 2


@pytest.mark.parametrize("address", ["8.8.8.8", "127.0.0.1"])
def test_youtube_pinned_client_connect_error_is_native_not_sent(feed, replier, wire, monkeypatch, address):
    import ipaddress

    import httpcore2

    from angee.integrate import http as integrate_http

    monkeypatch.setattr(integrate_http, "resolved_addresses", lambda *args: (ipaddress.ip_address(address),))
    connections = []

    def fail_connect(*args, **kwargs):
        connections.append(True)
        raise httpcore2.ConnectError("Synthetic connect refusal")

    monkeypatch.setattr(httpcore2.SyncBackend, "connect_tcp", fail_connect)
    with pytest.raises(IntegrationError) as raised:
        feed.feed_backend.deliver(reply(feed, wire, actor=replier))
    # A refused connect is retryable as not sent; the SSRF policy refusal is permanent.
    assert isinstance(raised.value, RequestNotSentError) is (address == "8.8.8.8")
    assert raised.value.transient is (address == "8.8.8.8")
    assert len(connections) == (1 if address == "8.8.8.8" else 0)


def test_youtube_quarantine_overflow_respects_the_record_page_bound(feed, wire, monkeypatch):
    raw = deepcopy(wire["threads_first"]["items"][0])
    raw["snippet"]["topLevelComment"]["snippet"]["textDisplay"] = {"invalid": "text"}
    calls = transport(monkeypatch, lambda method, resource, *args: (
        wire["video_fresh"] if resource == "videos" else {"items": [raw]}
    ))
    cursor = HistoryCursor(stage="threads", videos=["video-fresh"])
    first = step(feed, source(feed, cursor=cursor.model_dump(mode="json")), bound=1)
    assert first.count == 1 and first.stream.cursor["refusals"][0]["code"] == "youtube_malformed_comment"
    previous = len(calls)
    second = step(feed, first.stream, bound=1)
    assert second.count == 0 and len(calls) == previous and not second.stream.cursor["refusals"]
    assert bool(_failures(feed, "youtube_malformed_comment"))


@pytest.mark.parametrize("budget", [0, 1])
def test_youtube_reobservation_budget_exhaustion_does_not_stop_live_comments(feed, wire, monkeypatch, budget):
    seed(feed, wire, count=0)
    feed.config = {"reobserve_units_per_cycle": budget}
    feed.save(update_fields=["config", "updated_at"])
    calls = transport(monkeypatch, activity(wire))
    state = source(feed, "reobserve", ReobserveCursor(stage="comments").model_dump(mode="json"))
    adapter = feed.feed_backend
    if budget:
        assert adapter.consume_reobserve_quota(1)
    previous = len(calls)
    paused = advance_stream(state, adapter)
    assert paused.exhausted and paused.stream.cursor == state.cursor and len(calls) == previous
    live = source(feed, "activity")
    for _ in range(20):
        result = advance_stream(live, adapter)
        live = result.stream
        if result.exhausted:
            break
    assert result.exhausted and messages(feed).filter(external_id="comment-b").exists()
    assert adapter._reobserve_units_used == budget


def test_youtube_daily_exhaustion_stops_only_reobservation_without_cycle_debit(feed, wire, monkeypatch):
    seed(feed, wire, count=0)
    feed.config = {"quota_limit": 2, "reply_reserve": 1}
    feed.save(update_fields=["config", "updated_at"])
    adapter = feed.feed_backend
    period = adapter.consume_quota(1)
    calls = transport(monkeypatch, lambda *args: pytest.fail("Exhausted polling reached YouTube"))
    state = source(feed, "reobserve", ReobserveCursor(stage="comments").model_dump(mode="json"))
    paused = advance_stream(state, adapter)
    assert paused.exhausted and paused.stream.cursor == state.cursor and adapter._reobserve_units_used == 0
    period.refresh_from_db()
    assert period.quota_used == 1 and not calls


def test_youtube_small_reobservation_budget_commits_thread_work_and_missing_root(feed, wire, monkeypatch):
    held = deepcopy(wire["threads_held"])
    calls = transport(monkeypatch, lambda method, resource, query, kwargs: (
        wire["video_fresh"] if resource == "videos" else held
    ))
    feed.config = {"reobserve_units_per_cycle": 1}
    feed.save(update_fields=["config", "updated_at"])
    state = source(feed, "reobserve")
    first = drain(feed, state)
    assert first.cursor["pending"] and len(calls) == 1
    second = drain(feed, first)
    assert messages(feed).filter(external_id="video-fresh").exists() and second.cursor["pending"]
    third = step(feed, second)
    assert messages(feed).get(external_id="comment-held").is_trashed
    assert not third.stream.cursor["pending"]


def test_youtube_owner_scans_restore_only_top_level_comments_and_preserve_hidden_reply(feed, wire, monkeypatch):
    seed(feed, wire, hidden=True)
    raw = deepcopy(wire["replies_first"]["items"][0])
    raw["snippet"].pop("moderationStatus", None)
    land_posts(feed, [comment_post(
        raw, video_id="video-fresh", channel_id=feed.external_id, moderation_status="heldForReview",
    )],
               owner_id=feed.owner_id, historical=True)
    top = deepcopy(wire["threads_first"]["items"][0]["snippet"]["topLevelComment"])
    top["snippet"].pop("moderationStatus", None)
    calls = transport(monkeypatch, lambda *args: {"items": [top, raw]})
    cursor = ReobserveCursor(
        stage="comments", moderation_complete=True, moderation_floor=AT - timedelta(days=30),
    )
    drain(feed, source(feed, "reobserve", cursor.model_dump(mode="json")))
    restored = messages(feed).get(external_id="comment-a")
    held = messages(feed).get(external_id=raw["id"])
    assert not restored.is_trashed and "provider_hidden" not in restored.local_metadata
    assert held.is_trashed and held.local_metadata["provider_hidden"] == held.trashed_at.isoformat()
    assert len(calls) == 1


@pytest.mark.parametrize("status, payload", [(403, "html"), (403, []), (200, "html"), (503, {})])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_youtube_shared_http_classifier_preserves_definite_and_ambiguous_outcomes(
    feed, wire, replier, monkeypatch, status, payload, method,
):
    response = httpx2.Response(
        status, content=b"<html>refused</html>" if payload == "html" else json.dumps(payload).encode(),
        request=httpx2.Request(method, youtube.API_ROOT + "comments"), headers={"Retry-After": "123"},
    )
    transport(monkeypatch, lambda *args: response)
    expected = DeliveryUncertain if method == "POST" and status != 403 else IntegrationError
    with pytest.raises(expected) as caught:
        if method == "POST":
            feed.feed_backend.deliver(reply(feed, wire, actor=replier))
        else:
            step(feed, source(feed, "activity"))
    if expected is IntegrationError:
        assert caught.value.transient == (status != 403)
        assert caught.value.retry_after == (timedelta(seconds=123) if status != 403 else None)


def test_youtube_delivery_hook_opens_current_quota_period_across_midnight(feed, wire, replier, monkeypatch):
    adapter = feed.feed_backend
    original = adapter.consume_quota
    midnight = AT.astimezone(ZoneInfo(adapter.quota_reset_timezone)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    ) + timedelta(days=1)
    moments = [midnight - timedelta(seconds=1)]
    monkeypatch.setattr(timezone, "now", lambda: moments[0])
    message = reply(feed, wire, actor=replier)

    def debit_then_cross_reset(*args, **kwargs):
        period = original(*args, **kwargs)
        moments[0] = midnight + timedelta(seconds=1)
        return period

    monkeypatch.setattr(adapter, "consume_quota", debit_then_cross_reset)
    transport(monkeypatch, lambda *args: (403, wire["quota_exceeded"], {}))
    with pytest.raises(IntegrationError) as caught:
        adapter.deliver(message)
    periods = list(apps.get_model("posts", "Quota").objects.order_by("period_start"))
    assert len(periods) == 2 and [period.quota_used for period in periods] == [50, 0]
    assert caught.value.retry_after == periods[1].period_end - moments[0]


def test_youtube_first_reconciliation_waits_for_visibility_without_spending_lookup_quota(
    feed, wire, replier, monkeypatch,
):
    calls = transport(monkeypatch, lambda *args: httpx2.ReadTimeout("Synthetic lost acknowledgement"))
    message = claim_feed_reply(reply(feed, wire, actor=replier), actor=feed.owner)
    adapter = feed.feed_backend
    with pytest.raises(DeliveryUncertain) as uncertain:
        adapter.deliver(message)
    assert type(message).objects.record_delivery(
        message.pk, token=message.external_id, claim=message.delivery_claim, error=uncertain.value,
    ) == "uncertain"
    message.refresh_from_db()
    assert message.delivery_lease_until is None
    assert message.scheduled_at == AT + adapter.delivery_visibility_delay
    with pytest.raises(DeliveryUncertain, match="visibility margin"):
        adapter.deliver(message)
    period = adapter.current_quota_period()
    assert period.quota_used == 50 and [call[0] for call in calls] == ["POST"]
