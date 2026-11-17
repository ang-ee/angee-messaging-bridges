"""Register the YouTube feed adapter; Google OAuth comes from integrate_google."""

SETTINGS = {
    "ANGEE_POSTS_FEED_BACKEND_CLASSES.youtube": "angee.posts_integrate_youtube.backend.YouTubeFeedBackend",
}
