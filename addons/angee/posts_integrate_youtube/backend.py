"""YouTube channel comments through the integrate stream and quota owners."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from time import monotonic
from typing import Any, ClassVar, cast
from urllib.parse import urlencode

import httpx2
from django.apps import apps
from django.db.models import Q
from django.utils import timezone
from pydantic import Field
from pydantic import ValidationError as CursorValidationError

from angee.integrate.credentials import CredentialKind
from angee.integrate.discovery import ConnectionDiscovery
from angee.integrate.errors import IntegrationError
from angee.integrate.streams import CursorInvalid, StreamDefinition, StreamPage
from angee.messaging.backends import DeliveryUncertain, ParsedHandle
from angee.posts.backends import FeedBackend, ParsedPost, RecordRefusal

from .cursors import (
    ActivityCursor,
    HistoryCursor,
    Moderation,
    PagePosition,
    ReobserveCursor,
    ReplyPosition,
    StreamCursor,
    ThreadIdentity,
)
from .parsing import comment_post, timestamp, video_post

API_ROOT = "https://www.googleapis.com/youtube/v3/"
"""YouTube Data API origin; Google OAuth endpoints belong solely to the preset."""
CALL_COSTS = {
    "channels.list": 1, "playlistItems.list": 1, "videos.list": 1,
    "commentThreads.list": 1, "comments.list": 1, "comments.insert": 50,
}
"""Data API unit charges, including the 50-unit public reply insertion."""
_COMMENT_LIMIT = 100
_ID_LIMIT = 50  # Conservative id-filter cap, also the documented videos.list cap.
_MODERATION: tuple[Moderation, ...] = ("published", "heldForReview", "likelySpam")
_RECONCILIATION_PAGES = 10
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


class _RecheckBudgetExhausted(Exception):
    """Pause provider re-observation, retaining the unlanded page's position."""


class _ItemUnavailable(IntegrationError):
    """A permanent resource read refusal that must not wedge a work queue."""


