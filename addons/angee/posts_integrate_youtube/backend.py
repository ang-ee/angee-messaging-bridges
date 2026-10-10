"""YouTube channel comments through the integrate stream and quota owners."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from email.utils import parsedate_to_datetime
from math import isfinite
from time import monotonic
from typing import Any, Self, cast
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx2
from django.apps import apps
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic import ValidationError as CursorValidationError

from angee.integrate.credentials import CredentialKind
from angee.integrate.discovery import ConnectionDiscovery
from angee.integrate.errors import IntegrationError
from angee.integrate.http import ResponseTooLargeError
from angee.integrate.states import DiscrepancyKind
from angee.integrate.streams import CursorInvalid, StreamDefinition, StreamPage
from angee.messaging.backends import DeliveryOutcome, ParsedHandle
from angee.messaging.delivery import TransientDeliveryError
from angee.posts.backends import FeedBackend, ParsedPost
from angee.posts.ingest import PROVIDER_TRASH_REASON

from .cursors import ActivityCursor, HistoryCursor, PagePosition, ReplyPosition, StreamCursor, ThreadIdentity
from .parsing import comment_post, timestamp, video_post

API_ROOT = "https://www.googleapis.com/youtube/v3/"
"""YouTube Data API origin; Google OAuth endpoints belong solely to the preset."""
PACIFIC = ZoneInfo("America/Los_Angeles")
"""Google's daily YouTube quota resets at Pacific midnight."""
CALL_COSTS = {
    "channels.list": 1, "playlistItems.list": 1, "videos.list": 1,
    "commentThreads.list": 1, "comments.list": 1, "comments.insert": 50,
}
"""Data API unit charges, including the 50-unit public reply insertion."""
_COMMENT_LIMIT = 100
_ID_LIMIT = 50  # Conservative id-filter cap, also the documented videos.list cap.
_MODERATION = ("published", "heldForReview", "likelySpam")
_QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "dailyLimitExceededUnreg"}
_RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}
_ITEM_ERRORS = (KeyError, TypeError, ValueError, AttributeError, OverflowError)
_REFUSALS = {
    "commentTextTooLong": "The YouTube reply exceeds the permitted comment length.",
    "commentTextRequired": "The YouTube reply needs text.",
    "parentCommentNotFound": "The parent YouTube comment is no longer available.",
    "commentNotFound": "The YouTube comment is no longer available.",
    "videoNotFound": "The YouTube video is no longer available.",
    "commentsDisabled": "Comments are disabled for this YouTube video.",
    "forbidden": "YouTube refused access to this comment; check the channel grant.",
    "insufficientPermissions": "The Google grant lacks the required YouTube permissions.",
}


class _PublishUncertain(IntegrationError):
    """A reply might have been accepted; only observation may settle it."""


class _ItemUnavailable(IntegrationError):
    """A permanent resource read refusal that must not wedge a work queue."""


