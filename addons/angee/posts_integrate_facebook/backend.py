"""Facebook Page activity, archive extraction, and reconciled public replies."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from math import isfinite
from time import monotonic
from typing import Any, ClassVar
from urllib.parse import quote, urlencode

import httpx2
from django.apps import apps
from django.core.exceptions import ValidationError
from django.utils import timezone
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic import ValidationError as CursorValidationError

from angee.integrate.credentials import CredentialKind
from angee.integrate.discovery import ConnectionDiscovery, DerivedCredential
from angee.integrate.errors import IntegrationError
from angee.integrate.streams import CursorInvalid, SemanticError, StreamDefinition, StreamPage
from angee.messaging.backends import DeliveryUncertain, ParsedHandle
from angee.posts.backends import FeedBackend, ParsedPost, RecordRefusal

from .oauth import MetaFacebook
from .parsing import comment_post, root_post, timestamp

_POST_FIELDS = "id,message,story,created_time,permalink_url,comments.filter(stream).limit(0).summary(true)"
_COMMENT_FIELDS = "id,from,message,created_time,parent,is_hidden,like_count,comment_count"
_REPLY_FIELDS = "id,from,message,created_time,parent"
_RECONCILIATION_PAGES = 10
_RECHECK_INTERVAL = timedelta(hours=1)
_RATE_CODES = frozenset({4, 17, 32, 613, 80001})
_GRAPH_PAGE_LIMIT = 100


class FacebookCursor(BaseModel):
    """Durable positions contain only ids, opaque cursors, and time watermarks."""

    model_config = ConfigDict(extra="forbid", strict=True)
    pending: list[str] = Field(default_factory=list, max_length=_GRAPH_PAGE_LIMIT)
    posts_after: str = ""
    comments_after: str = ""
    posts_seen: list[str] = Field(default_factory=list, max_length=16)
    comments_seen: list[str] = Field(default_factory=list, max_length=16)
    posts_done: bool = False
    complete: bool = False
    force: bool = False
    posts_resets: int = Field(default=0, ge=0, le=1)
    comments_resets: int = Field(default=0, ge=0, le=1)
    newest_published_at: str = ""
    scan_newest: str = ""
    scan_floor: str = ""
    recheck_after: str = ""

    @field_validator("newest_published_at", "scan_newest", "scan_floor", "recheck_after")
    @classmethod
    def valid_time(cls, value: str) -> str:
        """Require an aware publication time when a watermark is present."""

        if value and timestamp(value) is None:
            raise ValueError("invalid watermark")
        return value

    @field_validator("pending")
    @classmethod
    def valid_ids(cls, values: list[str]) -> list[str]:
        """Retain Graph resource identities without carrying record payloads."""

        if any(not value or not value.replace("_", "").isdigit() for value in values):
            raise ValueError("invalid resource id")
        return values

    def dump(self) -> dict[str, Any]:
        """Keep the JSON position small without storing provider record payloads."""

        return self.model_dump(exclude_defaults=True)


class FacebookFeedBackend(FeedBackend):
    """Extract one Page through independent live, history and re-observation streams.

    Root message and thread ``external_id`` values are Graph Page post ids
    (``<page-id>_<post-id>``). Comments and replies use Graph comment resource ids.
    Activity compares comment summaries; re-observation re-reads the active window
    hourly under the shared cycle allowance. History reads each comment stream once.
    Activity scans newest-first within the live/active window, retaining its
    newest-seen watermark independently of landed metrics. Paging URLs are never
    followed. Streams run serially because
    Page requests share one business usage allowance and one quota ledger.
    """

    key = "facebook"
    label = "Facebook Page"
    icon = "facebook"
    oauth_client = "facebook"
    defaults = {"vendor": "meta", "poll_interval": 900}
    sync_parallelism = 1

    # Prepay the bounded walk; reconciliation hooks never debit quota.
    find_published_reply_cost: ClassVar[int] = _RECONCILIATION_PAGES

    def discover_connection(self, credential: Any) -> ConnectionDiscovery:
        """Read Page facts; integrate persists derived material after fencing."""

        if credential.kind == CredentialKind.STATIC_TOKEN and self.bridge.external_id:
            return ConnectionDiscovery()
        if credential.kind != CredentialKind.OAUTH or credential.oauth_client is None:
            raise IntegrationError("Connect this Facebook Page through Meta OAuth.", transient=False)
        pages: dict[str, dict[str, Any]] = {}
        after, seen = "", set()
        while True:
            data = self._request(
                "me/accounts", credential=credential, fields="id,name,access_token",
                limit=_GRAPH_PAGE_LIMIT, after=after or None,
            )
            for page in _rows(data):
                identity = page.get("id") if isinstance(page, dict) else None
                if (not isinstance(identity, str) or not identity.isdigit()
                        or not isinstance(page.get("access_token"), str) or not page["access_token"]):
                    raise IntegrationError("Facebook returned an unusable Page discovery.")
                pages[identity] = page
            if len(pages) > 1:
                raise IntegrationError(
                    "Page selection is not supported by connection discovery; "
                    "grant this app access to exactly one Facebook Page."
                )
            try:
                after = _after(data, previous=after, seen=seen)
            except CursorInvalid:
                raise IntegrationError("Facebook returned an invalid Page discovery cursor.") from None
            if not after:
                break
            seen.add(after)
        if not pages:
            raise IntegrationError("Facebook returned no managed Page; check the granted Page permissions.")
        identity, page = next(iter(pages.items()))
        if self.bridge.external_id and self.bridge.external_id != identity:
            raise IntegrationError("This feed is connected to another Facebook Page; create a separate feed.")
        title = str(page.get("name") or identity)
        material = {"api_key": page["access_token"]}
        return ConnectionDiscovery(
            data={
                "external_id": identity, "display_name": title,
                "handle": ParsedHandle("facebook", identity, title, identity),
            },
            credential=DerivedCredential(
                kind=CredentialKind.STATIC_TOKEN, name=f"Facebook Page ({identity})", material=material,
            ),
        )

    def streams(self, *, deadline: float | None = None) -> tuple[StreamDefinition, ...]:
        """Prioritize activity before independent, resumable backfill."""

        if not self.bridge.external_id:
            raise IntegrationError("Connect a Facebook Page before polling comments.")
        return tuple(
            StreamDefinition(key=key, partition=self.bridge.external_id) for key in ("activity", "history", "reobserve")
        )

    def extract(self, stream: Any, page_bound: int, *, deadline: float | None = None) -> StreamPage:
        """Fetch one bounded page; integrate commits its cursor with landing.

        Pending work contains at most one posts page's identities. Reset only
        the failing provider position; preserve pending ids and time watermarks.
        A malformed record travels to the driver's quarantine with its page.
        """

        state = _cursor(stream.cursor)
        limit = min(page_bound, _GRAPH_PAGE_LIMIT)
        if stream.key == "history" and state.complete:
            return StreamPage((), state.dump(), exhausted=True)
        if deadline is not None and monotonic() >= deadline:
            return StreamPage((), state.dump(), exhausted=False)
        if stream.key == "reobserve":
            due = timestamp(state.recheck_after)
            if not state.scan_floor and due is not None and due > timezone.now():
                return StreamPage((), state.dump(), exhausted=True)
            if not self.consume_reobserve_quota(1):
                return StreamPage((), stream.cursor, exhausted=True)
        if state.pending:
            return self._comments_page(stream, state, limit)
        if stream.key in {"activity", "reobserve"} and not state.scan_floor:
            floor = timezone.now() - timedelta(days=self.policy.active_thread_days)
            if stream.key == "activity" and self.bridge.live_since is not None:
                floor = max(floor, self.bridge.live_since)
            state.scan_floor = floor.isoformat()
        floor = timestamp(state.scan_floor)
        try:
            data = self._request(
                f"{stream.partition}/posts", fields=_POST_FIELDS, limit=limit,
                after=state.posts_after or None, reobserve=stream.key == "reobserve",
                since=int(floor.timestamp()) if floor is not None else None,
            )
            after = _after(data, previous=state.posts_after, seen=state.posts_seen)
        except CursorInvalid:
            if state.posts_resets:
                raise IntegrationError("Facebook repeatedly refused the posts cursor; reset this stream.") from None
            state.posts_after, state.posts_seen = "", []
            state.posts_resets = 1
            raise CursorInvalid(cursor=state.dump()) from None
        records: list[ParsedPost | RecordRefusal] = []
        scan_newest = timestamp(state.scan_newest)
        for index, raw in enumerate(_rows(data, limit)):
            record = self._parse(raw, post_id="", fallback=f"{stream.partition}:post:{index}")
            if isinstance(record, ParsedPost):
                published = record.message.sent_at
                if published is not None and floor is not None and published < floor:
                    after = ""
                    break
                if published is not None and (scan_newest is None or published > scan_newest):
                    state.scan_newest = published.isoformat()
                    scan_newest = published
            records.append(record)
        roots = [record for record in records if isinstance(record, ParsedPost)]
        counts = dict(
            apps.get_model("posts", "PostMetrics").objects.filter(
                message__channel_id=self.bridge.pk, message__platform="facebook",
                message__external_id__in=[record.message.external_id for record in roots],
            ).values_list("message__external_id", "reply_count")
        )
        newest = timestamp(state.newest_published_at)
        state.pending = [
            record.message.external_id for record in roots
            if stream.key in {"history", "reobserve"} or state.force
            or (record.message.sent_at is not None and (newest is None or record.message.sent_at > newest))
            or record.message.external_id not in counts
            or record.metrics is None or record.metrics.reply_count != counts[record.message.external_id]
        ]
        state.posts_after = after
        state.posts_seen = [*state.posts_seen, after][-16:] if after else []
        state.posts_done = not after
        return self._page(stream.key, records, state)

    def _comments_page(self, stream: Any, state: FacebookCursor, limit: int) -> StreamPage:
        post_id = state.pending[0]
        try:
            data = self._request(
                f"{post_id}/comments", fields=_COMMENT_FIELDS, filter="stream", order="chronological",
                limit=limit, after=state.comments_after or None, record_local=True, reobserve=stream.key == "reobserve",
            )
            after = _after(data, previous=state.comments_after, seen=state.comments_seen)
            try:
                rows = _rows(data, limit)
            except IntegrationError:
                raise SemanticError("invalid_comment_page") from None
            records = [
                self._parse(raw, post_id=post_id, fallback=f"{post_id}:comment:{index}")
                for index, raw in enumerate(rows)
            ]
        except CursorInvalid:
            if not state.comments_resets:
                state.comments_after, state.comments_seen = "", []
                state.comments_resets = 1
                raise CursorInvalid(cursor=state.dump()) from None
            after, records = "", [RecordRefusal(post_id, "repeated_comment_cursor")]
        except SemanticError as error:
            after, records = "", [RecordRefusal(post_id, error.code)]
        if after:
            state.comments_after = after
            state.comments_seen = [*state.comments_seen, after][-16:]
        else:
            state.comments_after, state.comments_seen = "", []
            state.comments_resets = 0
            state.pending = state.pending[1:]
        return self._page(stream.key, records, state)

    def _parse(self, raw: Any, *, post_id: str, fallback: str) -> ParsedPost | RecordRefusal:
        try:
            if not isinstance(raw, dict):
                raise ValueError("invalid record")
            if post_id:
                return comment_post(raw, post_id=post_id, page_id=self.bridge.external_id)
            return root_post(raw, page_id=self.bridge.external_id, page_name=self.bridge.display_name)
        except (IntegrationError, ValidationError, KeyError, TypeError, ValueError, AttributeError):
            identity = raw.get("id") if isinstance(raw, dict) else None
            safe_id = identity if isinstance(identity, str) and identity.replace("_", "").isdigit() else fallback
            return RecordRefusal(safe_id, "invalid_record")

    @staticmethod
    def _page(key: str, records: Sequence[ParsedPost | RecordRefusal], state: FacebookCursor) -> StreamPage:
        exhausted = state.posts_done and not state.pending
        if exhausted:
            seen = [moment for value in (state.newest_published_at, state.scan_newest)
                    if (moment := timestamp(value)) is not None]
            newest = max(seen, default=None)
            state = FacebookCursor(
                complete=key == "history", newest_published_at=newest.isoformat() if newest is not None else "",
                recheck_after=(
                    (timezone.now() + _RECHECK_INTERVAL).isoformat() if key == "reobserve" else state.recheck_after
                ),
            )
        return StreamPage(records, state.dump(), exhausted=exhausted)

    def publish_reply(self, message: Any) -> str:
        """Publish on the messaging-owned anchor; the base owns settlement."""

        data = self._request(
            f"{message.top_level_comment().external_id}/comments", method="POST",
            form={"message": message.body_text()}, delivery=True,
        )
        identity = data.get("id")
        if not isinstance(identity, str) or not identity.strip():
            raise DeliveryUncertain("Facebook did not acknowledge the reply identity.")
        return identity

    def find_published_reply(self, message: Any, *, since: datetime) -> str | None:
        """Walk newest-first on this anchor, stopping at the inclusive time bound."""

        parent_id, text = message.top_level_comment().external_id, message.body_text()
        after = ""
        seen: set[str] = set()
        differing_reply = False
        window_complete = False
        for _ in range(_RECONCILIATION_PAGES):
            try:
                data = self._request(
                    f"{parent_id}/comments", filter="stream", order="reverse_chronological",
                    fields=_REPLY_FIELDS, limit=_GRAPH_PAGE_LIMIT, after=after or None, delivery=True,
                )
                for reply in _rows(data):
                    created = timestamp(reply.get("created_time")) if isinstance(reply, dict) else None
                    if created is None:
                        raise IntegrationError("Facebook returned an undated reply lookup record.")
                    if created < since:
                        window_complete = True
                        break
                    author, parent = reply.get("from") or {}, reply.get("parent") or {}
                    if not isinstance(author, dict) or not isinstance(parent, dict):
                        raise IntegrationError("Facebook returned an invalid reply lookup record.")
                    if not author.get("id") or not parent.get("id"):
                        raise IntegrationError("Facebook omitted identity needed for reply reconciliation.")
                    if author.get("id") == self.bridge.external_id and parent.get("id") == parent_id:
                        if reply.get("message") != text:
                            differing_reply = True
                            continue
                        identity = reply.get("id")
                        if not isinstance(identity, str) or not identity.strip():
                            raise IntegrationError("Facebook returned an invalid reply identity.")
                        return identity
                if window_complete:
                    break
                after = _after(data, previous=after, seen=seen)
            except CursorInvalid:
                raise IntegrationError("Facebook refused the reply lookup cursor.") from None
            if not after:
                break
            seen.add(after)
        else:
            raise IntegrationError("Facebook reply reconciliation exceeded its page budget.")
        if differing_reply:
            raise DeliveryUncertain("A recent Page reply may have a normalized body.")
        return None

    @staticmethod
    def retry_after(response: Any, *, now: datetime | None = None) -> timedelta:
        """Combine the shared Retry-After parser with Meta usage recovery hints.

        Business usage supplies recovery minutes. Page/app usage supplies only
        utilization; at 100% use a conservative one-hour cooling interval.
        """

        delays = [FeedBackend.retry_after(response, now=now).total_seconds()]
        try:
            usage = json.loads(response.headers.get("X-Business-Use-Case-Usage", "{}"))
            for entries in usage.values():
                for entry in entries:
                    minutes = float(entry.get("estimated_time_to_regain_access") or 0)
                    if minutes > 0 and isfinite(minutes):
                        delays.append(minutes * 60)
        except (TypeError, ValueError, AttributeError, OverflowError):
            pass
        for header in ("X-Page-Usage", "X-App-Usage"):
            try:
                usage = json.loads(response.headers.get(header, "{}"))
                if any(float(usage.get(key, 0)) >= 100 for key in ("call_count", "total_cputime", "total_time")):
                    delays.append(3600)
            except (TypeError, ValueError, AttributeError, OverflowError):
                pass
        return timedelta(seconds=max(delays))

    def _request(
        self, path: str, *, method: str = "GET", credential: Any = None,
        form: dict[str, str] | None = None, delivery: bool = False, record_local: bool = False,
        reobserve: bool = False, **params: Any,
    ) -> dict[str, Any]:
        credential = credential or self.bridge.credential
        if credential is None:
            raise IntegrationError("Connect a Facebook Page before making requests.", transient=False)
        credential.ensure_fresh()
        headers = credential.auth_headers()
        if not delivery and not reobserve:
            self.consume_quota(1)
        # FK descriptors retain the app identity for this adapter's sync cycle.
        # A fresh cycle reloads the client, including a rotated secret.
        client = credential.oauth_client
        if client is None and self.bridge.discovery_credential_id is not None:
            client = self.bridge.discovery_credential.oauth_client
        url = MetaFacebook.graph_url(quote(path, safe="/"))
        query = urlencode({
            key: value for key, value in (params | MetaFacebook.auth_params(credential, client=client)).items()
            if value is not None
        })
        if query:
            url += "?" + query
        body = None
        if form is not None:
            body = urlencode(form).encode()
            headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
        return self.request_json(
            method, url, headers=headers, body=body, timeout=10, max_bytes=2**21,
            is_transient=self._is_transient,
            refusal=lambda response, data: self._request_refusal(
                response, data, params=params, record_local=record_local,
            ),
        )

    @staticmethod
    def _is_transient(response: httpx2.Response, data: dict[str, Any]) -> bool:
        """Graph's ambiguous temporary outcomes precede definite refusals."""

        error = data.get("error")
        return isinstance(error, dict) and (error.get("is_transient") is True or error.get("code") in (1, 2))

    def _request_refusal(
        self, response: httpx2.Response, data: dict[str, Any], *, params: dict[str, Any], record_local: bool,
    ) -> Exception | None:
        """Graph-owned authorization, rate, cursor and record-local refusals."""

        error = data.get("error") or {}
        code = error.get("code") if isinstance(error, dict) else None
        if code == 190:
            return IntegrationError("Facebook authorization was refused; reconnect the Page.")
        if (isinstance(code, int) and code in _RATE_CODES) or response.status_code == 429:
            delay = self.retry_after(response)
            if code in (4, 17, 32) and not any(response.headers.get(header) for header in (
                "Retry-After", "X-Business-Use-Case-Usage", "X-Page-Usage", "X-App-Usage",
            )):
                delay = timedelta(minutes=15)
            return IntegrationError("Facebook temporarily limited Page requests.", retry_after=delay)
        if params.get("after") and code == 100 and "cursor" in str(error.get("message") or "").lower():
            return CursorInvalid()
        if error or response.status_code >= 400:
            if record_local:
                return SemanticError("comment_read_refused")
            return IntegrationError("Facebook refused the request; check Page permissions.")
        return None


