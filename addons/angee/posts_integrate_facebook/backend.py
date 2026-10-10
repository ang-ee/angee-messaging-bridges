"""Facebook Page activity, archive extraction, and reconciled public replies."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from math import isfinite
from time import monotonic
from typing import Any, cast
from urllib.parse import quote, urlencode

import httpx2
from django.apps import apps
from django.core.exceptions import ValidationError
from django.utils import timezone
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic import ValidationError as CursorValidationError

from angee.integrate.credentials import CredentialKind
from angee.integrate.discovery import ConnectionDiscovery, DerivedCredential
from angee.integrate.errors import IntegrationError
from angee.integrate.http import ResponseTooLargeError
from angee.integrate.streams import ApplyResult, CursorInvalid, SemanticError, StreamDefinition, StreamPage
from angee.messaging.backends import DeliveryOutcome, ParsedHandle
from angee.messaging.delivery import TransientDeliveryError
from angee.posts.backends import FeedBackend, ParsedPost

from .oauth import MetaFacebook
from .parsing import comment_post, root_post, timestamp

_POST_FIELDS = "id,message,story,created_time,permalink_url,comments.filter(stream).limit(0).summary(true)"
_COMMENT_FIELDS = "id,from,message,created_time,parent,is_hidden,like_count,comment_count"
_REPLY_FIELDS = "id,from,message,created_time"
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

    @field_validator("newest_published_at", "scan_newest", "scan_floor")
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


@dataclass(frozen=True, slots=True)
class FacebookRecordRefusal:
    """Safe evidence for integrate's per-record quarantine, without raw payloads."""

    external_id: str
    code: str


class _PublishUncertain(IntegrationError):
    """A publish may have succeeded and requires reconciliation before settlement."""


