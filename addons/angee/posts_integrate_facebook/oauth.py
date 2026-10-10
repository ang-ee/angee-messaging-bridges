"""Meta's Facebook Pages preset contributed to integrate's OAuth registry."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from angee.integrate.oauth.client import OAuthClientProtocol
from angee.integrate.oauth.providers import OAuthProviderType


class MetaFacebook(OAuthProviderType):
    """Supply Page permissions and refine the grant before OAuth stores it."""

    key = "meta_facebook"
    label = "Facebook Pages"
    icon = "facebook"
    graph_api_version = "v23.0"
    """The single Graph API version used by authorization and Page requests."""

    _scopes = ["pages_show_list", "pages_read_engagement", "pages_read_user_content", "pages_manage_engagement"]
    defaults = {
        "authorize_endpoint": f"https://www.facebook.com/{graph_api_version}/dialog/oauth",
        "token_endpoint": f"https://graph.facebook.com/{graph_api_version}/oauth/access_token",
        "userinfo_endpoint": f"https://graph.facebook.com/{graph_api_version}/me",
        "supports_pkce": False,
        "supports_refresh": False,
        "default_scopes": _scopes,
        "scopes_catalogue": _scopes,
        "external_id_claim": "id",
        "email_claim": "",
        "display_name_claim": "name",
        "avatar_url_claim": "",
    }

    @classmethod
    def graph_url(cls, path: str) -> str:
        """Resolve a Graph resource against the preset's versioned origin."""

        return f"https://graph.facebook.com/{cls.graph_api_version}/{path}"

    @classmethod
    def refine_grant(cls, protocol: OAuthClientProtocol, tokens: dict[str, Any]) -> dict[str, Any]:
        """Refine a user grant only through integrate's shared OAuth transport."""

        proof = cls.appsecret_proof(tokens["access_token"], protocol.oauth_client)
        grant = {"fb_exchange_token": tokens["access_token"]}
        if proof:
            grant["appsecret_proof"] = proof
        extended = protocol.exchange_grant("fb_exchange_token", **grant)
        # Expiry belongs to the replacement grant, including when it is absent.
        return {key: value for key, value in tokens.items() if key not in {"expires_in", "expires_at"}} | extended

    @classmethod
    def userinfo_params(cls, protocol: OAuthClientProtocol, access_token: str) -> dict[str, str]:
        """Sign the profile request with the refined grant, like every Graph call."""

        proof = cls.appsecret_proof(access_token, protocol.oauth_client)
        return {"appsecret_proof": proof} if proof else {}

    @staticmethod
    def appsecret_proof(token: str, client: Any) -> str:
        """Sign the current token when the app has a confidential registration."""

        secret = str(client.client_secret or "") if client is not None else ""
        return hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest() if secret else ""

    @classmethod
    def auth_params(cls, credential: Any) -> dict[str, str]:
        """Use credential-owned material for both user and derived Page proofs."""

        proof = credential.reveal().get("appsecret_proof") or cls.appsecret_proof(
            credential.secret_value(), credential.oauth_client,
        )
        return {"appsecret_proof": proof} if proof else {}