class YouTubeFeedBackend(FeedBackend):
    """Poll one channel and publish replies using YouTube Data API v3.

    A video id identifies both the original post and its public thread. Comments
    and replies use comment resource ids; thread resource ids are lookup metadata.

    Activity retains a newest-seen publication watermark and bounds its first
    published scan by max(Feed.live_since, now - active_thread_days). Independent
    owner re-observation resumes moderation scans and identity rechecks under the
    framework's per-cycle allowance. History independently walks uploads and
    per-video comment pages.

    Cursors contain only identities, paging positions and watermarks. Discovery
    stores uploads in the read-only Feed.youtube_uploads_playlist_id extension.
    Owner moderation scans expose held/spam comments; id reads omit their state.
    An absent resource cannot prove deletion, so absence is recorded for review
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

    quota_reset_timezone: ClassVar[str] = "America/Los_Angeles"
    publish_reply_cost: ClassVar[int] = CALL_COSTS["comments.insert"]
    # Prepay a bounded lookup walk; hooks perform no quota writes.
    find_published_reply_cost: ClassVar[int] = _RECONCILIATION_PAGES

    class Config(FeedBackend.Config):
        """Extend the shared policy with the owner re-observation cadence."""

        reply_reserve: int = Field(default=1000, ge=0)
        recheck_interval: int = Field(default=3600, ge=3600)

    config_model = Config

    def quota_scope(self) -> str:
        """Channels using one OAuth client spend the same Cloud-project allowance."""

        credential = self.bridge.credential
        if credential is None or credential.oauth_client_id is None:
            raise IntegrationError("Connect this YouTube channel through Google OAuth.")
        return f"youtube:oauth-client:{credential.oauth_client_id}"

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
                raise ValueError("incomplete_discovery")
        except _ITEM_ERRORS as error:
            raise IntegrationError("YouTube returned an incomplete channel discovery.") from error
        if self.bridge.external_id and identity != self.bridge.external_id:
            raise IntegrationError("This feed belongs to another YouTube channel; create a separate feed.")
        return ConnectionDiscovery(data={
            "external_id": identity, "display_name": title,
            "handle": ParsedHandle("youtube", identity, title, identity), "uploads_playlist_id": uploads,
        })

    def apply_discovery(self, discovery: ConnectionDiscovery) -> None:
        """Apply posts identity first, then the addon-owned non-editable discovery field."""

        uploads = discovery.data["uploads_playlist_id"]
        if not isinstance(uploads, str) or not uploads:
            raise IntegrationError("YouTube discovery did not include an uploads playlist.")
        super().apply_discovery(discovery)
        self.bridge.youtube_uploads_playlist_id = uploads
        self.bridge.save(update_fields=["youtube_uploads_playlist_id", "updated_at"])

    def streams(self, *, deadline: float | None = None) -> tuple[StreamDefinition, ...]:
        """Declare independent live, history and owner re-observation partitions."""

        if not self.bridge.external_id or not self.bridge.youtube_uploads_playlist_id:
            raise IntegrationError("Connect a YouTube channel before polling comments.")
        return tuple(
            StreamDefinition(key=key, partition=self.bridge.external_id) for key in ("activity", "history", "reobserve")
        )

    def extract(self, stream: Any, page_bound: int, *, deadline: float | None = None) -> StreamPage:
        """Return a bounded page; the driver alone advances or resets its epoch."""

        cursor_type = {"activity": ActivityCursor, "history": HistoryCursor, "reobserve": ReobserveCursor}[stream.key]
        try:
            state = cursor_type.model_validate(stream.cursor)
        except CursorValidationError as error:
            raise CursorInvalid() from error
        bound = min(page_bound, _COMMENT_LIMIT)
        if state.refusals:
            refusals = state.refusals[:bound]
            state.refusals = state.refusals[bound:]
            exhausted = not state.refusals and state.stage in {"complete", "idle"}
            return StreamPage(refusals, state.model_dump(mode="json"), exhausted=exhausted)
        if isinstance(state, ActivityCursor) and (state.floor is None or state.stage == "idle"):
            floor = timezone.now() - timedelta(days=self.policy.active_thread_days)
            state.begin(floor=max(floor, self.bridge.live_since or floor))
        if isinstance(state, ReobserveCursor) and state.stage == "idle":
            if state.recheck_after is not None and state.recheck_after > timezone.now():
                return StreamPage((), state.model_dump(mode="json"), exhausted=True)
            state.begin(floor=timezone.now() - timedelta(days=self.policy.active_thread_days))
        if _out_of_time(deadline):
            return StreamPage((), state.model_dump(mode="json"), exhausted=False)
        self._refusals: list[RecordRefusal] = []
        self._reobserving = isinstance(state, ReobserveCursor)
        try:
            if state.pending:
                records = self._pending_threads(stream, state, bound, deadline=deadline)
            elif state.replies:
                records = self._replies(stream, state, bound)
            elif isinstance(state, HistoryCursor):
                records = self._history(stream, state, bound, deadline=deadline)
            else:
                records = self._activity(stream, state, bound, deadline=deadline)
        except _RecheckBudgetExhausted:
            # Replay only the unlanded page next cycle; landed pages stay committed.
            if not isinstance(state, ReobserveCursor):
                raise
            return StreamPage((), stream.cursor, exhausted=True)
        finally:
            self._reobserving = False
        # Refusals consume page slots too. Defer only their safe id/code evidence,
        # never parsed bodies, when roots/comments already occupy this page.
        room = bound - len(records)
        state.refusals = self._refusals[room:]
        exhausted = not state.refusals and state.stage == ("complete" if isinstance(state, HistoryCursor) else "idle")
        return StreamPage(
            (*records, *self._refusals[:room]), state.model_dump(mode="json"),
            exhausted=exhausted and not _out_of_time(deadline),
        )

    def _recheck_interval(self) -> int:
        return cast(YouTubeFeedBackend.Config, self.policy).recheck_interval

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
                self._refuse("page_token_loop", identity)
                if isinstance(state, ReobserveCursor) and state.stage in {"heldForReview", "likelySpam"}:
                    state.moderation_complete = False
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
                    self._refuse("malformed_upload", _identity(raw))
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
            self._refuse("video_comments_unavailable", state.videos.pop(0))
            state.threads, state.moderation = PagePosition(), "published"
            return ()
        return self._thread_records(
            stream, state, _items(data, max(1, min(_ID_LIMIT, bound // 2))), bound,
            state.moderation, deadline=deadline,
        )

    def _activity(
        self, stream: Any, state: ActivityCursor | ReobserveCursor, bound: int, *, deadline: float | None,
    ) -> tuple[ParsedPost, ...]:
        """Read live publications or a separate owner re-observation walk."""

        if state.stage in _MODERATION:
            if state.page.token is None:
                if isinstance(state, ActivityCursor):
                    state.newest_published_at = state.cycle_newest
                    state.stage, state.force = "idle", False
                else:
                    state.stage = "likelySpam" if state.stage == "heldForReview" else "threads"
                state.page = PagePosition()
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
                    self._refuse("malformed_thread", _identity(raw))
                    if state.stage != "published":
                        state.moderation_complete = False
                    continue
                floor = state.floor if isinstance(state, ActivityCursor) else state.moderation_floor
                if floor is None:
                    floor = timezone.now() - timedelta(days=self.policy.active_thread_days)
                seen = isinstance(state, ActivityCursor) and state.newest_published_at is not None and (
                    published < state.newest_published_at
                )
                if seen or (floor is not None and published < floor):
                    state.page.token = None
                    break
                if isinstance(state, ActivityCursor):
                    state.cycle_newest = max(state.cycle_newest or published, published)
                rows.append(raw)
            return self._thread_records(stream, state, rows, bound, cast(Moderation, state.stage), deadline=deadline)
        if isinstance(state, ReobserveCursor) and state.stage == "threads":
            return self._recheck_threads(stream, state, bound, deadline=deadline)
        if isinstance(state, ReobserveCursor) and state.stage == "comments":
            return self._reobserve_comments(stream, state, bound)
        return ()

    def _recent_messages(self, after: int, *, parents_only: bool = False) -> Any:
        """Use landed identities only for reconciliation, never as stream position."""

        floor = timezone.now() - timedelta(days=self.policy.active_thread_days)
        rows = apps.get_model("messaging", "Message").objects.filter(
            channel_id=self.bridge.pk, platform="youtube", is_original_post=False, pk__gt=after,
        ).filter(Q(sent_at__gte=floor) | Q(replies__sent_at__gte=floor))
        if parents_only:
            rows = rows.filter(parent__is_original_post=True).exclude(metadata__youtube__thread_id="")
        return rows.select_related("post_metrics", "parent").order_by("pk").distinct()

    def _recheck_threads(
        self, stream: Any, state: ReobserveCursor, bound: int, *, deadline: float | None,
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
                self._refuse("thread_unavailable", identity)
            return ()
        return self._thread_records(
            stream, state, _items(data, len(identities)), bound, "published", deadline=deadline, changed_only=True,
        )

    def _reobserve_comments(self, stream: Any, state: ReobserveCursor, bound: int) -> tuple[ParsedPost, ...]:
        """Owner id reads refresh available moderation without inferring deletion from absence."""

        candidates = list(self._recent_messages(state.active_after)[:min(_ID_LIMIT, bound)])
        if not candidates:
            state.stage, state.force = "idle", False
            state.recheck_after = timezone.now() + timedelta(seconds=self._recheck_interval())
            return ()
        state.active_after = candidates[-1].pk
        by_id = {row.external_id: row for row in candidates}
        hidden_parents = {
            row.parent_id for row in candidates if row.parent is not None and row.parent.provider_hidden
        }
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
                prior_hidden = row.provider_hidden
                # Id reads omit moderationStatus. Complete owner scans can clear
                # a prior hold; incomplete scans retain the previous evidence.
                covered = (
                    row.parent is not None and row.parent.is_original_post
                    and state.moderation_complete and state.moderation_floor is not None
                    and row.sent_at is not None and row.sent_at >= state.moderation_floor
                )
                held = row.external_id in state.hidden_ids or (prior_hidden and not covered)
                if row.parent_id is not None and row.parent_id in hidden_parents:
                    held = True
                records.append(comment_post(
                    raw, video_id=metadata["video_id"], channel_id=self.bridge.external_id,
                    thread_id=metadata.get("thread_id", ""),
                    reply_count=getattr(getattr(row, "post_metrics", None), "reply_count", 0),
                    moderation_status="heldForReview" if held else "published",
                ))
                seen.add(row.external_id)
            except _ITEM_ERRORS:
                self._refuse("malformed_comment", _identity(raw))
        for identity in by_id.keys() - seen:
            self._refuse("comment_unavailable", identity)
        return tuple(records)

    def _pending_threads(
        self, stream: Any, state: StreamCursor, bound: int, *, deadline: float | None,
    ) -> tuple[ParsedPost, ...]:
        """Rehydrate deferred identities without ever storing their provider payloads."""

        chosen = state.pending[:min(_ID_LIMIT, max(1, bound // 2))]
        if isinstance(state, ReobserveCursor):
            existing = set(apps.get_model("messaging", "Message").objects.filter(
                channel_id=self.bridge.pk, platform="youtube", is_original_post=True,
                external_id__in=[work.video_id for work in chosen],
            ).values_list("external_id", flat=True))
            missing = list(dict.fromkeys(work.video_id for work in chosen if work.video_id not in existing))
            if missing:
                roots = self._videos(stream, missing)
                unavailable = set(missing) - {record.message.external_id for record in roots}
                state.pending = [work for work in state.pending if work.video_id not in unavailable]
                return roots
        try:
            data = self._request(
                "commentThreads.list", id=",".join(work.thread_id for work in chosen),
                part="snippet", textFormat="plainText",
            )
        except _ItemUnavailable:
            for work in chosen:
                self._refuse("thread_unavailable", work.thread_id)
            state.pending = state.pending[len(chosen):]
            return ()
        state.pending = state.pending[len(chosen):]
        by_id = {work.thread_id: work for work in chosen}
        rows = []
        for raw in _items(data, len(chosen)):
            work = by_id.pop(_identity(raw), None)
            if work is None:
                self._refuse("unexpected_thread", _identity(raw))
                continue
            rows.append(raw)
        for identity in by_id:
            self._refuse("thread_unavailable", identity)
        return self._thread_records(
            stream, state, rows, bound, chosen[0].moderation, deadline=deadline,
        )

    def _thread_records(
        self, stream: Any, state: StreamCursor, rows: list[Any], bound: int, moderation: Moderation,
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
                if (isinstance(state, ReobserveCursor) and moderation != "published"
                        and top["id"] not in state.hidden_ids):
                    if len(state.hidden_ids) < 5000:
                        state.hidden_ids.append(top["id"])
                    else:
                        state.moderation_complete = False
            except _ITEM_ERRORS:
                self._refuse("malformed_thread", _identity(raw))
                if isinstance(state, ReobserveCursor) and moderation != "published":
                    state.moderation_complete = False
        comments = [top["id"] for _, top, _ in usable]
        counts = dict(apps.get_model("posts", "PostMetrics").objects.filter(
            message__channel_id=self.bridge.pk, message__platform="youtube", message__external_id__in=comments,
        ).values_list("message__external_id", "reply_count"))
        if changed_only:
            usable = [entry for entry in usable if state.force or entry[2] != counts.get(entry[1]["id"])]
        prior_hidden = {
            row.external_id for row in apps.get_model("messaging", "Message").objects.filter(
                channel_id=self.bridge.pk, platform="youtube", external_id__in=comments, is_trashed=True,
                metadata__local__has_key="provider_hidden",
            ) if row.provider_hidden
        } if changed_only else set()
        video_ids = list(dict.fromkeys(work.video_id for work, _, _ in usable))
        existing = set(apps.get_model("messaging", "Message").objects.filter(
            channel_id=self.bridge.pk, platform="youtube", is_original_post=True, external_id__in=video_ids,
        ).values_list("external_id", flat=True))
        missing = [identity for identity in video_ids if identity not in existing]
        if missing and _out_of_time(deadline):
            state.pending.extend(work for work, _, _ in usable)
            return ()
        try:
            roots = self._videos(stream, missing[:min(_ID_LIMIT, bound)]) if missing else ()
        except _RecheckBudgetExhausted:
            state.pending.extend(work for work, _, _ in usable)
            return ()
        available = existing | {record.message.external_id for record in roots}
        records = list(roots)
        for work, top, count in usable:
            if work.video_id not in available:
                self._refuse("video_unavailable", work.video_id)
                continue
            try:
                record = comment_post(
                    top, video_id=work.video_id, channel_id=self.bridge.external_id, thread_id=work.thread_id,
                    reply_count=count, moderation_status=(
                        "heldForReview" if top["id"] in prior_hidden else work.moderation
                    ),
                )
            except _ITEM_ERRORS:
                self._refuse("malformed_comment", _identity(top))
                continue
            if len(records) == bound:
                state.pending.append(work)
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
            self._refuse("replies_unavailable", work.parent_id)
            state.replies.pop(0)
            return ()
        rows = _items(data, bound)
        prior_hidden = {
            row.external_id for row in apps.get_model("messaging", "Message").objects.filter(
                channel_id=self.bridge.pk, platform="youtube", external_id__in=[_identity(raw) for raw in rows],
                is_trashed=True, metadata__local__has_key="provider_hidden",
            ) if row.provider_hidden
        }
        records = []
        for raw in rows:
            try:
                if raw["snippet"].get("parentId") != work.parent_id:
                    raise ValueError("unexpected_parent")
                records.append(comment_post(
                    raw, video_id=work.video_id, channel_id=self.bridge.external_id, hidden_parent=work.hidden,
                    moderation_status="heldForReview" if raw["id"] in prior_hidden else "published",
                ))
            except _ITEM_ERRORS:
                self._refuse("malformed_reply", _identity(raw))
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
                self._refuse("video_unavailable", identity)
            return ()
        records = []
        for raw in _items(data, len(identities)):
            try:
                if (raw["id"] not in identities or raw["snippet"]["channelId"] != self.bridge.external_id
                        or raw.get("status", {}).get("privacyStatus") == "private"):
                    continue
                records.append(video_post(raw, channel_id=self.bridge.external_id))
            except _ITEM_ERRORS:
                self._refuse("malformed_video", _identity(raw))
        return tuple(records)

    def _refuse(self, code: str, identity: str) -> None:
        """Return safe evidence with the page, outside any database transaction."""

        identity = identity or f"{self.bridge.external_id}:{code}:{len(self._refusals)}"
        self._refusals.append(RecordRefusal(identity, f"youtube_{code}"))

    def publish_reply(self, message: Any) -> str:
        """Post once on the messaging-owned anchor, without local settlement rules."""

        data = self._request("comments.insert", delivery=True, part="snippet", body={
            "snippet": {"parentId": message.top_level_comment().external_id, "textOriginal": message.body_text()},
        })
        identity = data.get("id")
        if not isinstance(identity, str) or not identity.strip():
            raise DeliveryUncertain("YouTube did not acknowledge the reply identity.")
        return identity

    def find_published_reply(self, message: Any, *, since: datetime) -> str | None:
        """Read a bounded complete reply set, then match newest-first.

        comments.list has no order parameter. An old record in a provider page
        cannot terminate pagination. Incomplete walks and absent unindexed
        acknowledgements cannot authorize another POST.
        """

        parent_id, text = message.top_level_comment().external_id, message.body_text()
        token, seen = "", set()
        replies: list[tuple[datetime, dict[str, Any]]] = []
        for _ in range(_RECONCILIATION_PAGES):
            try:
                data = self._request(
                    "comments.list", delivery=True, parentId=parent_id, part="snippet", textFormat="plainText",
                    maxResults=_COMMENT_LIMIT, pageToken=token or None,
                )
            except CursorInvalid:
                raise IntegrationError("YouTube refused the reply lookup cursor.") from None
            for raw in _items(data):
                try:
                    created = timestamp(raw["snippet"]["publishedAt"])
                    if created is None:
                        raise ValueError("missing_publication")
                    replies.append((created, raw))
                except _ITEM_ERRORS:
                    raise IntegrationError("YouTube returned an invalid reply lookup record.") from None
            following = data.get("nextPageToken")
            if following is None:
                break
            if not isinstance(following, str) or not following or following == token or following in seen:
                raise IntegrationError("YouTube repeated the reply lookup cursor.")
            token = following
            seen.add(token)
        else:
            raise IntegrationError("YouTube reply reconciliation exceeded its page budget.")
        for created, raw in sorted(replies, key=lambda entry: entry[0], reverse=True):
            if created < since:
                break
            snippet = raw["snippet"]
            author = snippet.get("authorChannelId") or {}
            if not isinstance(author, dict):
                raise IntegrationError("YouTube returned an invalid reply author.")
            if (author.get("value") == self.bridge.external_id and snippet.get("parentId") == parent_id
                    and snippet.get("textOriginal") == text):
                identity = raw.get("id")
                if not isinstance(identity, str) or not identity.strip():
                    raise IntegrationError("YouTube returned an invalid reply identity.")
                return identity
        # No documented read-after-write/indexing horizon establishes absence.
        raise DeliveryUncertain("The YouTube reply is absent or not yet indexed; inspect the thread.")

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
        now = timezone.now()
        if delivery:
            period = self.current_quota_period(now=now)
        elif getattr(self, "_reobserving", False):
            if not self.consume_reobserve_quota(CALL_COSTS[operation], now=now):
                raise _RecheckBudgetExhausted()
            period = self.current_quota_period(now=now)
        else:
            period = self.consume_quota(CALL_COSTS[operation], now=now)
        resource, verb = operation.split(".")
        method = "POST" if verb == "insert" else "GET"
        url = API_ROOT + resource + "?" + urlencode({key: value for key, value in params.items() if value is not None})
        encoded = None if body is None else json.dumps(body).encode()
        if body is not None:
            headers = {**headers, "Content-Type": "application/json"}
        return self.request_json(
            method, url, headers=headers, body=encoded, timeout=10, max_bytes=2**21,
            is_transient=lambda response, data: False,
            refusal=lambda response, data: self._request_refusal(
                response, data, operation=operation, params=params, period=period, now=now,
            ),
        )

    def _request_refusal(
        self, response: httpx2.Response, data: dict[str, Any], *, operation: str,
        params: dict[str, Any], period: Any, now: datetime,
    ) -> Exception | None:
        """Supply YouTube quota, rate, cursor and resource refusals to the shared classifier."""

        if response.status_code == 401:
            return IntegrationError("Google authorization was refused; reconnect the YouTube channel.")
        if response.status_code == 429:
            return IntegrationError("YouTube temporarily limited requests.", retry_after=self.retry_after(response))
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
            return CursorInvalid()
        if reasons & _RATE_REASONS:
            return IntegrationError(
                "YouTube temporarily limited requests.", transient=True, retry_after=self.retry_after(response),
            )
        if reasons & _QUOTA_REASONS:
            return IntegrationError("YouTube refused the daily quota.", retry_after=period.period_end - now)
        if refusal or response.status_code >= 400:
            message = next((_REFUSALS[reason] for reason in sorted(reasons) if reason in _REFUSALS), None)
            message = message or f"YouTube refused the request (HTTP {response.status_code}); check the channel grant."
            if operation in {"commentThreads.list", "comments.list", "videos.list"} and (
                params.get("videoId") or params.get("parentId") or params.get("id")
            ):
                return _ItemUnavailable(message, transient=False)
            return IntegrationError(message, transient=False)
        return None


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
