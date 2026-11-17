"""Register the YouTube feed adapter; Google OAuth comes from integrate_google."""

SETTINGS = {
    "ANGEE_POSTS_FEED_BACKEND_CLASSES.youtube": "angee.posts_integrate_youtube.backend.YouTubeFeedBackend",
    # Retired key kept loadable until the move_youtube_client_to_google step ran everywhere.
    "ANGEE_OAUTH_PROVIDER_TYPE_CLASSES.google_youtube": "angee.posts_integrate_youtube.oauth.GoogleYouTube",
}
