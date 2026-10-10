"""Map Graph Page objects onto the shared public-message envelopes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from angee.integrate.errors import IntegrationError
from angee.messaging.backends import ParsedHandle, ParsedMessage, ParsedPart, ParsedThread
from angee.posts.backends import ParsedMetrics, ParsedPost


def timestamp(value: str | None) -> datetime | None:
    """Accept timezone-aware Graph dates without substituting the polling time."""

    try:
        parsed = parse_datetime(value) if value else None
    except (TypeError, ValueError):
        parsed = None
    return parsed if parsed is not None and timezone.is_aware(parsed) else None


def root_post(raw: dict[str, Any], *, page_id: str, page_name: str) -> ParsedPost:
    """Represent a Page's own post as the root of one public thread."""

    identity = _identity(raw)
    body = str(raw.get("message") or raw.get("story") or "")
    return ParsedPost(
        message=ParsedMessage(
            external_id=identity,
            platform="facebook",
            direction="outbound",
            sender=ParsedHandle("facebook", page_id, page_name, page_id),
            sent_at=timestamp(raw.get("created_time")),
            subject=body[:280],
            body=ParsedPart(text=body),
            thread=ParsedThread(identity, modality="public_thread", visibility="public", title=body[:280]),
        ),
        is_original_post=True,
        subject_url=str(raw.get("permalink_url") or ""),
        metrics=ParsedMetrics(reply_count=int(raw["comments"]["summary"]["total_count"])),
    )


def comment_post(raw: dict[str, Any], *, post_id: str, page_id: str) -> ParsedPost:
    """Keep reply parents; unavailable authors remain unattributed."""

    identity = _identity(raw)
    author = raw.get("from") or {}
    author_id = str(author.get("id") or "")
    return ParsedPost(
        message=ParsedMessage(
            external_id=identity,
            platform="facebook",
            direction="outbound" if author_id and author_id == page_id else "inbound",
            sender=ParsedHandle(
                "facebook",
                author_id,
                str(author.get("name") or ""),
                author_id,
            ) if author_id else None,
            sent_at=timestamp(raw.get("created_time")),
            body=ParsedPart(text=str(raw.get("message") or "")),
            thread=ParsedThread(post_id, modality="public_thread", visibility="public"),
            in_reply_to=str((raw.get("parent") or {}).get("id") or post_id),
            metadata={"facebook": {"post_id": post_id}},
        ),
        hidden=bool(raw.get("is_hidden")),
        metrics=ParsedMetrics(
            like_count=int(raw.get("like_count") or 0),
            reply_count=int(raw.get("comment_count") or 0),
        ),
    )


def _identity(raw: dict[str, Any]) -> str:
    identity = raw.get("id")
    if not isinstance(identity, str) or not identity or not identity.replace("_", "").isdigit():
        raise IntegrationError("Facebook returned a record without an identity.")
    return identity
