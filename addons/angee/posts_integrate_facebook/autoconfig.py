"""Contribute the Facebook Page backend to the posts registry."""

SETTINGS = {
    "ANGEE_POSTS_FEED_BACKEND_CLASSES.facebook": (
        "angee.posts_integrate_facebook.backend.FacebookFeedBackend"
    ),
    "ANGEE_OAUTH_PROVIDER_TYPE_CLASSES.meta_facebook": "angee.posts_integrate_facebook.oauth.MetaFacebook",
}
