"""YouTube contracts exercised through the real integrate stream engine."""

from __future__ import annotations

import json
import tomllib
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
import yaml
from django.apps import apps
from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
from rebac import system_context

from angee.integrate import streams as stream_engine
from angee.integrate.credentials import CredentialKind
from angee.integrate.discovery import ConnectionDiscovery
from angee.integrate.errors import IntegrationError
from angee.integrate.http import HttpClient
from angee.integrate.streams import advance_stream, open_stream, sync_bridge
from angee.integrate.testing.models import SyncDiscrepancy, SyncStream
from angee.messaging.backends import DeliveryOutcome
from angee.messaging.delivery import TransientDeliveryError
from angee.messaging.events import message_ingested
from angee.posts.ingest import land_posts
from angee.posts_integrate_youtube import backend as youtube
from angee.posts_integrate_youtube.backend import CALL_COSTS, PACIFIC, YouTubeFeedBackend
from angee.posts_integrate_youtube.cursors import (
    ActivityCursor,
    HistoryCursor,
    PagePosition,
    ReplyPosition,
    ThreadIdentity,
)
from angee.posts_integrate_youtube.oauth import GoogleYouTube
from angee.posts_integrate_youtube.parsing import comment_post, timestamp, video_post
from tests.conftest import make_integration

pytestmark = pytest.mark.django_db(transaction=True)
AT = datetime(2026, 10, 10, 12, tzinfo=UTC)
"""A fixed instant for synthetic publication, watermark and quota assertions."""
ADDON = Path(__file__).resolve().parents[1] / "addons/angee/posts_integrate_youtube"
"""The addon-owned manifest and adopted OAuth client resource."""


@pytest.fixture(autouse=True)
def context(monkeypatch):
    """Keep provider tests deterministic and authorized without external calls."""

    monkeypatch.setattr(timezone, "now", lambda: AT)
    with system_context(reason="test.youtube.contract"):
        yield


@pytest.fixture
def wire():
    """Invented provider identities and payloads, containing no credentials."""

    return json.loads((Path(__file__).parent / "fixtures/youtube/contract.json").read_text())


