"""Console-safe Matrix channel backend declaration."""

from __future__ import annotations

from typing import Any

from angee.integrate.credentials import CredentialKind
from angee.messaging.backends import LiveChannelBackend, ParsedMessage
from angee.messaging_integrate_matrix.constants import SESSION_QUEUE


class MatrixChannelBackend(LiveChannelBackend):
    """Live bridge for one user's own Matrix account."""

    key = "matrix"
    label = "Matrix"
    icon = "message-circle"
    session_queue = SESSION_QUEUE
    session_class = "angee.messaging_integrate_matrix.session.MatrixSession"
    transient_material_keys = ("recovery_key",)

    def normalize_account_id(self, raw: str) -> str:
        """Return the stable Matrix user id without surrounding whitespace."""

        return str(raw or "").strip()

    def account_label(self, own_id: str) -> str:
        """Use the stable Matrix user id as the account label."""

        return self.normalize_account_id(own_id)

    def parse_live_message(self, message: ParsedMessage) -> ParsedMessage:
        """Pass the Matrix boundary's already-neutral message through."""

        return message


def matrix_login(credential: Any) -> tuple[str, str]:
    """Return the Matrix ``(username, password)`` owned by one BASIC_AUTH row."""

    if credential.kind != CredentialKind.BASIC_AUTH:
        raise ValueError("A Matrix channel requires a basic-auth credential.")
    material = credential.reveal()
    username = str(material.get("username") or "").strip()
    password = str(material.get("password") or "")
    if not username or not password:
        raise ValueError("The Matrix credential requires a username and password.")
    return username, password
