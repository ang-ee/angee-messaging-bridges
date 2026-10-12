"""Register the feed adapter and Google YouTube OAuth preset."""

SETTINGS = {
    "ANGEE_POSTS_FEED_BACKEND_CLASSES.youtube": "angee.posts_integrate_youtube.backend.YouTubeFeedBackend",
    "ANGEE_OAUTH_PROVIDER_TYPE_CLASSES.google_youtube": "angee.posts_integrate_youtube.oauth.GoogleYouTube",
}
