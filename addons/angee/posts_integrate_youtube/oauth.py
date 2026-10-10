"""Google OAuth defaults contributed by the YouTube feed addon."""

from angee.integrate.oauth.providers import OAuthProviderType


class GoogleYouTube(OAuthProviderType):
    """Request a refreshable grant for reading comments and publishing replies."""

    key = "google_youtube"
    label = "Google YouTube"
    icon = "youtube"
    _scopes = ["openid", "email", "profile", "https://www.googleapis.com/auth/youtube.force-ssl"]
    defaults = {
        "authorize_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_endpoint": "https://oauth2.googleapis.com/token",
        "userinfo_endpoint": "https://openidconnect.googleapis.com/v1/userinfo",
        "revoke_endpoint": "https://oauth2.googleapis.com/revoke",
        "supports_pkce": True,
        "supports_refresh": True,
        "default_scopes": _scopes,
        "scopes_catalogue": _scopes,
        "authorize_params": {"access_type": "offline", "prompt": "consent"},
        "external_id_claim": "sub",
        "email_claim": "email",
        "display_name_claim": "name",
        "avatar_url_claim": "picture",
    }
