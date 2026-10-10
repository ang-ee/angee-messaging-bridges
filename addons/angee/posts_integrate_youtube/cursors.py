"""Validated, payload-free positions for YouTube's independent polling streams."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

Identity = Annotated[str, Field(min_length=1, max_length=512, strict=True)]
"""A provider identity, never a comment body or an author display name."""


class PagePosition(BaseModel):
    """Empty token starts a page walk; None means the walk finished.

    One reset is allowed per walk. A second invalid or looping token quarantines
    that walk instead of perpetually replaying the same inaccessible partition.
    """

    model_config = ConfigDict(extra="forbid")
    token: str | None = ""
    seen: list[str] = Field(default_factory=list, max_length=16)
    resets: int = Field(default=0, ge=0, le=1, strict=True)


class ThreadIdentity(BaseModel):
    """Only the identities needed to re-read a deferred thread page."""

    model_config = ConfigDict(extra="forbid")
    thread_id: Identity
    video_id: Identity
    moderation: Literal["published", "heldForReview", "likelySpam"] = "published"


class ReplyPosition(BaseModel):
    """A parent's resumable replies walk, retained until its final page lands."""

    model_config = ConfigDict(extra="forbid")
    parent_id: Identity
    video_id: Identity
    hidden: bool = Field(default=False, strict=True)
    page: PagePosition = Field(default_factory=PagePosition)


class StreamCursor(BaseModel):
    """Shared identity queues; integrate commits the document with its records."""

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    force: bool = Field(default=False, strict=True)
    pending: list[ThreadIdentity] = Field(default_factory=list, max_length=50)
    replies: list[ReplyPosition] = Field(default_factory=list, max_length=100)


class ActivityCursor(StreamCursor):
    """Keep the completed watermark separate from the in-progress scan's maximum."""

    stage: Literal["published", "heldForReview", "likelySpam", "threads", "comments", "idle"] = "published"
    page: PagePosition = Field(default_factory=PagePosition)
    newest_published_at: AwareDatetime | None = None
    cycle_newest: AwareDatetime | None = None
    floor: AwareDatetime | None = None
    recheck_after: AwareDatetime | None = None
    active_after: int = Field(default=0, ge=0, strict=True)

    def begin(self, *, floor: datetime) -> None:
        """Start the next poll without discarding its completed watermark."""

        self.stage = "published"
        self.page = PagePosition()
        self.floor = self.newest_published_at or floor
        self.cycle_newest = self.newest_published_at
        self.active_after = 0


class HistoryCursor(StreamCursor):
    """Uploads paging survives a reset of one video's threads or one reply walk."""

    stage: Literal["uploads", "videos", "threads", "complete"] = "uploads"
    uploads: PagePosition = Field(default_factory=PagePosition)
    root_ids: list[Identity] = Field(default_factory=list, max_length=50)
    videos: list[Identity] = Field(default_factory=list, max_length=50)
    threads: PagePosition = Field(default_factory=PagePosition)
    moderation: Literal["published", "heldForReview", "likelySpam"] = "published"
