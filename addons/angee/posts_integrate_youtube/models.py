"""Provider binding facts folded onto the posts Feed by the composer."""

from django.db import models


class YouTubeFeedState(models.Model):
    """Discovery alone writes this playlist identity; it is excluded from edit forms."""

    extends = "posts.Feed"
    youtube_uploads_playlist_id = models.CharField(max_length=512, blank=True, default="", editable=False)

    class Meta:
        """Same-row extension, with no additional resource or table."""

        abstract = True