class YouTubeFeedBackend(FeedBackend):
    """Poll one channel and publish replies using YouTube Data API v3.

    A video id identifies both the original post and its public thread. Comments
    and replies use comment resource ids; thread resource ids are lookup metadata.

    Activity retains a newest-seen publication watermark and bounds its first
    published scan by max(Feed.live_since, now - active_thread_days). Hourly owner
    moderation scans within active_thread_days and identity rechecks observe late
    replies and held comments. History independently walks uploads and per-video
    comment pages.

    Cursors contain only identities, paging positions and watermarks. Discovery
    stores uploads in the read-only Feed.youtube_uploads_playlist_id extension.
    Explicit owner reads can expose held/spam and sometimes rejected comments;
    an absent resource cannot prove deletion, so absence is recorded for review
    rather than automatically trashing a previously landed comment.
    """

    key = "youtube"
    label = "YouTube"
    icon = "youtube"
    oauth_client = "youtube"
    defaults = {"vendor": "google", "poll_interval": 900}
    # The driver sorts activity ahead of history. Serial drains give the live
    # tail first use of the shared quota and serialize root/comment projection.
    sync_parallelism = 1

    class Config(BaseModel):
        """Daily units, protected reply units, and the recent-thread recheck policy."""

        model_config = ConfigDict(extra="allow")
        quota_limit: int = Field(default=10000, ge=1)
        reply_reserve: int = Field(default=1000, ge=0)
        active_thread_days: int = Field(default=30, ge=1)
        recheck_interval: int = Field(default=3600, ge=900)

        @model_validator(mode="after")
        def check_reserve(self) -> Self:
            """Leave a positive polling allowance within the total daily ceiling."""

            if self.reply_reserve >= self.quota_limit:
                raise ValueError("reply_reserve must be smaller than quota_limit")
            return self

    config_model = Config

    @property
    def policy(self) -> YouTubeFeedBackend.Config:
        """Resolve policy through the existing implementation descriptor."""

        return cast(self.Config, self.parse_config(self.bridge.config))

    def discover_connection(self, credential: Any) -> ConnectionDiscovery:
        """Return network facts; integrate applies them under its generation fence."""

        if credential.kind != CredentialKind.OAUTH or credential.oauth_client is None:
            raise IntegrationError("Connect this YouTube channel through Google OAuth.", transient=False)
        data = self._request(
            "channels.list", credential=credential, part="id,snippet,contentDetails", mine="true",
        )
        channels = _items(data)
        if len(channels) != 1:
            raise IntegrationError("Connect a Google account with exactly one YouTube channel.")
        try:
            channel = channels[0]
            identity = channel["id"]
            uploads = channel["contentDetails"]["relatedPlaylists"]["uploads"]
            title = channel["snippet"].get("title") or identity
            if not all(isinstance(value, str) and value for value in (identity, uploads, title)):
                raise ValueError("incomplete_binding")
        except _ITEM_ERRORS as error:
            raise IntegrationError("YouTube returned an incomplete channel binding.") from error
        if self.bridge.external_id and identity != self.bridge.external_id:
            raise IntegrationError("This feed belongs to another YouTube channel; create a separate feed.")
        return ConnectionDiscovery(data={
            "external_id": identity, "display_name": title,
            "handle": ParsedHandle("youtube", identity, title, identity), "uploads_playlist_id": uploads,
        })

    def apply_discovery(self, discovery: ConnectionDiscovery) -> None:
        """Apply posts identity first, then the addon-owned non-editable binding field."""

        uploads = discovery.data["uploads_playlist_id"]
        if not isinstance(uploads, str) or not uploads:
            raise IntegrationError("YouTube discovery did not include an uploads playlist.")
        super().apply_discovery(discovery)
        self.bridge.youtube_uploads_playlist_id = uploads
        self.bridge.save(update_fields=["youtube_uploads_playlist_id", "updated_at"])

    def streams(self, *, deadline: float | None = None) -> tuple[StreamDefinition, ...]:
        """Declare the two channel partitions; discovery has already supplied identity."""

        if not self.bridge.external_id or not self.bridge.youtube_uploads_playlist_id:
            raise IntegrationError("Connect a YouTube channel before polling comments.")
        return tuple(StreamDefinition(key=key, partition=self.bridge.external_id) for key in ("activity", "history"))

    def extract(self, stream: Any, page_bound: int, *, deadline: float | None = None) -> StreamPage:
        """Return a bounded page; the driver alone advances or resets its epoch."""

        cursor_type = ActivityCursor if stream.key == "activity" else HistoryCursor
        try:
            state = cursor_type.model_validate(stream.cursor)
        except CursorValidationError as error:
            raise CursorInvalid() from error
        bound = min(page_bound, _COMMENT_LIMIT)
        if isinstance(state, ActivityCursor) and (state.floor is None or state.stage == "idle"):
            floor = timezone.now() - timedelta(days=self.policy.active_thread_days)
            state.begin(floor=max(floor, self.bridge.live_since or floor))
        if _out_of_time(deadline):
            return StreamPage((), state.model_dump(mode="json"), exhausted=False)
        if state.pending:
            records = self._pending_threads(stream, state, bound, deadline=deadline)
        elif state.replies:
            records = self._replies(stream, state, bound)
        elif isinstance(state, HistoryCursor):
            records = self._history(stream, state, bound, deadline=deadline)
        else:
            records = self._activity(stream, state, bound, deadline=deadline)
        exhausted = state.stage == ("complete" if isinstance(state, HistoryCursor) else "idle")
        return StreamPage(records, state.model_dump(mode="json"), exhausted=exhausted and not _out_of_time(deadline))

    def _paged(
        self, stream: Any, state: StreamCursor, position: PagePosition, operation: str,
        *, identity: str, **params: Any,
    ) -> dict[str, Any]:
        """Reset only a failed walk; quarantine a second failure to avoid a token loop."""

        try:
            data = self._request(operation, pageToken=position.token or None, **params)
            token = data.get("nextPageToken")
            if token is not None and (
                not isinstance(token, str) or not token or token == position.token or token in position.seen
            ):
                raise CursorInvalid()
        except CursorInvalid as error:
            if position.resets:
                self._discrepancy(stream, "page_token_loop", identity)
                position.token = None
                return {"items": []}
            position.token, position.seen, position.resets = "", [], 1
            state.force = True
            raise CursorInvalid(cursor=state.model_dump(mode="json")) from error
        position.token = token
        position.seen = [*position.seen, token][-16:] if token else []
        return data

    def _history(
        self, stream: Any, state: HistoryCursor, bound: int, *, deadline: float | None,
    ) -> tuple[ParsedPost, ...]:
        """Batch video metadata and walk only videos that may have comments."""

        if state.stage == "complete":
            return ()
        if state.stage == "uploads":
            if state.uploads.token is None:
                state.stage = "complete"
                return ()
            data = self._paged(
                stream, state, state.uploads, "playlistItems.list", identity=self.bridge.youtube_uploads_playlist_id,
                playlistId=self.bridge.youtube_uploads_playlist_id, part="contentDetails,status",
                maxResults=min(bound, _ID_LIMIT),
            )
            for raw in _items(data, min(bound, _ID_LIMIT)):
                try:
                    if raw.get("status", {}).get("privacyStatus") != "private":
                        identity = raw["contentDetails"]["videoId"]
                        if not isinstance(identity, str) or not identity or len(identity) > 512:
                            raise ValueError("missing_video")
                        if identity not in state.root_ids:
                            state.root_ids.append(identity)
                except _ITEM_ERRORS:
                    self._discrepancy(stream, "malformed_upload", _identity(raw))
            state.stage = "videos" if state.root_ids else "uploads"
            return ()
        if state.stage == "videos":
            chosen = state.root_ids[:min(bound, _ID_LIMIT)]
            records = self._videos(stream, chosen)
            state.root_ids = state.root_ids[len(chosen):]
            state.videos.extend(
                record.message.external_id for record in records
                if record.metrics is not None and (
                    record.metrics.reply_count != 0 or not record.metrics.metadata["comment_count_known"]
                )
            )
            if not state.root_ids:
                state.stage, state.moderation, state.threads = "threads", "published", PagePosition()
            return records
        if not state.videos:
            state.stage = "uploads"
            return ()
        if state.threads.token is None:
            index = _MODERATION.index(state.moderation)
            if index == len(_MODERATION) - 1:
                state.videos.pop(0)
                state.moderation = "published"
            else:
                state.moderation = _MODERATION[index + 1]
            state.threads = PagePosition()
            state.force = False
            return ()
        try:
            data = self._paged(
                stream, state, state.threads, "commentThreads.list", identity=state.videos[0],
                videoId=state.videos[0], part="snippet", order="time", textFormat="plainText",
                moderationStatus=state.moderation, maxResults=max(1, min(_ID_LIMIT, bound // 2)),
            )
        except _ItemUnavailable:
            self._discrepancy(stream, "video_comments_unavailable", state.videos.pop(0))
            state.threads, state.moderation = PagePosition(), "published"
            return ()
        return self._thread_records(
            stream, state, _items(data, max(1, min(_ID_LIMIT, bound // 2))), bound,
            state.moderation, deadline=deadline,
        )

    def _activity(
        self, stream: Any, state: ActivityCursor, bound: int, *, deadline: float | None,
    ) -> tuple[ParsedPost, ...]:
        """Bound the live scan, then perform independently paced owner re-observation."""

        if state.stage in _MODERATION:
            if state.page.token is None:
                if state.stage == "published":
                    state.newest_published_at = state.cycle_newest
                    state.stage = "heldForReview" if self._recheck_due(state) else "idle"
                    if state.stage == "idle":
                        state.force = False
                else:
                    state.stage = "likelySpam" if state.stage == "heldForReview" else "threads"
                state.page, state.active_after = PagePosition(), 0
                return ()
            data = self._paged(
                stream, state, state.page, "commentThreads.list", identity=self.bridge.external_id,
                allThreadsRelatedToChannelId=self.bridge.external_id, part="snippet", order="time",
                textFormat="plainText", moderationStatus=state.stage,
                maxResults=max(1, min(_ID_LIMIT, bound // 2)),
            )
            rows = []
            for raw in _items(data, max(1, min(_ID_LIMIT, bound // 2))):
                try:
                    published = timestamp(raw["snippet"]["topLevelComment"]["snippet"]["publishedAt"])
                    if published is None:
                        raise ValueError("missing_publication")
                except _ITEM_ERRORS:
                    self._discrepancy(stream, "malformed_thread", _identity(raw))
                    continue
                floor = state.floor if state.stage == "published" else (
                    timezone.now() - timedelta(days=self.policy.active_thread_days)
                )
                seen = state.stage == "published" and state.newest_published_at is not None and (
                    published <= state.newest_published_at
                )
                if seen or (floor is not None and published < floor):
                    state.page.token = None
                    break
                if state.stage == "published":
                    state.cycle_newest = max(state.cycle_newest or published, published)
                rows.append(raw)
            return self._thread_records(stream, state, rows, bound, state.stage, deadline=deadline)
        if state.stage == "threads":
            return self._recheck_threads(stream, state, bound, deadline=deadline)
        if state.stage == "comments":
            return self._reobserve_comments(stream, state, bound)
        return ()

    def _recheck_due(self, state: ActivityCursor) -> bool:
        """Limit moderation and recent-thread scans to the configured cadence."""

        return state.recheck_after is None or state.recheck_after <= timezone.now()

    def _recent_messages(self, after: int, *, parents_only: bool = False) -> Any:
        """Use landed identities only for reconciliation, never as stream position."""

        floor = timezone.now() - timedelta(days=self.policy.active_thread_days)
        rows = apps.get_model("messaging", "Message").objects.filter(
            channel_id=self.bridge.pk, platform="youtube", is_original_post=False, pk__gt=after,
        ).filter(Q(sent_at__gte=floor) | Q(replies__sent_at__gte=floor))
        if parents_only:
            rows = rows.filter(parent__is_original_post=True).exclude(metadata__youtube__thread_id="")
        return rows.order_by("pk").distinct()

    def _recheck_threads(
        self, stream: Any, state: ActivityCursor, bound: int, *, deadline: float | None,
    ) -> tuple[ParsedPost, ...]:
        """Re-read at most 50 thread ids and fetch replies only for changed counts."""

        batch_size = min(_ID_LIMIT, max(1, bound // 2))
        candidates = list(self._recent_messages(state.active_after, parents_only=True)[:batch_size])
        if not candidates:
            state.stage, state.active_after = "comments", 0
            return ()
        state.active_after = candidates[-1].pk
        identities = [row.metadata.get("youtube", {}).get("thread_id") for row in candidates]
        identities = [identity for identity in identities if identity]
        if not identities:
            return ()
        try:
            data = self._request("commentThreads.list", id=",".join(identities), part="snippet", textFormat="plainText")
        except _ItemUnavailable:
            for identity in identities:
                self._discrepancy(stream, "thread_unavailable", identity)
            return ()
        return self._thread_records(
            stream, state, _items(data, len(identities)), bound, "published", deadline=deadline, changed_only=True,
        )

    def _reobserve_comments(self, stream: Any, state: ActivityCursor, bound: int) -> tuple[ParsedPost, ...]:
        """Owner id reads refresh available moderation without inferring deletion from absence."""

        candidates = list(self._recent_messages(state.active_after)[:min(_ID_LIMIT, bound)])
        if not candidates:
            state.stage, state.force = "idle", False
            state.recheck_after = timezone.now() + timedelta(seconds=self.policy.recheck_interval)
            return ()
        state.active_after = candidates[-1].pk
        by_id = {row.external_id: row for row in candidates}
        try:
            data = self._request("comments.list", id=",".join(by_id), part="snippet", textFormat="plainText")
        except _ItemUnavailable:
            data = {"items": []}
        records = []
        seen = set()
        for raw in _items(data, len(by_id)):
            try:
                row = by_id[raw["id"]]
                metadata = row.metadata["youtube"]
                prior_hidden = row.is_trashed and row.trash_reason == PROVIDER_TRASH_REASON
                records.append(comment_post(
                    raw, video_id=metadata["video_id"], channel_id=self.bridge.external_id,
                    thread_id=metadata.get("thread_id", ""),
                    reply_count=getattr(getattr(row, "post_metrics", None), "reply_count", 0),
                    moderation_status="heldForReview" if prior_hidden else "published",
                ))
                seen.add(row.external_id)
            except _ITEM_ERRORS:
                self._discrepancy(stream, "malformed_comment", _identity(raw))
        for identity in by_id.keys() - seen:
            self._discrepancy(stream, "comment_unavailable", identity)
        return tuple(records)

    def _pending_threads(
        self, stream: Any, state: StreamCursor, bound: int, *, deadline: float | None,
    ) -> tuple[ParsedPost, ...]:
        """Rehydrate deferred identities without ever storing their provider payloads."""

        chosen = state.pending[:min(_ID_LIMIT, max(1, bound // 2))]
        try:
            data = self._request(
                "commentThreads.list", id=",".join(work.thread_id for work in chosen),
                part="snippet", textFormat="plainText",
            )
        except _ItemUnavailable:
            for work in chosen:
                self._discrepancy(stream, "thread_unavailable", work.thread_id)
            state.pending = state.pending[len(chosen):]
            return ()
        state.pending = state.pending[len(chosen):]
        by_id = {work.thread_id: work for work in chosen}
        rows = []
        for raw in _items(data, len(chosen)):
            work = by_id.pop(_identity(raw), None)
            if work is None:
                self._discrepancy(stream, "unexpected_thread", _identity(raw))
                continue
            rows.append(raw)
        for identity in by_id:
            self._discrepancy(stream, "thread_unavailable", identity)
        return self._thread_records(
            stream, state, rows, bound, chosen[0].moderation, deadline=deadline,
        )

    def _thread_records(
        self, stream: Any, state: StreamCursor, rows: list[Any], bound: int, moderation: str,
        *, deadline: float | None, changed_only: bool = False,
    ) -> tuple[ParsedPost, ...]:
        """Return roots before comments; retain only ids if time or page size defers landing."""

        usable = []
        for raw in rows:
            try:
                snippet = raw["snippet"]
                top = snippet["topLevelComment"]
                if (not isinstance(top["snippet"], dict) or not isinstance(top["id"], str)
                        or not top["id"] or len(top["id"]) > 512):
                    raise ValueError("missing_comment")
                work = ThreadIdentity(
                    thread_id=raw["id"], video_id=snippet["videoId"], moderation=moderation,
                )
                count = int(snippet.get("totalReplyCount", 0))
                if count < 0:
                    raise ValueError("negative_reply_count")
                usable.append((work, top, count))
            except _ITEM_ERRORS:
                self._discrepancy(stream, "malformed_thread", _identity(raw))
        comments = [top["id"] for _, top, _ in usable]
        counts = dict(apps.get_model("posts", "PostMetrics").objects.filter(
            message__channel_id=self.bridge.pk, message__platform="youtube", message__external_id__in=comments,
        ).values_list("message__external_id", "reply_count"))
        if changed_only:
            usable = [entry for entry in usable if state.force or entry[2] != counts.get(entry[1]["id"])]
        prior_hidden = set(apps.get_model("messaging", "Message").objects.filter(
            channel_id=self.bridge.pk, platform="youtube", external_id__in=comments,
            trash_reason=PROVIDER_TRASH_REASON,
        ).values_list("external_id", flat=True)) if changed_only else set()
        video_ids = list(dict.fromkeys(work.video_id for work, _, _ in usable))
        existing = set(apps.get_model("messaging", "Message").objects.filter(
            channel_id=self.bridge.pk, platform="youtube", is_original_post=True, external_id__in=video_ids,
        ).values_list("external_id", flat=True))
        missing = [identity for identity in video_ids if identity not in existing]
        if missing and _out_of_time(deadline):
            state.pending.extend(work for work, _, _ in usable)
            return ()
        roots = self._videos(stream, missing[:min(_ID_LIMIT, bound)]) if missing else ()
        available = existing | {record.message.external_id for record in roots}
        records = list(roots)
        for work, top, count in usable:
            if work.video_id not in available:
                self._discrepancy(stream, "video_unavailable", work.video_id)
                continue
            if len(records) == bound:
                state.pending.append(work)
                continue
            try:
                record = comment_post(
                    top, video_id=work.video_id, channel_id=self.bridge.external_id, thread_id=work.thread_id,
                    reply_count=count, moderation_status=(
                        "heldForReview" if top["id"] in prior_hidden else work.moderation
                    ),
                )
            except _ITEM_ERRORS:
                self._discrepancy(stream, "malformed_comment", _identity(top))
                continue
            records.append(record)
            if count and (
                isinstance(state, HistoryCursor) or state.force or count != counts.get(top["id"])
            ) and not any(reply.parent_id == top["id"] for reply in state.replies):
                state.replies.append(ReplyPosition(parent_id=top["id"], video_id=work.video_id, hidden=record.hidden))
        return tuple(records)

    def _replies(self, stream: Any, state: StreamCursor, bound: int) -> tuple[ParsedPost, ...]:
        """A permanent parent refusal advances the queue instead of stranding its head."""

        work = state.replies[0]
        try:
            data = self._paged(
                stream, state, work.page, "comments.list", identity=work.parent_id,
                parentId=work.parent_id, part="snippet", textFormat="plainText", maxResults=bound,
            )
        except _ItemUnavailable:
            self._discrepancy(stream, "replies_unavailable", work.parent_id)
            state.replies.pop(0)
            return ()
        records = []
        for raw in _items(data, bound):
            try:
                if raw["snippet"].get("parentId") != work.parent_id:
                    raise ValueError("unexpected_parent")
                records.append(comment_post(
                    raw, video_id=work.video_id, channel_id=self.bridge.external_id, hidden_parent=work.hidden,
                ))
            except _ITEM_ERRORS:
                self._discrepancy(stream, "malformed_reply", _identity(raw))
        if work.page.token is None:
            state.replies.pop(0)
        return tuple(records)

    def _videos(self, stream: Any, identities: list[str]) -> tuple[ParsedPost, ...]:
        """One videos.list call handles up to 50 root identities."""

        if not identities:
            return ()
        try:
            data = self._request("videos.list", id=",".join(identities), part="snippet,statistics,status")
        except _ItemUnavailable:
            for identity in identities:
                self._discrepancy(stream, "video_unavailable", identity)
            return ()
        records = []
        for raw in _items(data, len(identities)):
            try:
                if (raw["id"] not in identities or raw["snippet"]["channelId"] != self.bridge.external_id
                        or raw.get("status", {}).get("privacyStatus") == "private"):
                    continue
                records.append(video_post(raw, channel_id=self.bridge.external_id))
            except _ITEM_ERRORS:
                self._discrepancy(stream, "malformed_video", _identity(raw))
        return tuple(records)

    @staticmethod
    def _discrepancy(stream: Any, code: str, identity: str) -> None:
        """Retain skipped resource evidence through integrate, containing no provider text."""

        apps.get_model("integrate", "SyncDiscrepancy").objects.record(
            stream, kind=DiscrepancyKind.SEMANTIC, code=f"youtube_{code}",
            source_hash=hashlib.sha256(identity.encode()).hexdigest(), details={"external_id": identity},
        )

    def deliver(self, message: Any) -> DeliveryOutcome:
        """Send each distinct reply once; text matching is reserved for ambiguous acknowledgements."""

        if message.channel_id != self.bridge.pk or message.platform != "youtube" or message.direction != "outbound":
            return DeliveryOutcome(accepted=False)
        try:
            parent = message.top_level_comment()
        except ValidationError:
            return DeliveryOutcome(accepted=False)
        text = message.body_text().strip()
        if not text or parent.is_original_post:
            return DeliveryOutcome(accepted=False)
        try:
            try:
                data = self._request("comments.insert", delivery=True, part="snippet", body={
                    "snippet": {"parentId": parent.external_id, "textOriginal": text},
                })
                identity = data.get("id")
                if not isinstance(identity, str) or not identity:
                    raise _PublishUncertain("YouTube did not acknowledge the reply identity.")
                return DeliveryOutcome(accepted=True, provider_id=identity)
            except _PublishUncertain:
                try:
                    existing = self._existing_reply(parent.external_id, text)
                except IntegrationError:
                    existing = None
                if existing:
                    return DeliveryOutcome(accepted=True, provider_id=existing)
                raise IntegrationError(
                    "The YouTube reply may have been sent or is not yet indexed; "
                    "inspect the thread before sending again.",
                    transient=False,
                ) from None
        except IntegrationError as error:
            if error.transient:
                raise TransientDeliveryError(error.public_message, retry_after=error.retry_after) from error
            raise

    def _existing_reply(self, parent_id: str, text: str) -> str | None:
        """Reconcile only after send ambiguity, with the full reserved delivery allowance."""

        token, seen = "", set()
        while True:
            try:
                data = self._request(
                    "comments.list", delivery=True, parentId=parent_id, part="snippet", textFormat="plainText",
                    maxResults=_COMMENT_LIMIT, pageToken=token or None,
                )
            except CursorInvalid as error:
                raise IntegrationError("YouTube refused the reply lookup cursor.") from error
            for raw in _items(data):
                try:
                    snippet = raw["snippet"]
                    if (snippet.get("authorChannelId", {}).get("value") == self.bridge.external_id
                            and str(snippet.get("textOriginal", snippet.get("textDisplay", ""))).strip() == text
                            and raw.get("id")):
                        return str(raw["id"])
                except _ITEM_ERRORS:
                    continue
            following = data.get("nextPageToken")
            if following is None:
                return None
            if not isinstance(following, str) or not following or following == token or following in seen:
                raise IntegrationError("YouTube repeated the reply lookup cursor.")
            token = following
            seen.add(token)

    def _request(
        self, operation: str, *, credential: Any = None, body: dict[str, Any] | None = None,
        delivery: bool = False, **params: Any,
    ) -> dict[str, Any]:
        """Use fixed HTTP timeouts, native credential refresh and the shared atomic unit ledger."""

        credential = credential or self.bridge.credential
        if credential is None:
            raise IntegrationError("Connect a YouTube channel before making requests.", transient=False)
        credential.ensure_fresh()
        headers = credential.auth_headers()
        now = timezone.now().astimezone(PACIFIC)
        quota = apps.get_model("posts", "Quota").objects
        # Open with the full ceiling so the ledger does not acquire the smaller
        # polling limit as its permanent daily ceiling.
        period = quota.open_period(integration=self.bridge, limit=self.policy.quota_limit, now=now)
        limit = self.policy.quota_limit if delivery else self.policy.quota_limit - self.policy.reply_reserve
        if not quota.consume(integration=self.bridge, units=CALL_COSTS[operation], limit=limit, now=now):
            raise IntegrationError(
                "The YouTube daily unit allowance is exhausted.", transient=True, retry_after=period.period_end - now,
            )
        resource, verb = operation.split(".")
        method = "POST" if verb == "insert" else "GET"
        url = API_ROOT + resource + "?" + urlencode({key: value for key, value in params.items() if value is not None})
        encoded = None if body is None else json.dumps(body).encode()
        if body is not None:
            headers = {**headers, "Content-Type": "application/json"}
        try:
            response = self.http.request(method, url, headers=headers, body=encoded, timeout=10.0, max_bytes=2**21)
        except (httpx2.ConnectError, httpx2.ConnectTimeout) as error:
            raise IntegrationError("YouTube could not be reached.", transient=True) from error
        except (OSError, httpx2.TransportError, ResponseTooLargeError) as error:
            if method == "POST":
                raise _PublishUncertain("YouTube lost the reply acknowledgement.") from error
            raise IntegrationError("YouTube could not be read.", transient=True) from error
        if response.status_code == 401:
            raise IntegrationError(
                "Google authorization was refused; reconnect the YouTube channel.", transient=False,
            )
        if response.status_code == 429:
            raise IntegrationError(
                "YouTube temporarily limited requests.", transient=True, retry_after=_retry_after(response),
            )
        if response.status_code >= 500:
            if method == "POST":
                raise _PublishUncertain("YouTube lost the reply acknowledgement.")
            raise IntegrationError(
                "YouTube is temporarily unavailable.", transient=True, retry_after=_retry_after(response),
            )
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("invalid_response")
        except ValueError as error:
            if 400 <= response.status_code < 500:
                message = f"YouTube refused the request (HTTP {response.status_code}); check the channel grant."
                if method == "GET" and operation in {"commentThreads.list", "comments.list", "videos.list"} and (
                    params.get("videoId") or params.get("parentId") or params.get("id")
                ):
                    raise _ItemUnavailable(message, transient=False) from error
                raise IntegrationError(message, transient=False) from error
            if method == "POST":
                raise _PublishUncertain("YouTube returned an unreadable reply acknowledgement.") from error
            raise IntegrationError("YouTube returned an unreadable response.", transient=True) from error
        refusal = data.get("error") or {}
        errors = refusal.get("errors", []) if isinstance(refusal, dict) else []
        reasons = {
            entry["reason"] for entry in errors
            if isinstance(entry, dict) and isinstance(entry.get("reason"), str)
        } if isinstance(errors, list) else set()
        invalid_token = isinstance(errors, list) and any(
            entry.get("location") == "pageToken" and entry.get("reason") in {"invalidParameter", "invalidValue"}
            for entry in errors if isinstance(entry, dict)
        )
        if params.get("pageToken") and (invalid_token or reasons & {"invalidPageToken", "pageTokenExpired"}):
            raise CursorInvalid()
        if reasons & _RATE_REASONS:
            raise IntegrationError(
                "YouTube temporarily limited requests.", transient=True, retry_after=_retry_after(response),
            )
        if reasons & _QUOTA_REASONS:
            raise IntegrationError(
                "YouTube refused the daily quota.", transient=True, retry_after=period.period_end - now,
            )
        if refusal or response.status_code >= 400:
            message = next((_REFUSALS[reason] for reason in sorted(reasons) if reason in _REFUSALS), None)
            message = message or f"YouTube refused the request (HTTP {response.status_code}); check the channel grant."
            if method == "GET" and operation in {"commentThreads.list", "comments.list", "videos.list"} and (
                params.get("videoId") or params.get("parentId") or params.get("id")
            ):
                raise _ItemUnavailable(message, transient=False)
            raise IntegrationError(message, transient=False)
        return data


def _items(data: dict[str, Any], bound: int = _COMMENT_LIMIT) -> list[Any]:
    """Malformed individual resources are skipped separately from an invalid page envelope."""

    rows = data.get("items")
    if not isinstance(rows, list) or len(rows) > bound:
        raise IntegrationError("YouTube returned an invalid resource page.")
    return rows


def _identity(raw: Any) -> str:
    """Only safe scalar ids enter discrepancy telemetry."""

    identity = raw.get("id") if isinstance(raw, dict) else None
    return identity if isinstance(identity, str) and len(identity) <= 512 else ""


def _out_of_time(deadline: float | None) -> bool:
    """Check the engine budget only at request boundaries, never shorten a timeout."""

    return deadline is not None and monotonic() >= deadline


def _retry_after(response: httpx2.Response) -> timedelta:
    """Honor numeric or HTTP-date throttling hints without exposing response bodies."""

    value = response.headers.get("Retry-After", "")
    try:
        seconds = float(value)
        if isfinite(seconds) and seconds > 0:
            return timedelta(seconds=seconds)
    except ValueError:
        try:
            delay = parsedate_to_datetime(value) - timezone.now()
            if delay > timedelta(0):
                return delay
        except (ValueError, TypeError):
            pass
    return timedelta(minutes=15)
