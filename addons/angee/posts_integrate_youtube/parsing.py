"""Project provider resources onto the current message core and posts overlay."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from pydantic import AwareDatetime, TypeAdapter, ValidationError

from angee.messaging.backends import ParsedHandle, ParsedMessage, ParsedPart, ParsedThread
from angee.posts.backends import ParsedMetrics, ParsedPost

_TIME = TypeAdapter(AwareDatetime)
_HIDDEN = frozenset({"heldForReview", "likelySpam", "rejected"})


def timestamp(value: Any) -> datetime | None:
    """Unknown publication times remain unknown for Feed.live_since classification."""

    if not isinstance(value, str):
        return None
    try:
        return _TIME.validate_python(value)
    except ValidationError:
        return None


def _core(resource: dict[str, Any], *, video_id: str, channel_id: str, original: bool) -> ParsedMessage:
    """Use one provider projection for the shared public-thread message fields."""

    identity, snippet = resource["id"], resource["snippet"]
    if not isinstance(identity, str) or not identity or len(identity) > 512 or not isinstance(snippet, dict):
        raise ValueError("unusable_resource")
    author = channel_id if original else (snippet.get("authorChannelId") or {}).get("value") or ""
    name = snippet.get("channelTitle" if original else "authorDisplayName") or ""
    title = snippet.get("title", "") if original else ""
    body = snippet.get("description", "") if original else snippet.get("textOriginal", snippet.get("textDisplay", ""))
    if not all(isinstance(value, str) for value in (body, title, author, name)):
        raise ValueError("unusable_text")
    return ParsedMessage(
        external_id=identity, platform="youtube", direction="outbound" if author == channel_id else "inbound",
        sender=ParsedHandle("youtube", author, name, author) if author else None,
        subject=title, sent_at=timestamp(snippet.get("publishedAt")), body=ParsedPart(text=body),
        thread=ParsedThread(external_id=video_id, title=title, modality="public_thread", visibility="public"),
        in_reply_to="" if original else snippet.get("parentId") or video_id,
    )


def video_post(resource: dict[str, Any], *, channel_id: str) -> ParsedPost:
    """Video identity is both root and thread identity; SEO keywords are not hashtags."""

    core = _core(resource, video_id=resource["id"], channel_id=channel_id, original=True)
    counters = resource.get("statistics") or {}
    return ParsedPost(
        message=core, is_original_post=True,
        subject_url="https://www.youtube.com/watch?" + urlencode({"v": core.external_id}),
        metrics=ParsedMetrics(metadata={"comment_count_known": "commentCount" in counters}, **{
            target: int(counters.get(source, 0))
            for source, target in (
                ("viewCount", "view_count"), ("likeCount", "like_count"), ("commentCount", "reply_count"),
            )
        }),
    )


def comment_post(
    resource: dict[str, Any], *, video_id: str, channel_id: str, thread_id: str = "",
    reply_count: int = 0, moderation_status: str = "published", hidden_parent: bool = False,
) -> ParsedPost:
    """Owner-filtered moderation is authoritative even when an id read omits it.

    Metadata retains only the thread lookup identity and provider update time.
    Names and text already belong to messaging's sender and body fields.
    """

    core = _core(resource, video_id=video_id, channel_id=channel_id, original=False)
    snippet = resource["snippet"]
    status = snippet.get("moderationStatus") or moderation_status
    updated_at = timestamp(snippet.get("updatedAt"))
    core.metadata["youtube"] = {
        "video_id": video_id, "thread_id": thread_id, "updated_at": updated_at.isoformat() if updated_at else "",
    }
    return ParsedPost(
        message=core, metrics=ParsedMetrics(like_count=int(snippet.get("likeCount", 0)), reply_count=reply_count),
        hidden=hidden_parent or status in _HIDDEN,
    )