@pytest.fixture
def feed(composed_tables):
    """Compose the real Feed, quota ledger and credential owners."""

    del composed_tables
    row = make_integration(
        "youtube", model=apps.get_model("posts", "Feed"), kind=CredentialKind.OAUTH, backend_class="feed",
        feed_backend_class="youtube",
        material={"access_token": "synthetic-access", "refresh_token": "synthetic-refresh"},
        external_id="channel-demo", youtube_uploads_playlist_id="uploads-demo", cursor={},
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
        status, payload, headers = result if isinstance(result, tuple) else (200, result, {})
        return httpx2.Response(status, json=deepcopy(payload), headers=headers, request=httpx2.Request(method, url))

    monkeypatch.setattr(HttpClient, "request", request)
    return calls


def source(feed, key="history", cursor=None):
    """Open a genuine durable stream through its public declaration contract."""

    adapter = feed.backend
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

    adapter = feed.backend
    try:
        return advance_stream(stream, adapter, page_bound=bound, deadline=deadline)
    finally:
        adapter.close()


def drain(feed, stream, *, bound=10):
    """Resume the returned epoch, including the driver's repeated-page checks."""

    previous, resets = None, 0
    for _ in range(200):
        result = step(feed, stream, bound=bound)
        previous, resets = result.check_continuation(previous=previous, resets=resets)
        stream = result.stream
        if result.exhausted:
            return stream
    pytest.fail("The stream did not become exhausted.")


def messages(feed):
    """Read the records actually landed by the engine."""

    return apps.get_model("messaging", "Message").objects.filter(channel_id=feed.pk, platform="youtube")


def seed(feed, wire, *, count=2):
    """Prepare delivery context through posts' public landing owner."""

    raw = wire["threads_first"]["items"][0]
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
            if query.get("videoId") == "video-fresh" or query.get("id") == "thread-resource-a":
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
                return {"items": wire["threads_first"]["items"], "nextPageToken": "activity-old"}
            return {"items": wire["threads_last"]["items"], "nextPageToken": "activity-second"}
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


def test_youtube_preset_seed_and_read_only_binding_field(feed):
    manifest = tomllib.loads((ADDON / "addon.toml").read_text())
    resource = manifest["resources"]["install"][0]
    [client] = yaml.safe_load((ADDON / resource["path"]).read_text())
    assert resource["adopt"] == ["slug", "environment"]
    assert client["xref"] == "oauth_youtube" and client["slug"] == YouTubeFeedBackend.oauth_client
    assert client["provider_type"] == GoogleYouTube.key
    assert "client_id" not in client and "client_secret" not in client
    assert settings.ANGEE_OAUTH_PROVIDER_TYPE_CLASSES[GoogleYouTube.key].endswith(".GoogleYouTube")
    assert GoogleYouTube.defaults["supports_refresh"] and GoogleYouTube.defaults["supports_pkce"]
    assert "uploads_playlist_id" not in YouTubeFeedBackend.Config.model_fields
    assert not feed._meta.get_field("youtube_uploads_playlist_id").editable
    assert [(item.key, item.partition) for item in feed.backend.streams()] == [
        ("activity", "channel-demo"), ("history", "channel-demo"),
    ]


def test_youtube_discovery_separates_network_facts_from_database_apply(feed, monkeypatch, wire):
    calls = transport(monkeypatch, lambda *args: wire["channel"])
    before = (feed.external_id, feed.handle_id, feed.youtube_uploads_playlist_id, feed.credential.updated_at)
    discovery = feed.backend.discover_connection(feed.credential)
    feed.refresh_from_db()
    assert isinstance(discovery, ConnectionDiscovery) and discovery.credential is None
    assert set(discovery.data) == {"external_id", "display_name", "handle", "uploads_playlist_id"}
    assert before == (feed.external_id, feed.handle_id, feed.youtube_uploads_playlist_id, feed.credential.updated_at)
    assert discovery.data["handle"].external_id == discovery.data["external_id"] == "channel-demo"
    assert calls[0][2] == {"part": "id,snippet,contentDetails", "mine": "true"}
    feed.youtube_uploads_playlist_id = ""
    feed.save(update_fields=["youtube_uploads_playlist_id", "updated_at"])
    with transaction.atomic():
        feed.backend.apply_discovery(discovery)
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
        feed.backend.discover_connection(feed.credential)


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
    assert feed.cursor == {}
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
    assert row.cursor["newest_published_at"] == "2026-10-09T11:00:00Z"
    assert row.cursor["stage"] == "idle" and row.cursor["recheck_after"]
    assert not messages(feed).filter(external_id="comment-old").exists()
    assert not any(query.get("pageToken") == "activity-unneeded" for _, _, query, _ in calls)
    previous = len(calls)
    row = drain(feed, row)
    assert row.cursor["newest_published_at"] == "2026-10-09T11:00:00Z"
    assert len(calls) == previous + 1  # Existing watermark stops page one; recheck is not due.


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
    row = drain(feed, source(feed, "activity"))
    assert messages(feed).filter(external_id__in=["reply-a", "reply-b"]).count() == 2
    assert sum(bool(query.get("parentId")) for _, _, query, _ in calls) == 2
    assert any(
        query.get("id") == "thread-resource-a"
        for _, resource, query, _ in calls if resource == "commentThreads"
    )
    previous = len(calls)
    drain(feed, row)
    assert len(calls) == previous + 1


def test_youtube_first_activity_scan_includes_the_exact_live_since_boundary(feed, monkeypatch, wire):
    payload = deepcopy(wire["threads_last"])
    payload["items"][0]["snippet"]["topLevelComment"]["snippet"]["publishedAt"] = feed.live_since.isoformat()
    cursor = ActivityCursor(recheck_after=AT + timedelta(hours=1)).model_dump(mode="json")

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
    row = source(feed, "activity", ActivityCursor(stage="threads", floor=AT).model_dump(mode="json"))
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
        "thread_id": "thread-resource-b", "video_id": "video-fresh", "moderation": "published",
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
    feed.binding_retry_at = None
    feed.binding_completed_at = AT
    feed.save(update_fields=["config", "binding_retry_at", "binding_completed_at", "updated_at"])

    def respond(method, resource, query, kwargs):
        del method, resource, query, kwargs
        clock[0] = 20.0
        return wire["threads_last"]

    calls = transport(monkeypatch, respond)
    assert sync_bridge(feed) == 0
    row = SyncStream.objects.current_for_bridge(feed, "activity").get()
    assert row.cursor["pending"][0]["thread_id"] == "thread-resource-b" and len(calls) == 1


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
    feed.binding_retry_at = None
    feed.binding_completed_at = AT
    feed.save(update_fields=["config", "binding_retry_at", "binding_completed_at", "updated_at"])
    source(feed, "activity", ActivityCursor(stage="threads", floor=AT).model_dump(mode="json"))
    calls = transport(monkeypatch, lambda *args: pytest.fail("There is no provider work on this page."))
    assert sync_bridge(feed) == 0 and calls == []
    assert extracts[0] == 2
    assert SyncStream.objects.current_for_bridge(feed, "activity").get().cursor["stage"] == "comments"


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
    assert SyncDiscrepancy.objects.filter(code="youtube_page_token_loop").exists()


@pytest.mark.parametrize("key", ["activity", "history"])
def test_youtube_deleted_reply_parent_cannot_wedge_the_stream(feed, monkeypatch, key):
    model = ActivityCursor if key == "activity" else HistoryCursor
    cursor = model(replies=[ReplyPosition(parent_id="comment-gone", video_id="video-fresh")]).model_dump(mode="json")
    refusal = {"error": {"errors": [{"reason": "parentCommentNotFound"}]}}
    calls = transport(monkeypatch, lambda *args: (404, refusal, {}))
    row = step(feed, source(feed, key, cursor)).stream
    assert row.cursor["replies"] == [] and len(calls) == 1
    assert SyncDiscrepancy.objects.get(code="youtube_replies_unavailable").details == {"external_id": "comment-gone"}


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
    assert SyncDiscrepancy.objects.get(code="youtube_thread_unavailable").details == {
        "external_id": "thread-unavailable",
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
    assert SyncDiscrepancy.objects.filter(code="youtube_video_comments_unavailable").exists()


def test_youtube_refused_recheck_advances_beyond_the_thread(feed, monkeypatch, wire):
    seed(feed, wire)
    cursor = ActivityCursor(stage="threads", floor=AT).model_dump(mode="json")
    refusal = {"error": {"errors": [{"reason": "commentNotFound"}]}}
    calls = transport(monkeypatch, lambda *args: (404, refusal, {}))
    row = step(feed, source(feed, "activity", cursor)).stream
    assert row.cursor["active_after"] > 0
    result = step(feed, row)
    assert result.stream.cursor["stage"] == "comments" and len(calls) == 1
    assert SyncDiscrepancy.objects.filter(code="youtube_thread_unavailable").exists()


def test_youtube_malformed_reply_is_recorded_and_does_not_hold_the_parent_cursor(feed, monkeypatch):
    cursor = HistoryCursor(replies=[ReplyPosition(
        parent_id="comment-a", video_id="video-fresh",
    )]).model_dump(mode="json")
    transport(monkeypatch, lambda *args: {"items": [{"id": "reply-malformed", "snippet": None}]})
    result = step(feed, source(feed, cursor=cursor))
    assert result.count == 0 and result.stream.cursor["replies"] == []
    assert SyncDiscrepancy.objects.filter(code="youtube_malformed_reply").exists()


@pytest.mark.parametrize("key", ["activity", "history"])
def test_youtube_malformed_thread_is_skipped_and_progress_is_committed(feed, monkeypatch, key):
    cursor = None if key == "activity" else HistoryCursor(
        stage="threads", videos=["video-fresh"],
    ).model_dump(mode="json")
    transport(monkeypatch, lambda *args: {"items": [{"id": "thread-malformed"}, None]})
    result = step(feed, source(feed, key, cursor))
    assert result.count == 0 and result.stream.cursor != (cursor or {})
    assert SyncDiscrepancy.objects.filter(code="youtube_malformed_thread").count() == 2


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
        drain(feed, source(feed, "activity"))
    finally:
        message_ingested.disconnect(ingested)
    assert messages(feed).get(external_id="comment-held").is_trashed
    assert "comment-held" not in seen
    assert any(query.get("moderationStatus") == moderation for _, _, query, _ in calls)


@pytest.mark.parametrize("moderation", ["heldForReview", "likelySpam", "rejected"])
def test_youtube_owner_identity_reobservation_trashes_a_previously_landed_comment(
    feed, monkeypatch, wire, moderation,
):
    seed(feed, wire)
    raw = deepcopy(wire["threads_first"]["items"][0]["snippet"]["topLevelComment"])
    raw["snippet"]["moderationStatus"] = moderation
    transport(monkeypatch, lambda *args: {"items": [raw]})
    cursor = ActivityCursor(stage="comments", floor=AT).model_dump(mode="json")
    drain(feed, source(feed, "activity", cursor))
    assert messages(feed).get(external_id="comment-a").is_trashed


def test_youtube_missing_reobserved_comment_records_absence_without_claiming_deletion(feed, monkeypatch, wire):
    seed(feed, wire)
    transport(monkeypatch, lambda *args: {"items": []})
    cursor = ActivityCursor(stage="comments", floor=AT).model_dump(mode="json")
    drain(feed, source(feed, "activity", cursor))
    assert not messages(feed).get(external_id="comment-a").is_trashed
    assert SyncDiscrepancy.objects.get(code="youtube_comment_unavailable").details == {"external_id": "comment-a"}


def test_youtube_polling_limit_protects_reply_units_on_the_real_ledger(feed, replier, monkeypatch, wire):
    message = reply(feed, wire, actor=replier)
    feed.config = {"quota_limit": 100, "reply_reserve": 50}
    feed.save(update_fields=["config", "updated_at"])
    quota = apps.get_model("posts", "Quota").objects
    now = AT.astimezone(PACIFIC)
    period = quota.open_period(integration=feed, limit=100, now=now)
    quota.filter(pk=period.pk).update(quota_used=50)
    calls = transport(monkeypatch, lambda *args: wire["inserted_reply"])
    with pytest.raises(IntegrationError) as caught:
        step(feed, source(feed, "activity"))
    assert caught.value.transient and caught.value.retry_after == period.period_end - now and calls == []
    assert feed.backend.deliver(message) == DeliveryOutcome(True, "reply-published")
    period.refresh_from_db()
    assert period.quota_used == 100 and period.period_start.astimezone(PACIFIC).hour == 0


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
    assert feed.backend.deliver(message) == DeliveryOutcome(True, "reply-published")
    assert len(calls) == 1
    assert apps.get_model("posts", "Quota").objects.get(integration=feed).quota_used == CALL_COSTS["comments.insert"]


def test_youtube_ambiguous_send_reconciles_existing_channel_reply(feed, replier, monkeypatch, wire):
    message = reply(feed, wire, actor=replier)

    def respond(method, resource, query, kwargs):
        del resource, query, kwargs
        return httpx2.ReadTimeout("Synthetic acknowledgement loss") if method == "POST" else wire["replies_last"]

    calls = transport(monkeypatch, respond)
    assert feed.backend.deliver(message) == DeliveryOutcome(True, "reply-b")
    assert [method for method, _, _, _ in calls] == ["POST", "GET"]


@pytest.mark.parametrize("lookup", ["throttle", "expired", "loop", "not_indexed"])
def test_youtube_ambiguous_lookup_failure_never_authorizes_resend(feed, replier, monkeypatch, wire, lookup):
    message = reply(feed, wire, actor=replier)

    def respond(method, resource, query, kwargs):
        del resource, kwargs
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
    with pytest.raises(IntegrationError) as caught:
        feed.backend.deliver(message)
    assert not caught.value.transient and not isinstance(caught.value, TransientDeliveryError)
    assert sum(method == "POST" for method, _, _, _ in calls) == 1


@pytest.mark.parametrize("reason, phrase", [
    ("commentTextTooLong", "length"), ("parentCommentNotFound", "parent"), ("commentsDisabled", "disabled"),
])
def test_youtube_delivery_reports_specific_permanent_refusals(feed, replier, monkeypatch, wire, reason, phrase):
    message = reply(feed, wire, actor=replier)
    transport(monkeypatch, lambda *args: (400, {"error": {"errors": [{"reason": reason}]}}, {}))
    with pytest.raises(IntegrationError) as caught:
        feed.backend.deliver(message)
    assert not caught.value.transient and phrase in caught.value.public_message


def test_youtube_certain_connect_failure_is_retryable(feed, replier, monkeypatch, wire):
    message = reply(feed, wire, actor=replier)
    transport(monkeypatch, lambda *args: httpx2.ConnectTimeout("Synthetic connect failure"))
    with pytest.raises(TransientDeliveryError):
        feed.backend.deliver(message)


def test_youtube_delivery_and_polling_throttle_keep_retry_hints(feed, replier, monkeypatch, wire):
    message = reply(feed, wire, actor=replier)
    transport(monkeypatch, lambda *args: (429, wire["rate_limited"], {"Retry-After": "120"}))
    with pytest.raises(TransientDeliveryError) as delivery:
        feed.backend.deliver(message)
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
    assert top.message.external_id == "comment-a" and top.message.external_id != raw["id"]
    assert top.message.in_reply_to == "video-fresh" and child.message.in_reply_to == "comment-a"
    assert child.message.direction == "outbound" and top.message.direction == "inbound"
    assert "author_display_name" not in top.message.metadata["youtube"]


@pytest.mark.parametrize("value", [None, "invalid", "2026-19-01T09:00:00Z", "2026-10-01T09:00:00"])
def test_youtube_unusable_publication_time_stays_unknown(value):
    assert timestamp(value) is None