def _cursor(value: dict[str, Any]) -> FacebookCursor:
    """Repair invalid fields without erasing other positions or watermarks."""

    seed = {key: item for key, item in value.items() if key in FacebookCursor.model_fields}
    invalid = seed != value
    lost_pending = False
    while True:
        try:
            state = FacebookCursor.model_validate(seed)
            break
        except CursorValidationError as error:
            invalid = True
            for issue in error.errors():
                lost_pending |= issue["loc"][0] == "pending"
                seed.pop(issue["loc"][0], None)
    if invalid:
        if lost_pending:
            state.posts_after, state.posts_seen, state.posts_done = "", [], False
            state.comments_after, state.comments_seen = "", []
            state.force = True
        raise CursorInvalid(cursor=state.dump())
    return state


def _rows(data: dict[str, Any], bound: int = _GRAPH_PAGE_LIMIT) -> list[Any]:
    rows = data.get("data")
    if not isinstance(rows, list) or len(rows) > bound:
        raise IntegrationError("Facebook returned an invalid record page.")
    return rows


def _after(data: dict[str, Any], *, previous: str, seen: Any = ()) -> str:
    paging = data.get("paging") or {}
    if not isinstance(paging, dict) or not isinstance(seen, (list, tuple, set)):
        raise CursorInvalid()
    if not paging.get("next"):
        return ""
    cursors = paging.get("cursors") or {}
    if not isinstance(cursors, dict):
        raise CursorInvalid()
    after = cursors.get("after")
    if not isinstance(after, str) or not after or after == previous or after in seen:
        raise CursorInvalid()
    return after
