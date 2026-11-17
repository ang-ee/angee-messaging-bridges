"""Compatibility key for OAuth clients the YouTube addon created before ``integrate_google``."""

from angee.integrate_google.providers import GoogleType


class GoogleYouTube(GoogleType):
    """Load rows still holding the retired ``google_youtube`` provider key as Google.

    The ``move_youtube_client_to_google`` migration template moves those rows to
    ``provider_type = google``. Remove this class and its autoconfig key once no
    supported deployment has an OAuth client holding ``google_youtube``.
    """

    key = "google_youtube"
    label = "Google (retired YouTube key)"
    retired = True