class FacebookFeedBackend(FeedBackend):
    """Extract one Page through integrate's durable activity and history streams.

    Root message and thread ``external_id`` values are Graph Page post ids
    (``<page-id>_<post-id>``). Comments and replies use Graph comment resource ids.
    Activity compares stream comment summaries with stored root ``reply_count``;
    history reads every comment stream once. Activity scans newest-first within
    the live/active window, retaining its newest-seen watermark independently of
    landed metrics. Paging URLs are never followed. Streams run serially because
    Page requests share one business usage allowance and one quota ledger.
    """

    key = "facebook"
    label = "Facebook Page"
    icon = "facebook"
    oauth_client = "facebook"
    defaults = {"vendor": "meta", "poll_interval": 900}
    sync_parallelism = 1

    class Config(BaseModel):
        """Integration-scoped daily allowance, reply reserve, and activity window."""

        model_config = ConfigDict(extra="allow")
        quota_limit: int = Field(default=10000, ge=1)
        reply_reserve: int = Field(default=100, ge=0)
        active_thread_days: int = Field(default=30, ge=1)

        @model_validator(mode="after")
        def check_reserve(self) -> FacebookFeedBackend.Config:
            """Leave a positive polling allowance within the daily ceiling."""

            if self.reply_reserve >= self.quota_limit:
                raise ValueError("reply_reserve must be smaller than quota_limit")
            return self

    config_model = Config

    @property
    def policy(self) -> FacebookFeedBackend.Config:
        """Resolve configuration through the implementation descriptor."""

        return cast(self.Config, self.parse_config(self.bridge.config))

    def after_connect(self, credential: Any) -> ConnectionDiscovery:
        """Compose integrate's adapter dispatch with connection discovery."""

        return self.discover_connection(credential)

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
                    raise IntegrationError("Facebook returned an unusable Page binding.")
                pages[identity] = page
            if len(pages) > 1:
                raise IntegrationError("Connect an account that manages exactly one Facebook Page.")
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
            raise IntegrationError("This feed is bound to another Facebook Page; create a separate feed.")
        title = str(page.get("name") or identity)
        material = {"api_key": page["access_token"]}
        proof = MetaFacebook.appsecret_proof(page["access_token"], credential.oauth_client)
        if proof:
            material["appsecret_proof"] = proof
        return ConnectionDiscovery(
            data={
                "external_id": identity, "display_name": title,
                "handle": ParsedHandle("facebook", identity, title, identity),
            },
            credential=DerivedCredential(
                kind=CredentialKind.STATIC_TOKEN, name=f"Facebook Page ({identity})", material=material,
            ),
        )

    def apply_discovery(self, discovery: ConnectionDiscovery) -> None:
        """Delegate Page identity writes to posts under integrate's row fence."""

        super().apply_discovery(discovery)

    def streams(self, *, deadline: float | None = None) -> tuple[StreamDefinition, ...]:
        """Prioritize activity before independent, resumable backfill."""

        if not self.bridge.external_id:
            raise IntegrationError("Connect a Facebook Page before polling comments.")
        return tuple(StreamDefinition(key=key, partition=self.bridge.external_id) for key in ("activity", "history"))

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
        if state.pending:
            return self._comments_page(stream, state, limit)
        if stream.key == "activity" and not state.scan_floor:
            floor = timezone.now() - timedelta(days=self.policy.active_thread_days)
            if self.bridge.live_since is not None:
                floor = max(floor, self.bridge.live_since)
            state.scan_floor = floor.isoformat()
        floor = timestamp(state.scan_floor)
        try:
            data = self._request(
                f"{stream.partition}/posts", fields=_POST_FIELDS, limit=limit,
                after=state.posts_after or None,
                since=int(floor.timestamp()) if floor is not None else None,
            )
            after = _after(data, previous=state.posts_after, seen=state.posts_seen)
        except CursorInvalid:
            if state.posts_resets:
                raise IntegrationError("Facebook repeatedly refused the posts cursor; reset this stream.") from None
            state.posts_after, state.posts_seen = "", []
            state.posts_resets = 1
            raise CursorInvalid(cursor=state.dump()) from None
        records: list[ParsedPost | FacebookRecordRefusal] = []
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
            if stream.key == "history" or state.force
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
                limit=limit, after=state.comments_after or None, record_local=True,
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
            after, records = "", [FacebookRecordRefusal(post_id, "repeated_comment_cursor")]
        except SemanticError as error:
            after, records = "", [FacebookRecordRefusal(post_id, error.code)]
        if after:
            state.comments_after = after
            state.comments_seen = [*state.comments_seen, after][-16:]
        else:
            state.comments_after, state.comments_seen = "", []
            state.comments_resets = 0
            state.pending = state.pending[1:]
        return self._page(stream.key, records, state)

    def _parse(self, raw: Any, *, post_id: str, fallback: str) -> ParsedPost | FacebookRecordRefusal:
        try:
            if not isinstance(raw, dict):
                raise ValueError("invalid record")
            if post_id:
                return comment_post(raw, post_id=post_id, page_id=self.bridge.external_id)
            return root_post(raw, page_id=self.bridge.external_id, page_name=self.bridge.display_name)
        except (IntegrationError, ValidationError, KeyError, TypeError, ValueError, AttributeError):
            identity = raw.get("id") if isinstance(raw, dict) else None
            safe_id = identity if isinstance(identity, str) and identity.replace("_", "").isdigit() else fallback
            return FacebookRecordRefusal(safe_id, "invalid_record")

    @staticmethod
    def _page(key: str, records: Sequence[ParsedPost | FacebookRecordRefusal], state: FacebookCursor) -> StreamPage:
        exhausted = state.posts_done and not state.pending
        if exhausted:
            seen = [moment for value in (state.newest_published_at, state.scan_newest)
                    if (moment := timestamp(value)) is not None]
            newest = max(seen, default=None)
            state = FacebookCursor(
                complete=key == "history", newest_published_at=newest.isoformat() if newest is not None else "",
            )
        return StreamPage(records, state.dump(), exhausted=exhausted)

    def record_key(self, record: ParsedPost | FacebookRecordRefusal) -> str:
        """Use resource ids for both landed records and quarantined read failures."""

        return record.external_id if isinstance(record, FacebookRecordRefusal) else super().record_key(record)

    def apply_record(self, stream: Any, record: ParsedPost | FacebookRecordRefusal) -> ApplyResult:
        """Let integrate quarantine a refused record while committing its page."""

        if isinstance(record, FacebookRecordRefusal):
            raise SemanticError(record.code, details={"external_key": record.external_id})
        return super().apply_record(stream, record)

    def finish_page(self, stream: Any, page: StreamPage, outcomes: Sequence[ApplyResult]) -> None:
        """Resolve relations for parsed records through the posts owner."""

        records = tuple(record for record in page.records if isinstance(record, ParsedPost))
        super().finish_page(stream, replace(page, records=records), outcomes)

    def deliver(self, message: Any) -> DeliveryOutcome:
        """Publish a reply, reconciling Page-authored text before any repeat send."""

        if message.channel_id != self.bridge.pk or message.platform != "facebook" or message.direction != "outbound":
            return DeliveryOutcome(accepted=False)
        try:
            parent = message.top_level_comment()
        except ValidationError:
            return DeliveryOutcome(accepted=False)
        text = message.body_text().strip()
        if not text or parent.is_original_post:
            return DeliveryOutcome(accepted=False)
        try:
            existing = self._existing_reply(parent.external_id, text)
            if existing:
                return DeliveryOutcome(accepted=True, provider_id=existing)
            try:
                data = self._request(
                    f"{parent.external_id}/comments", method="POST", form={"message": text}, delivery=True,
                )
                identity = data.get("id")
                if not isinstance(identity, str) or not identity.replace("_", "").isdigit():
                    raise _PublishUncertain("Facebook did not acknowledge the reply identity.")
                return DeliveryOutcome(accepted=True, provider_id=identity)
            except _PublishUncertain:
                # A failed lookup cannot prove absence after the publish. Never
                # turn its throttle or transport failure into a resend request.
                try:
                    existing = self._existing_reply(parent.external_id, text)
                except (IntegrationError, CursorInvalid):
                    existing = None
                if existing:
                    return DeliveryOutcome(accepted=True, provider_id=existing)
                raise IntegrationError("The Facebook reply may have been sent; inspect the Page before sending again.")
        except IntegrationError as error:
            if error.transient:
                raise TransientDeliveryError(error.public_message, retry_after=error.retry_after) from error
            raise

    def _existing_reply(self, parent_id: str, text: str) -> str | None:
        after = ""
        seen: set[str] = set()
        while True:
            try:
                data = self._request(
                    f"{parent_id}/comments", filter="stream", order="reverse_chronological",
                    fields=_REPLY_FIELDS, limit=_GRAPH_PAGE_LIMIT, after=after or None, delivery=True,
                )
            except CursorInvalid:
                raise IntegrationError("Facebook refused the reply lookup cursor.") from None
            for reply in _rows(data):
                if not isinstance(reply, dict) or not isinstance(reply.get("from") or {}, dict):
                    raise IntegrationError("Facebook returned an invalid reply lookup record.")
                if (str((reply.get("from") or {}).get("id") or "") == self.bridge.external_id
                        and str(reply.get("message") or "").strip() == text and reply.get("id")):
                    return str(reply["id"])
            try:
                after = _after(data, previous=after, seen=seen)
            except CursorInvalid:
                raise IntegrationError("Facebook returned an invalid reply lookup cursor.") from None
            if not after:
                return None
            seen.add(after)

    def _request(
        self, path: str, *, method: str = "GET", credential: Any = None,
        form: dict[str, str] | None = None, delivery: bool = False, record_local: bool = False, **params: Any,
    ) -> dict[str, Any]:
        credential = credential or self.bridge.credential
        if credential is None:
            raise IntegrationError("Connect a Facebook Page before making requests.", transient=False)
        credential.ensure_fresh()
        headers = credential.auth_headers()
        now = timezone.now()
        quota = apps.get_model("posts", "Quota").objects
        policy = self.policy
        limit = policy.quota_limit if delivery else policy.quota_limit - policy.reply_reserve
        # The ledger records the full allowance; consume enforces each call's
        # threshold atomically, so polling leaves the reply reserve available.
        period = quota.open_period(integration=self.bridge, limit=policy.quota_limit, now=now)
        if not quota.consume(integration=self.bridge, units=1, limit=limit, now=now):
            raise IntegrationError(
                "The Facebook request budget is exhausted.", transient=True, retry_after=period.period_end - now,
            )
        url = MetaFacebook.graph_url(quote(path, safe="/"))
        query = urlencode({key: value for key, value in (params | MetaFacebook.auth_params(credential)).items()
                           if value is not None})
        if query:
            url += "?" + query
        body = None
        if form is not None:
            body = urlencode(form).encode()
            headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
        try:
            response = self.http.request(method, url, headers=headers, body=body, timeout=10.0, max_bytes=2**21)
        except (httpx2.ConnectError, httpx2.ConnectTimeout) as error:
            raise IntegrationError("Facebook could not be reached.", transient=True) from error
        except (OSError, httpx2.TransportError, ResponseTooLargeError) as error:
            if method == "POST":
                raise _PublishUncertain("Facebook lost the reply acknowledgement.") from error
            raise IntegrationError("Facebook could not be read.", transient=True) from error
        try:
            data = response.json()
        except ValueError as error:
            if method == "POST":
                raise _PublishUncertain("Facebook returned an unreadable reply acknowledgement.") from error
            raise IntegrationError("Facebook returned an unreadable response.", transient=True) from error
        if not isinstance(data, dict):
            if method == "POST":
                raise _PublishUncertain("Facebook returned an unreadable reply acknowledgement.")
            if record_local:
                raise SemanticError("invalid_comment_page")
            raise IntegrationError("Facebook returned an invalid response.")
        refusal = data.get("error") or {}
        if not isinstance(refusal, dict):
            if method == "POST":
                raise _PublishUncertain("Facebook returned an unreadable reply acknowledgement.")
            if record_local:
                raise SemanticError("invalid_comment_page")
            raise IntegrationError("Facebook returned an invalid refusal.")
        code = refusal.get("code")
        if code == 190:
            raise IntegrationError("Facebook authorization was refused; reconnect the Page.", transient=False)
        if (isinstance(code, int) and code in _RATE_CODES) or response.status_code == 429:
            raise IntegrationError(
                "Facebook temporarily limited Page requests.", transient=True, retry_after=_retry_after(response),
            )
        if params.get("after") and code == 100 and "cursor" in str(refusal.get("message") or "").lower():
            raise CursorInvalid()
        if refusal:
            if record_local:
                raise SemanticError("comment_read_refused")
            raise IntegrationError("Facebook refused the request; check Page permissions.")
        if response.status_code >= 500:
            if method == "POST":
                raise _PublishUncertain("Facebook lost the reply acknowledgement.")
            raise IntegrationError("Facebook is temporarily unavailable.", transient=True)
        if response.status_code >= 400:
            if record_local:
                raise SemanticError("comment_read_refused")
            raise IntegrationError("Facebook refused the request; check Page permissions.")
        return data


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


def _retry_after(response: httpx2.Response) -> timedelta:
    """Meta's business usage recovery estimate is expressed in minutes."""

    delays = []
    try:
        usage = json.loads(response.headers.get("X-Business-Use-Case-Usage", "{}"))
        for entries in usage.values():
            for entry in entries:
                minutes = float(entry.get("estimated_time_to_regain_access") or 0)
                if minutes > 0 and isfinite(minutes):
                    delays.append(minutes * 60)
    except (TypeError, ValueError, AttributeError):
        pass
    try:
        seconds = float(response.headers.get("Retry-After", "0"))
        if seconds > 0 and isfinite(seconds):
            delays.append(seconds)
    except ValueError:
        pass
    return timedelta(seconds=max(delays, default=900))
