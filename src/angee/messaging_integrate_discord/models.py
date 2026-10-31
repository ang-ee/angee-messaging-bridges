"""Discord account profile and committed REST frontier contributions."""

from __future__ import annotations

from collections.abc import Mapping

from django_compose.models import Composable


class DiscordChannelState(Composable, extends="messaging.Channel"):
    """Persist Discord-specific account facts on the existing Channel row."""

    class Meta:
        abstract = True

    @property
    def discord_history_seeded(self) -> bool:
        """Whether the initial contiguous REST history completed."""
        return bool(self.subscription_state.get("history_seeded"))

    @property
    def discord_watermarks(self) -> dict[str, str]:
        """Committed snowflake frontiers, distinct from live Gateway arrivals."""
        raw = self.subscription_state.get("channel_watermarks")
        return {str(key): str(value) for key, value in raw.items()} if isinstance(raw, Mapping) else {}

    def advance_discord_watermark(self, channel_id: str, watermark: str) -> None:
        """Advance a REST frontier only after its preceding messages committed."""
        if not watermark.isdigit():
            raise ValueError("A Discord history watermark must be a numeric snowflake.")
        updated = self.discord_watermarks
        previous = updated.get(channel_id, "")
        if not previous.isdigit() or int(watermark) > int(previous):
            updated[channel_id] = watermark
            self.merge_subscription_state(channel_watermarks=updated)

    def remember_discord_profile(self, own_id: str, *, username: str) -> None:
        """Accept mutable profile facts only for this row's claimed bot identity."""
        if str(own_id or "").strip() != str(self.subscription_state.get("own_id") or "").strip():
            return
        self.merge_subscription_state(username=str(username or "").strip())
