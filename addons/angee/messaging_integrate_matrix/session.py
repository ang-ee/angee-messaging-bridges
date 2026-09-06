"""Live Matrix user session — matrix-nio protocol and vodozemac crypto, worker-only.

nio owns the Olm account, the peewee SQLite crypto store, sync decryption, and
authenticated media download; :mod:`angee.messaging_integrate_matrix.recovery`
ports the one round nio lacks — turning a pasted recovery key into a cross-signed,
self-verified device. The operator-facing shape is unchanged from the libolm
bridge this replaces: password login, optional recovery key, sync, a bounded
history seed, then live ingest, with encrypted rooms and attachments decrypted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

import nio

from angee.integrate.session import PASSWORD_SKIPPED
from angee.messaging.session import (
    API_TIMEOUT_SECONDS,
    INGEST_CHUNK,
    INITIAL_CONVERSATION_LIMIT,
    INITIAL_HISTORY_LIMIT,
    INITIAL_HISTORY_TIMEOUT_SECONDS,
    AsyncioLiveSession,
)
from angee.messaging_integrate_matrix import recovery
from angee.messaging_integrate_matrix.connect import matrix_login
from angee.messaging_integrate_matrix.identity import MatrixMediaFact, parsed_message

logger = logging.getLogger(__name__)

SYNC_TIMEOUT_MILLISECONDS = 30_000

_SESSION_FILE = "session.json"
_CRYPTO_STORE_FILE = "crypto.db"
_SYNC_FILTER = {
    "room": {
        "state": {"lazy_load_members": True},
        "timeline": {"lazy_load_members": True},
    }
}
_RECOVERY_PROMPT = (
    "Enter the Matrix recovery key to establish cross-signing and trust this device, "
    "or skip to continue with forward decryption. Earlier history is best-effort through "
    "to-device key sharing after verification."
)


class MatrixApiError(RuntimeError):
    """One Matrix client-server error carrying its stable ``errcode`` and message."""

    def __init__(self, errcode: str, message: str) -> None:
        self.errcode = str(errcode or "").upper()
        self.message = str(message or "")
        super().__init__(f"{self.errcode}: {self.message}" if self.errcode else self.message)


class MatrixSession(AsyncioLiveSession):
    """One Matrix account whose asyncio loop belongs to its connection thread."""

    session_file_name = _SESSION_FILE
    client_class: Any = nio.AsyncClient
    config_class: Any = nio.AsyncClientConfig
    decrypt_attachment: Any = staticmethod(nio.crypto.attachments.decrypt_attachment)
    verify_with_recovery_key: Any = staticmethod(recovery.verify_with_recovery_key)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._session_path: Path | None = None
        self._session_facts: dict[str, Any] = {}
        self._login_material: tuple[str, str] | None = None
        self._undecryptable_events = 0

    def _build_client(self, session_path: Path) -> Any:
        """Build the nio client against the retained login and vodozemac store."""

        self._session_path = session_path
        self._session_facts = _read_session_facts(session_path)
        if not self._session_facts.get("access_token"):
            credential = self._fresh_credential()
            if credential is None:
                raise ValueError("This Matrix channel has no basic-auth credential.")
            self._login_material = matrix_login(credential)
        homeserver = str(self.bridge.subscription_state.get("homeserver") or "").strip()
        if not homeserver:
            raise ValueError("This Matrix channel has no homeserver URL.")
        pickle_key = str(self._session_facts.get("pickle_key") or secrets.token_urlsafe(32))
        self._session_facts["pickle_key"] = pickle_key
        self._persist_session_facts()
        return self.client_class(
            homeserver,
            user=str(self._session_facts.get("user_id") or "") or self._login_username(),
            device_id=str(self._session_facts.get("device_id") or ""),
            store_path=str(session_path.parent),
            config=self.config_class(
                encryption_enabled=True,
                pickle_key=pickle_key,
                store_name=_CRYPTO_STORE_FILE,
                store_sync_tokens=False,
                max_timeouts=3,
                max_limit_exceeded=5,
            ),
        )

    def _login_username(self) -> str:
        """Return the login identifier prepared for a first password sign-in."""

        return self._login_material[0] if self._login_material is not None else ""

    async def _run_client(self) -> None:
        """Authenticate, upload device keys, recover, then sync.

        The first sync pass performs the bounded initial backfill (``_sync_loop``
        → ``_initial_history``); backfill is not a separate step before sync.
        """

        try:
            own_id = await self._authenticate()
        except MatrixApiError as error:
            if error.errcode == "M_FORBIDDEN":
                self.events.put(("logged_out", None))
                return
            raise
        if self._vendor_stopping():
            return
        if self.client.should_upload_keys:
            self._raise_for(await self._bounded(self.client.keys_upload()))
        if not await self._recover_keys():
            return
        self.events.put(("paired", own_id))
        await self._sync_loop()

    async def _authenticate(self) -> str:
        """Restore a retained token, or perform one m.login.password sign-in.

        nio loads the vodozemac store from the login or restore call, so the Olm
        account and peewee store exist before any key upload or sync.
        """

        facts = self._session_facts
        token = str(facts.get("access_token") or "")
        if token:
            user_id = str(facts.get("user_id") or "")
            device_id = str(facts.get("device_id") or "")
            if not user_id or not device_id:
                raise RuntimeError("Matrix login returned incomplete session facts.")
            self.client.restore_login(user_id, device_id, token)
            whoami = await self._bounded(self.client.whoami())
            self._raise_for(whoami)
            own_id = str(whoami.user_id)
            device_id = str(whoami.device_id or device_id)
            access_token = token
        else:
            if self._login_material is None:
                raise RuntimeError("The Matrix login material was not prepared.")
            _username, password = self._login_material
            login = await self._bounded(self.client.login(password=password, device_name="Angee"))
            self._raise_for(login)
            own_id = str(login.user_id)
            device_id = str(login.device_id)
            access_token = str(login.access_token)
        if not own_id or not device_id or not access_token:
            raise RuntimeError("Matrix login returned incomplete session facts.")
        facts.update(user_id=own_id, device_id=device_id, access_token=access_token)
        self._persist_session_facts()
        return own_id

    def _raise_for(self, response: Any) -> None:
        """Raise :class:`MatrixApiError` for any nio error response."""

        if isinstance(response, nio.ErrorResponse):
            raise MatrixApiError(str(response.status_code or ""), str(response.message or ""))

    async def _recover_keys(self) -> bool:
        """Run the optional recovery-key round once for this retained store."""

        if self._session_facts.get("recovery") in {"verified", "skipped"}:
            return True
        recovery_key = await self.request_password_async(
            _RECOVERY_PROMPT,
            material_key="recovery_key",
            optional=True,
        )
        if recovery_key is None:
            return False
        if recovery_key is PASSWORD_SKIPPED:
            self._session_facts["recovery"] = "skipped"
        else:
            # A wrong key raises RecoveryError and crashes the session exactly as a
            # bad key did before; the recovery module owns SSSS import, cross-signing
            # self-verification, and the signature upload.
            await self._bounded(
                self.verify_with_recovery_key(
                    _MatrixSecretsApi(self.client),
                    user_id=self.client.user_id,
                    device_id=self.client.device_id,
                    recovery_key=recovery_key,
                )
            )
            self._session_facts["recovery"] = "verified"
        self._persist_session_facts()
        return True

    async def _sync_loop(self) -> None:
        """Process sync responses with a self-persisted token and lazy member filter.

        nio's ``store_sync_tokens`` is off on purpose: the token watermark must not
        advance before the ingest acknowledgement, so ``next_batch`` is persisted in
        ``session.json`` only after ``_checkpoint_sync_batch`` confirms the landing.
        """

        since = str(self._session_facts.get("next_batch") or "") or None
        first = True
        while not self._vendor_stopping():
            response = await self.client.sync(
                since=since,
                timeout=0 if first else SYNC_TIMEOUT_MILLISECONDS,
                sync_filter=_SYNC_FILTER,
                set_presence="offline",
            )
            self._raise_for(response)
            if self.client.should_upload_keys:
                self._raise_for(await self._bounded(self.client.keys_upload()))
            if self.client.should_query_keys:
                self._raise_for(await self._bounded(self.client.keys_query()))
            batch = self._queued_sync(response)
            if batch:
                self.events.put(("messages", batch))
            if first and not self.bridge.subscription_state.get("history_seeded"):
                if await self._initial_history(response):
                    self.events.put(("history_seeded", None))
            next_batch = str(response.next_batch or "")
            if next_batch:
                if not await self._checkpoint_sync_batch(next_batch):
                    return
                since = next_batch
            first = False
            if self._vendor_stopping():
                return
            await asyncio.sleep(0)

    def _queued_sync(self, sync: Any) -> list[tuple[Any, Any]]:
        """Queue every joined-room timeline event from one sync in room order."""

        batch: list[tuple[Any, Any]] = []
        for room_id, room in _joined_rooms(sync):
            for event in room.timeline.events:
                queued = self._queued_event(event, room_id=room_id)
                if queued is not None:
                    batch.append(queued)
        return batch

    async def _initial_history(self, sync: Any) -> bool:
        """Queue at most 100 older events, capped at 20 per joined room."""

        remaining = INITIAL_HISTORY_LIMIT
        batch: list[tuple[Any, Any]] = []
        completed = True
        try:
            async with asyncio.timeout(INITIAL_HISTORY_TIMEOUT_SECONDS):
                for room_id, room in _joined_rooms(sync):
                    if remaining <= 0 or self._vendor_stopping():
                        break
                    start = str(room.timeline.prev_batch or "") or None
                    limit = min(INITIAL_CONVERSATION_LIMIT, remaining)
                    page = await self._bounded(
                        self.client.room_messages(
                            room_id,
                            start=start,
                            direction=nio.MessageDirection.back,
                            limit=limit,
                            message_filter={"lazy_load_members": True},
                        )
                    )
                    self._raise_for(page)
                    for event in reversed(page.chunk):
                        queued = self._queued_event(event, room_id=room_id)
                        if queued is None:
                            continue
                        batch.append(queued)
                        remaining -= 1
                        if len(batch) == INGEST_CHUNK:
                            self.events.put(("messages", batch))
                            batch = []
                        if remaining <= 0:
                            break
        except TimeoutError:
            completed = False
            logger.info("Matrix initial history for channel %s reached its bound.", self.bridge.sqid)
        if batch:
            self.events.put(("messages", batch))
        return completed and not self._vendor_stopping()

    def _queued_event(self, event: Any, *, room_id: str) -> tuple[Any, Any] | None:
        """Return one neutral message plus the raw event used for media download.

        nio decrypts Megolm events in place where it holds keys; an undecryptable
        one stays a :class:`nio.MegolmEvent` and is counted and skipped.
        """

        if isinstance(event, nio.MegolmEvent):
            self._undecryptable_events += 1
            logger.info(
                "Skipping undecryptable Matrix event for channel %s (total %s).",
                self.bridge.sqid,
                self._undecryptable_events,
            )
            return None
        if isinstance(event, (nio.BadEvent, nio.UnknownBadEvent)):
            return None
        wire = dict(event.source)
        wire["room_id"] = room_id
        title = ""
        room = self.client.rooms.get(room_id)
        if room is not None:
            sender = str(wire.get("sender") or "")
            member = room.users.get(sender) if sender else None
            display_name = getattr(member, "display_name", "") if member is not None else ""
            if display_name:
                wire["sender_display_name"] = display_name
            title = room.display_name or ""
        parsed = parsed_message(
            wire,
            room_id=room_id,
            room_name=title,
            own_user_id=self.client.user_id,
        )
        return (parsed, wire) if parsed is not None else None

    def _handle(self, kind: str, payload: Any) -> bool:
        """Persist the acknowledged sync token; delegate other events."""

        if kind == "sync_checkpoint":
            next_batch, persisted, done = payload
            try:
                self._run_coro_threadsafe(
                    self._put_next_batch(next_batch),
                    API_TIMEOUT_SECONDS,
                )
                persisted.set()
            except Exception as error:
                self.outcome_error = error
                logger.exception("Matrix sync checkpoint failed for channel %s.", self.bridge.sqid)
                return False
            finally:
                done.set()
            return self._still_wanted()
        return super()._handle(kind, payload)

    async def _download_coro(self, _payload: Any, fact: MatrixMediaFact) -> bytes | None:
        """Download authenticated Matrix media and decrypt encrypted attachments."""

        response = await self.client.download(mxc=fact.url)
        self._raise_for(response)
        raw = bytes(response.body)
        if fact.encrypted:
            raw = self.decrypt_attachment(raw, fact.key, fact.hash, fact.iv)
        return raw

    async def _put_next_batch(self, next_batch: str) -> None:
        """Persist one task-thread-acknowledged sync token in ``session.json``."""

        self._session_facts["next_batch"] = next_batch
        self._persist_session_facts()

    async def _checkpoint_sync_batch(self, next_batch: str) -> bool:
        """Wait until queued messages land before advancing the sync watermark."""

        persisted = threading.Event()
        done = threading.Event()
        self.events.put(("sync_checkpoint", (next_batch, persisted, done)))
        while not done.is_set():
            if self._vendor_stopping():
                return False
            await asyncio.sleep(0.01)
        return persisted.is_set()

    async def _close_client(self) -> None:
        """Close nio's aiohttp session and its owning store on their loop."""

        if self.client is not None:
            await self.client.close()

    def _is_logged_out(self, error: Exception) -> bool:
        """Classify Matrix token errors that prove retained auth is unusable."""

        return isinstance(error, MatrixApiError) and error.errcode in {"M_MISSING_TOKEN", "M_UNKNOWN_TOKEN"}

    def _teardown_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Close the Matrix client and cancel remaining loop tasks."""

        try:
            loop.run_until_complete(self._close_client())
        except Exception:
            logger.info("Closing Matrix channel %s did not finish cleanly.", self.bridge.sqid)
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        asyncio.set_event_loop(None)

    def _stop_main(self, loop: asyncio.AbstractEventLoop, *, deadline: float) -> None:
        """Cancel Matrix sync without blocking beyond its fire-and-forget handoff."""

        del deadline
        task = self._main_task
        if task is not None:
            loop.call_soon_threadsafe(task.cancel)

    def _persist_session_facts(self) -> None:
        """Atomically persist Matrix login and sync facts beside the nio store."""

        if self._session_path is None:
            raise RuntimeError("The Matrix session path is unavailable.")
        _write_session_facts(self._session_path, self._session_facts)


class _MatrixSecretsApi:
    """Adapt nio's raw transport to the recovery module's Secret Storage contract."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def get_account_data(self, event_type: str) -> Mapping[str, Any]:
        """Read one of this account's global account-data events."""

        user_id = quote(str(self._client.user_id), safe="")
        path = f"/_matrix/client/v3/user/{user_id}/account_data/{quote(event_type, safe='')}"
        return await self._json("GET", path, None)

    async def query_keys(self, user_id: str) -> Mapping[str, Any]:
        """Return the account's published device and cross-signing keys."""

        return await self._json("POST", "/_matrix/client/v3/keys/query", {"device_keys": {user_id: []}})

    async def upload_signatures(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Upload cross-signing signatures and return the server response body."""

        return await self._json("POST", "/_matrix/client/v3/keys/signatures/upload", dict(payload))

    async def _json(self, method: str, path: str, body: Mapping[str, Any] | None) -> dict[str, Any]:
        """Send one raw client-server request and parse its JSON body."""

        response = await self._client.send(
            method,
            path,
            data=json.dumps(dict(body)) if body is not None else None,
            headers={
                "Authorization": f"Bearer {self._client.access_token}",
                "Content-Type": "application/json",
            },
        )
        async with response:
            try:
                # A proxy or gateway error page is not JSON; keep the status as the fact.
                parsed = await response.json(content_type=None)
            except ValueError:
                parsed = None
            if response.status >= 400:
                errcode = str(parsed.get("errcode") or "") if isinstance(parsed, Mapping) else ""
                message = str(parsed.get("error") or "") if isinstance(parsed, Mapping) else f"HTTP {response.status}"
                raise MatrixApiError(errcode, message)
        return dict(parsed) if isinstance(parsed, Mapping) else {}


def _joined_rooms(sync: Any) -> tuple[tuple[str, Any], ...]:
    """Return joined ``(room_id, room)`` pairs in deterministic room-id order."""

    joined = sync.rooms.join
    return tuple((str(room_id), joined[room_id]) for room_id in sorted(joined, key=str))


def _read_session_facts(path: Path) -> dict[str, Any]:
    """Read the retained login/sync envelope, treating a missing file as new."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as error:
        raise ValueError(f"The Matrix session file {path} is invalid.") from error
    if not isinstance(value, dict):
        raise ValueError(f"The Matrix session file {path} must contain an object.")
    return value


def _write_session_facts(path: Path, facts: Mapping[str, Any]) -> None:
    """Write one private, replace-safe login/sync envelope."""

    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(dict(facts), sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(path)
