"""Real speaker names from the local Discord client (RPC over IPC).

The Discord desktop app serves a local RPC API on the named pipe
``\\\\.\\pipe\\discord-ipc-N`` (N = 0..9). We connect as the user's own Discord
application (client id + secret from the Developer Portal), get a one-time approval
in Discord (AUTHORIZE -> OAuth2 code -> token, cached and refreshed), follow whichever
voice channel the user is in, and turn SPEAKING_START / SPEAKING_STOP into
``SpeakingEvent``s carrying display names. ``SpeakingTimeline`` keeps them as
intervals so a caption can ask "who was talking between t0 and t1?".

Layers, bottom-up, so the protocol logic is testable without Discord:

- ``encode_frame`` / ``FrameDecoder``: IPC framing, ``<op u32 LE><length u32 LE><JSON>``.
- ``Transport`` (whole frames in/out): ``PipeTransport`` is the real Windows pipe.
- ``DiscordSpeakingClient``: handshake, auth + token cache, channel following, events.

Tested: tests/test_discord_rpc.py runs the client against an in-memory fake Discord
(handshake, AUTHORIZE with a mocked token exchange, cached / refreshed / rejected
tokens, membership from voice states, channel switches, reconnects, fatal config
errors) and PipeTransport against a local named-pipe server. Against the real Discord
client only a handshake with a dummy client id was tried: Discord answers with a CLOSE
frame ``{"code": 4000, "message": "Invalid Client ID"}`` and closes the pipe. A real
AUTHORIZE / speaking session has not been exercised yet.
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import os
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from .. import __version__, paths

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ protocol --

OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2
OP_PING = 3
OP_PONG = 4

RPC_VERSION = 1
TOKEN_URL = "https://discord.com/api/oauth2/token"

#: "rpc" already covers voice events for an app's owner/testers and "rpc.voice.read" is
#: the narrower scope Discord documents for them. "identify" isn't needed, but other
#: tools found Discord asks for it in a second popup when it's missing.
DEFAULT_SCOPES: tuple[str, ...] = ("rpc", "rpc.voice.read", "identify")

#: Subscribed per voice channel, with args {"channel_id": ...}.
CHANNEL_EVENTS = (
    "SPEAKING_START",
    "SPEAKING_STOP",
    "VOICE_STATE_CREATE",
    "VOICE_STATE_UPDATE",
    "VOICE_STATE_DELETE",
)

# RPC error codes (data.code of a response with evt "ERROR"); 4009 = invalid token and
# 5000 = OAuth2 error (details only in data.message, e.g. "access_denied").
ERR_INVALID_CLIENTID = 4007
# Close codes (payload of a CLOSE frame); 4002 = rate limited.
CLOSE_INVALID_CLIENTID = 4000
CLOSE_TOKEN_REVOKED = 4003

_GUILD_VOICE_TYPES = (2, 13)  # GUILD_VOICE, GUILD_STAGE_VOICE (1 = DM, 3 = group DM)

MSG_NOT_STARTED = "Not started"
MSG_STOPPED = "Stopped"
MSG_CONNECTING = "Connecting to Discord..."
MSG_NOT_RUNNING = "Discord not running"
MSG_PIPE_DENIED = "Can't open Discord's pipe (run VoiceRhino and Discord at the same admin level)"
MSG_WAITING = "Waiting for approval in Discord"
MSG_SIGNING_IN = "Signing in to Discord..."
MSG_NO_CHANNEL = "Connected: not in a voice channel"
MSG_BAD_CLIENT_ID = "Discord doesn't recognise the client ID (use the Application ID)"
MSG_BAD_SECRET = "Discord rejected the client secret (copy it again from the Developer Portal)"
MSG_DECLINED = "Authorization was declined in Discord"
MSG_NOT_ALLOWED = "This Discord account can't use the app: only its owner and App Testers can"


@dataclass(frozen=True)
class SpeakingEvent:
    """Someone in our voice channel started or stopped speaking."""

    user_id: str
    name: str
    speaking: bool
    t: float  # time.monotonic() when the event was received


class TransportClosed(ConnectionError):
    """The IPC connection is gone: the pipe closed, or Discord sent CLOSE."""

    def __init__(self, message: str = "connection closed", code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class ProtocolError(TransportClosed):
    """Bytes on the pipe that aren't valid frames."""


class RpcTimeout(TransportClosed):
    """Discord stopped answering."""


class DiscordNotRunning(ConnectionError):
    """No discord-ipc pipe to connect to."""


class PipeAccessDenied(DiscordNotRunning):
    """The pipe exists but Windows refused it (usually an admin / non-admin mismatch)."""


class RpcError(Exception):
    """Discord answered a command with evt "ERROR"."""

    def __init__(self, cmd: str, code: Any, message: str) -> None:
        super().__init__(f"{cmd} failed: {message} ({code})")
        self.cmd = cmd
        self.code = code
        self.message = message


class _Stopped(Exception):
    """stop() was called."""


class _Fatal(Exception):
    """Needs the user to change something; retrying won't help (and could re-prompt)."""


class _Retry(Exception):
    def __init__(self, status: str, delay: float | None = None) -> None:
        super().__init__(status)
        self.status = status
        self.delay = delay


# ------------------------------------------------------------------- framing --

_HEADER = struct.Struct("<II")  # opcode, payload length (little-endian)
MAX_FRAME_BYTES = 8 * 1024 * 1024


def encode_frame(op: int, payload: Any) -> bytes:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return _HEADER.pack(op, len(body)) + body


class FrameDecoder:
    """Splits the pipe's byte stream into (op, payload) frames."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, Any]]:
        self._buf += data
        frames: list[tuple[int, Any]] = []
        while len(self._buf) >= _HEADER.size:
            op, length = _HEADER.unpack_from(self._buf)
            if length > MAX_FRAME_BYTES:
                raise ProtocolError(f"frame too large ({length} bytes)")
            end = _HEADER.size + length
            if len(self._buf) < end:
                break
            body = bytes(self._buf[_HEADER.size : end])
            del self._buf[:end]
            try:
                payload = json.loads(body.decode("utf-8")) if body else None
            except ValueError as exc:  # includes UnicodeDecodeError
                raise ProtocolError(f"bad frame payload: {exc}") from None
            frames.append((op, payload))
        return frames


# ----------------------------------------------------------------- transport --


class Transport(Protocol):
    """Whole frames over some byte pipe. Used from one thread only."""

    def send(self, op: int, payload: Any) -> None:
        """Send one frame; raises TransportClosed if the connection is gone."""

    def recv(self, timeout: float) -> tuple[int, Any] | None:
        """Next (op, payload), or None if nothing arrived within timeout seconds.

        Raises TransportClosed once the other end has gone away.
        """

    def close(self) -> None:
        """Idempotent."""


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.ReadFile.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    ]
    _k32.ReadFile.restype = wintypes.BOOL
    _k32.WriteFile.argtypes = [
        wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    ]
    _k32.WriteFile.restype = wintypes.BOOL
    _k32.PeekNamedPipe.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
    ]
    _k32.PeekNamedPipe.restype = wintypes.BOOL
    _k32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    _k32.WaitNamedPipeW.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _INVALID_HANDLE = ctypes.c_void_p(-1).value

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_ERROR_ACCESS_DENIED = 5
_ERROR_PIPE_BUSY = 231
_ERROR_MORE_DATA = 234


class PipeTransport:
    """Discord IPC over a Windows named pipe, stdlib only (ctypes + kernel32).

    The handle is synchronous, and synchronous I/O on one handle is serialised, so a
    write queued behind a blocked ReadFile would deadlock. We therefore only ReadFile
    what PeekNamedPipe says has arrived and poll otherwise (every 10 ms: negligible CPU,
    and plenty precise for speaking timestamps). All calls come from one thread.
    """

    PATH_TEMPLATE = r"\\.\pipe\discord-ipc-{}"
    POLL_INTERVAL = 0.01
    _READ_CHUNK = 64 * 1024

    def __init__(self, handle: int, path: str) -> None:
        self._handle: int | None = handle
        self.path = path
        self._decoder = FrameDecoder()
        self._frames: deque[tuple[int, Any]] = deque()
        self._buf = ctypes.create_string_buffer(self._READ_CHUNK)

    @classmethod
    def connect(cls, indices: Iterable[int] = range(10)) -> PipeTransport:
        """Open the first discord-ipc-N pipe that accepts us."""
        denied: PipeAccessDenied | None = None
        for index in indices:
            try:
                return cls.open(cls.PATH_TEMPLATE.format(index))
            except PipeAccessDenied as exc:
                denied = denied or exc
            except DiscordNotRunning:
                continue
        raise denied or DiscordNotRunning("no Discord IPC pipe found")

    @classmethod
    def open(cls, path: str) -> PipeTransport:
        if sys.platform != "win32":
            raise DiscordNotRunning("Discord IPC over named pipes needs Windows")
        error = 0
        for attempt in range(2):
            handle = _k32.CreateFileW(
                path, _GENERIC_READ | _GENERIC_WRITE, 0, None, _OPEN_EXISTING, 0, None
            )
            if handle and handle != _INVALID_HANDLE:
                return cls(handle, path)
            error = ctypes.get_last_error()
            if error == _ERROR_PIPE_BUSY and attempt == 0:
                _k32.WaitNamedPipeW(path, 2000)
                continue
            break
        if error == _ERROR_ACCESS_DENIED:
            raise PipeAccessDenied(f"access denied opening {path}")
        raise DiscordNotRunning(f"can't open {path} (error {error})")

    def _live_handle(self) -> int:
        if self._handle is None:
            raise TransportClosed("pipe closed")
        return self._handle

    def send(self, op: int, payload: Any) -> None:
        data = encode_frame(op, payload)
        while data:
            written = wintypes.DWORD(0)
            ok = _k32.WriteFile(self._live_handle(), data, len(data), ctypes.byref(written), None)
            if not ok:
                raise TransportClosed(f"pipe write failed (error {ctypes.get_last_error()})")
            data = data[written.value :]

    def recv(self, timeout: float) -> tuple[int, Any] | None:
        deadline = time.monotonic() + max(0.0, timeout)
        while not self._frames:
            available = self._available()
            if available:
                self._frames.extend(self._decoder.feed(self._read(available)))
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(self.POLL_INTERVAL, remaining))
        return self._frames.popleft()

    def _available(self) -> int:
        available = wintypes.DWORD(0)
        ok = _k32.PeekNamedPipe(self._live_handle(), None, 0, None, ctypes.byref(available), None)
        if not ok:
            raise TransportClosed(f"pipe closed (error {ctypes.get_last_error()})")
        return available.value

    def _read(self, count: int) -> bytes:
        got = wintypes.DWORD(0)
        count = min(count, self._READ_CHUNK)
        ok = _k32.ReadFile(self._live_handle(), self._buf, count, ctypes.byref(got), None)
        if not ok and ctypes.get_last_error() != _ERROR_MORE_DATA:
            raise TransportClosed(f"pipe read failed (error {ctypes.get_last_error()})")
        return self._buf.raw[: got.value]

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            _k32.CloseHandle(handle)


# ---------------------------------------------------------------------- HTTP --

#: post(url, form) -> (HTTP status, JSON body or {}). Raises OSError on network trouble.
HttpPost = Callable[[str, dict[str, str]], tuple[int, dict]]


def _json_object(raw: bytes) -> dict:
    try:
        value = json.loads(raw.decode("utf-8"))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def post_form(url: str, form: dict[str, str], timeout: float = 15.0) -> tuple[int, dict]:
    """POST application/x-www-form-urlencoded, return (status, JSON object)."""
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(form).encode("ascii"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            # urllib's default User-Agent gets blocked by Cloudflare in front of discord.com.
            "User-Agent": f"VoiceRhino/{__version__}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _json_object(response.read())
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read() or b""
        except (OSError, http.client.HTTPException):
            body = b""
        finally:
            exc.close()
        return exc.code, _json_object(body)


# -------------------------------------------------------------------- client --


def _snowflake(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _fallback_name(user_id: str) -> str:
    return f"User {user_id[-4:]}"


def _seconds_left(token: dict) -> float:
    try:
        return float(token.get("expires_at") or 0) - time.time()
    except (TypeError, ValueError):
        return 0.0


def _parse_voice_state(state: Any) -> tuple[str, str] | None:
    """(user_id, display name) from an RPC voice state, or None."""
    if not isinstance(state, dict):
        return None
    user = state.get("user")
    if not isinstance(user, dict):
        return None
    user_id = _snowflake(user.get("id"))
    if user_id is None:
        return None
    # nick is what the Discord client displays (server nickname, else display name);
    # global_name (camelCase in some client builds) and username are fallbacks.
    for candidate in (
        state.get("nick"),
        user.get("global_name"),
        user.get("globalName"),
        user.get("username"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return user_id, candidate.strip()
    return user_id, _fallback_name(user_id)


def _channel_label(data: dict) -> str:
    name = data.get("name")
    name = name.strip() if isinstance(name, str) else ""
    if name:
        return name
    return "Voice channel" if data.get("type") in _GUILD_VOICE_TYPES else "Direct call"


class DiscordSpeakingClient:
    """Follows the user's current Discord voice channel and reports who is speaking.

    Everything runs on one daemon thread started by start(): find the first
    discord-ipc pipe, handshake, sign in (cached token -> refresh -> AUTHORIZE popup in
    Discord), subscribe to the selected voice channel, dispatch events, and reconnect
    with backoff when Discord is closed or restarts. on_event / on_status are called
    from that thread; keep them quick (hand off to your own queue or Qt signal).
    on_status also fires once from the caller's thread when stop() runs on an idle
    client. The access token and client secret are never logged or put in a status.
    """

    NOT_RUNNING_RETRY = 5.0  # seconds between looks for the pipe
    RETRY_DELAYS = (2.0, 5.0, 10.0, 20.0, 30.0, 60.0)  # Discord allows ~2 connections/min
    NETWORK_RETRY = 30.0  # discord.com unreachable during sign-in
    AUTH_RETRY = 60.0  # sign-in failed in a way that may be temporary
    RENEW_RETRY = 1.0  # reconnect to use a just-renewed token
    HANDSHAKE_TIMEOUT = 60.0  # Discord delays (rather than refuses) connections over its limit
    REQUEST_TIMEOUT = 10.0
    RESYNC_INTERVAL = 60.0  # re-read the channel now and then, in case we missed an event
    UNKNOWN_RESYNC_INTERVAL = 2.0  # at most this often for "who is that?" lookups
    REFRESH_MARGIN = 24 * 3600.0  # refresh access tokens with less than this left

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        on_event: Callable[[SpeakingEvent], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        token_path: Path | None = None,
        redirect_uri: str = "http://localhost",
        *,
        scopes: Iterable[str] = DEFAULT_SCOPES,
        include_self: bool = False,
        transport_factory: Callable[[], Transport] | None = None,
        http_post: HttpPost | None = None,
    ) -> None:
        """
        include_self: also report the signed-in user's own speaking. Off by default:
            captions come from system audio, which never contains your own mic.
        transport_factory / http_post: injection points for tests (defaults: the real
            pipe and urllib).
        """
        self.client_id = str(client_id or "").strip()
        self._client_secret = str(client_secret or "").strip()
        self._on_event = on_event
        self._on_status = on_status
        self._token_path = Path(token_path) if token_path else None
        self.redirect_uri = redirect_uri
        self.scopes = tuple(scopes)
        self.include_self = include_self
        self._transport_factory = transport_factory or PipeTransport.connect
        self._http_post = http_post or post_form

        self._lock = threading.Lock()  # guards the fields read by the properties
        self._status = MSG_NOT_STARTED
        self._channel_id: str | None = None
        self._channel_name: str | None = None
        self._channel_is_guild = True
        self._members: dict[str, str] = {}
        self._user_id: str | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # Session state, only touched by the client thread.
        self._transport: Transport | None = None
        self._stop_flag = self._stop_event
        self._queue: deque[tuple[dict, float]] = deque()  # frames read while awaiting a reply
        self._speaking: dict[str, str] = {}  # user_id -> name, currently speaking
        self._authenticated = False
        self._auth_failures = 0
        self._last_sync = 0.0
        self._last_unknown_sync = -1e9

    # ---------------------------------------------------------------- public --

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    @property
    def members(self) -> dict[str, str]:
        """user_id -> display name for everyone in the current voice channel."""
        with self._lock:
            return dict(self._members)

    @property
    def channel_name(self) -> str | None:
        with self._lock:
            return self._channel_name

    @property
    def channel_id(self) -> str | None:
        with self._lock:
            return self._channel_id

    @property
    def user_id(self) -> str | None:
        """The signed-in Discord user's id (None until connected)."""
        with self._lock:
            return self._user_id

    def start(self) -> None:
        """Start the client thread (no-op while it is already running)."""
        with self._lock:
            previous = self._thread
            if previous is not None and previous.is_alive() and not self._stop_event.is_set():
                return
            stop = threading.Event()
            thread = threading.Thread(
                target=self._run, args=(stop, previous), name="discord-rpc", daemon=True
            )
            self._stop_event = stop
            self._thread = thread
        thread.start()

    def stop(self) -> None:
        """Disconnect and end the client thread. Idempotent; safe from callbacks."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        if thread is None or not thread.is_alive():
            self._set_status(MSG_STOPPED)

    # ---------------------------------------------------------- thread main --

    def _run(self, stop: threading.Event, previous: threading.Thread | None) -> None:
        if previous is not None:
            # A quick stop()/start() can find the old thread still finishing (say, in an
            # HTTP call); session state is per-thread, so let it end before taking over.
            previous.join()
        self._stop_flag = stop
        failures = 0
        try:
            if stop.is_set():
                return
            problem = self._config_problem()
            if problem:
                self._set_status(problem)
                return
            self._set_status(MSG_CONNECTING)
            while not stop.is_set():
                try:
                    transport = self._transport_factory()
                except PipeAccessDenied:
                    self._set_status(MSG_PIPE_DENIED)
                    stop.wait(self.NOT_RUNNING_RETRY)
                    continue
                except OSError:  # DiscordNotRunning, or the pipe vanished mid-open
                    self._set_status(MSG_NOT_RUNNING)
                    stop.wait(self.NOT_RUNNING_RETRY)
                    continue
                delay: float | None = None
                status = "Lost connection to Discord"
                try:
                    self._session(transport)
                except _Stopped:
                    break
                except _Fatal as exc:
                    self._set_status(str(exc))
                    return
                except _Retry as exc:
                    status, delay = exc.status, exc.delay
                except TransportClosed as exc:
                    if exc.code == CLOSE_TOKEN_REVOKED:
                        self._clear_token()
                        status = "Discord revoked the sign-in"
                except RpcError as exc:
                    status = f"Discord error: {exc.message or exc.code}"
                except Exception:
                    log.exception("Discord RPC session crashed")
                    status = "Discord integration error"
                finally:
                    self._end_session(transport)
                if self._authenticated:
                    failures = 0
                failures += 1
                if delay is None:
                    delay = self.RETRY_DELAYS[min(failures, len(self.RETRY_DELAYS)) - 1]
                if not stop.is_set():
                    self._set_status(f"{status} - retrying in {delay:.0f} s")
                stop.wait(delay)
        finally:
            if stop.is_set():
                self._set_status(MSG_STOPPED)

    def _config_problem(self) -> str | None:
        if not self.client_id:
            return "Discord client ID not set"
        if not self.client_id.isdigit():
            return "Discord client ID should be the numeric Application ID"
        if not self._client_secret:
            return "Discord client secret not set"
        return None

    def _session(self, transport: Transport) -> None:
        self._transport = transport
        self._queue.clear()
        self._speaking.clear()
        self._authenticated = False
        transport.send(OP_HANDSHAKE, {"v": RPC_VERSION, "client_id": self.client_id})
        try:
            ready = self._wait_ready()
        except TransportClosed as exc:
            if exc.code == CLOSE_INVALID_CLIENTID:
                raise _Fatal(MSG_BAD_CLIENT_ID) from None
            raise
        user = ready.get("user")
        if isinstance(user, dict) and user.get("id"):
            with self._lock:
                self._user_id = str(user["id"])
        self._authenticate()
        self._authenticated = True
        self._request("SUBSCRIBE", {}, evt="VOICE_CHANNEL_SELECT")
        self._sync_channel()
        while True:
            self._drain_queue()
            self._check_stop()
            if time.monotonic() - self._last_sync >= self.RESYNC_INTERVAL:
                self._sync_channel()
                continue
            item = self._next_frame(0.25)
            if item is not None:
                self._handle(*item)

    def _end_session(self, transport: Transport) -> None:
        try:
            self._leave_channel(unsubscribe=False)  # closes open speaking intervals
        finally:
            self._transport = None
            self._queue.clear()
            try:
                transport.close()
            except Exception:
                log.debug("closing the Discord transport failed", exc_info=True)

    # ------------------------------------------------------------- plumbing --

    def _check_stop(self) -> None:
        if self._stop_flag.is_set():
            raise _Stopped

    def _next_frame(self, timeout: float) -> tuple[dict, float] | None:
        """One incoming FRAME (payload, receive time); handles PING and CLOSE itself."""
        assert self._transport is not None
        got = self._transport.recv(timeout)
        if got is None:
            return None
        received = time.monotonic()
        op, payload = got
        if op == OP_FRAME and isinstance(payload, dict):
            return payload, received
        if op == OP_PING:
            self._transport.send(OP_PONG, payload)
        elif op == OP_CLOSE:
            info = payload if isinstance(payload, dict) else {}
            code = info.get("code")
            raise TransportClosed(
                str(info.get("message") or "Discord closed the connection"),
                code=code if isinstance(code, int) else None,
            )
        return None

    def _wait_ready(self) -> dict:
        deadline = time.monotonic() + self.HANDSHAKE_TIMEOUT
        while True:
            self._check_stop()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RpcTimeout("Discord didn't answer the handshake")
            item = self._next_frame(min(0.25, remaining))
            if item is None:
                continue
            frame = item[0]
            data = frame.get("data") if isinstance(frame.get("data"), dict) else {}
            if frame.get("evt") == "READY":
                return data
            if frame.get("evt") == "ERROR":
                if data.get("code") == ERR_INVALID_CLIENTID:
                    raise _Fatal(MSG_BAD_CLIENT_ID)
                raise RpcError("HANDSHAKE", data.get("code"), str(data.get("message") or ""))

    def _request(
        self, cmd: str, args: dict, evt: str | None = None, timeout: float | None = None
    ) -> Any:
        """Send a command and wait for its reply (timeout: REQUEST_TIMEOUT if None).

        Other frames that arrive meanwhile are queued, in order, for _drain_queue.
        """
        assert self._transport is not None
        nonce = str(uuid.uuid4())
        message: dict[str, Any] = {"cmd": cmd, "args": args, "nonce": nonce}
        if evt is not None:
            message["evt"] = evt
        self._transport.send(OP_FRAME, message)
        deadline = time.monotonic() + (self.REQUEST_TIMEOUT if timeout is None else timeout)
        while True:
            self._check_stop()
            wait = min(0.25, deadline - time.monotonic())
            if wait <= 0:
                raise RpcTimeout(f"Discord didn't answer {cmd}")
            item = self._next_frame(wait)
            if item is None:
                continue
            frame = item[0]
            if frame.get("nonce") != nonce:
                self._queue.append(item)
                continue
            data = frame.get("data")
            if frame.get("evt") == "ERROR":
                info = data if isinstance(data, dict) else {}
                raise RpcError(cmd, info.get("code"), str(info.get("message") or ""))
            return data

    def _drain_queue(self) -> None:
        while self._queue:
            self._handle(*self._queue.popleft())

    def _set_status(self, status: str) -> None:
        with self._lock:
            if status == self._status:
                return
            self._status = status
        log.info("Discord: %s", status)
        if self._on_status is not None:
            try:
                self._on_status(status)
            except Exception:
                log.exception("Discord on_status callback failed")

    def _emit(self, event: SpeakingEvent) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception:
            log.exception("Discord on_event callback failed")

    # ----------------------------------------------------------------- auth --

    def _token_file(self) -> Path:
        return self._token_path or paths.config_dir() / "discord_token.json"

    def _load_token(self) -> dict | None:
        try:
            token = json.loads(self._token_file().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            log.warning("Ignoring an unreadable Discord token cache")
            return None
        if (
            not isinstance(token, dict)
            or token.get("client_id") != self.client_id
            or not token.get("access_token")
            or list(token.get("scopes") or []) != list(self.scopes)
        ):
            return None
        return token

    def _save_token(self, token: dict) -> None:
        path = self._token_file()
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(token, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            log.warning("Couldn't save the Discord token cache to %s", path, exc_info=True)

    def _clear_token(self) -> None:
        try:
            self._token_file().unlink(missing_ok=True)
        except OSError:
            log.warning("Couldn't delete the Discord token cache", exc_info=True)

    def _authenticate(self) -> None:
        token = self._load_token()
        if token is not None and _seconds_left(token) < self.REFRESH_MARGIN:
            token = self._refresh(token)
        if token is not None:
            try:
                self._send_authenticate(token["access_token"])
                self._auth_failures = 0
                return
            except RpcError as exc:
                self._auth_failures += 1
                log.info("Discord didn't accept the saved token (code %s)", exc.code)
            # Don't AUTHENTICATE twice on one connection: fix the token, then reconnect.
            if self._auth_failures >= 2 or self._refresh(token) is None:
                self._clear_token()  # the next connection asks for approval again
            raise _Retry("Renewing the Discord sign-in", self.RENEW_RETRY)
        code = self._authorize()
        self._set_status(MSG_SIGNING_IN)
        token = self._exchange_code(code)
        try:
            self._send_authenticate(token["access_token"])
        except RpcError as exc:
            self._clear_token()
            raise _Retry(f"Discord didn't accept the new sign-in ({exc.message})", self.AUTH_RETRY)
        self._auth_failures = 0

    def _send_authenticate(self, access_token: str) -> None:
        data = self._request("AUTHENTICATE", {"access_token": access_token}, timeout=20.0)
        user = data.get("user") if isinstance(data, dict) else None
        if isinstance(user, dict) and user.get("id"):
            with self._lock:
                self._user_id = str(user["id"])

    def _authorize(self) -> str:
        self._set_status(MSG_WAITING)
        args: dict[str, Any] = {
            "client_id": self.client_id,
            "scopes": list(self.scopes),
            # No redirect_uri: Discord refuses it here ("Redirect URI cannot be used in the RPC
            # OAuth2 Authorization flow") and uses the app's registered redirect instead, which
            # the token exchange must then repeat.
            # Skip the popup when this app was already approved with these scopes
            # (first-time approval still shows it).
            "prompt": "none",
        }
        while True:
            try:
                data = self._request("AUTHORIZE", args, timeout=math.inf)  # the user decides
                break
            except RpcError as exc:
                text = exc.message.lower()
                if "prompt" in args and any(
                    word in text for word in ("prompt", "consent", "interaction", "login_required")
                ):
                    del args["prompt"]  # a client that wants the popup shown: ask again with it
                    continue
                if "denied" in text or "cancel" in text:  # OAuth2 "access_denied"
                    raise _Fatal(MSG_DECLINED) from None
                if "invalid_scope" in text:
                    raise _Fatal(MSG_NOT_ALLOWED) from None
                if exc.code == ERR_INVALID_CLIENTID:
                    raise _Fatal(MSG_BAD_CLIENT_ID) from None
                if "redirect" in text:  # e.g. no redirect registered in the portal
                    raise _Fatal(self._redirect_message()) from None
                raise _Retry(
                    f"Discord authorization failed: {exc.message or exc.code}", self.AUTH_RETRY
                ) from None
        code = data.get("code") if isinstance(data, dict) else None
        if not isinstance(code, str) or not code:
            raise _Retry("Discord authorization returned no code", self.AUTH_RETRY)
        return code

    def _post_token(self, form: dict[str, str]) -> tuple[int, dict]:
        form = {**form, "client_id": self.client_id, "client_secret": self._client_secret}
        try:
            return self._http_post(TOKEN_URL, form)
        except (OSError, http.client.HTTPException) as exc:
            raise _Retry(
                f"Can't reach discord.com ({type(exc).__name__})", self.NETWORK_RETRY
            ) from None

    def _new_token(self, body: dict, old: dict | None = None) -> dict:
        try:
            lifetime = float(body.get("expires_in") or 604800)
        except (TypeError, ValueError):
            lifetime = 604800.0
        token = {
            "client_id": self.client_id,
            "scopes": list(self.scopes),
            "access_token": body["access_token"],
            "refresh_token": body.get("refresh_token") or (old or {}).get("refresh_token"),
            "expires_at": time.time() + lifetime,
            "scope": body.get("scope", ""),
        }
        self._save_token(token)
        return token

    def _redirect_message(self) -> str:
        return (
            f"Discord rejected the redirect URI: add {self.redirect_uri} "
            "under OAuth2 > Redirects in the Developer Portal"
        )

    def _token_error(self, status: int, body: dict) -> None:
        """Raise for token endpoint failures that mean the settings are wrong."""
        error = str(body.get("error") or "")
        description = str(body.get("error_description") or "")
        if status == 401 or error == "invalid_client":
            raise _Fatal(MSG_BAD_SECRET)
        if "redirect_uri" in description.lower():
            raise _Fatal(self._redirect_message())
        if error == "invalid_scope":
            raise _Fatal(MSG_NOT_ALLOWED)

    def _exchange_code(self, code: str) -> dict:
        status, body = self._post_token(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri}
        )
        if status == 200 and body.get("access_token"):
            return self._new_token(body)
        self._token_error(status, body)
        reason = body.get("error") or f"HTTP {status}"
        raise _Retry(f"Discord sign-in failed ({reason})", self.AUTH_RETRY)

    def _refresh(self, token: dict) -> dict | None:
        """A fresh token, or None if Discord no longer honours this refresh token."""
        refresh_token = token.get("refresh_token")
        if not refresh_token:
            return None
        self._set_status(MSG_SIGNING_IN)
        status, body = self._post_token(
            {"grant_type": "refresh_token", "refresh_token": str(refresh_token)}
        )
        if status == 200 and body.get("access_token"):
            return self._new_token(body, old=token)
        self._token_error(status, body)
        if status == 429 or status >= 500:
            raise _Retry(f"Discord sign-in is unavailable (HTTP {status})", self.AUTH_RETRY)
        log.info("Discord refused the refresh token (%s)", body.get("error") or status)
        self._clear_token()
        return None

    # -------------------------------------------------------------- channel --

    def _sync_channel(self) -> None:
        """Follow the client's selected voice channel: resubscribe and reload members."""
        self._last_sync = time.monotonic()
        data: dict | None = None
        new_id: str | None = None
        for _ in range(3):
            reply = self._request("GET_SELECTED_VOICE_CHANNEL", {})
            data = reply if isinstance(reply, dict) and reply.get("id") else None
            new_id = _snowflake(data.get("id")) if data else None
            if new_id == self._channel_id:
                break
            self._leave_channel(unsubscribe=True)
            if new_id is None:
                break
            for evt in CHANNEL_EVENTS:
                self._request("SUBSCRIBE", {"channel_id": new_id}, evt=evt)
            with self._lock:
                self._channel_id = new_id
            # Loop to fetch again: joins/leaves before SUBSCRIBE took effect are included.
        self._apply_channel(data if new_id is not None and new_id == self._channel_id else None)

    def _apply_channel(self, data: dict | None) -> None:
        with self._lock:
            if data is None:
                self._channel_id = None
                self._channel_name = None
                self._members = {}
            else:
                members: dict[str, str] = {}
                for state in data.get("voice_states") or []:
                    parsed = _parse_voice_state(state)
                    if parsed is not None:
                        members[parsed[0]] = parsed[1]
                self._channel_name = _channel_label(data)
                self._channel_is_guild = data.get("type", 2) in _GUILD_VOICE_TYPES
                self._members = members
        self._update_status()

    def _leave_channel(self, unsubscribe: bool) -> None:
        now = time.monotonic()
        for user_id, name in list(self._speaking.items()):
            self._emit(SpeakingEvent(user_id, name, False, now))
        self._speaking.clear()
        with self._lock:
            old = self._channel_id
            self._channel_id = None
            self._channel_name = None
            self._members = {}
        if old is None or not unsubscribe:
            return
        for evt in CHANNEL_EVENTS:
            try:
                self._request("UNSUBSCRIBE", {"channel_id": old}, evt=evt)
            except RpcError as exc:
                log.debug("UNSUBSCRIBE %s failed: %s", evt, exc)
        # Whatever is still queued for the old channel is stale now.
        keep = [item for item in self._queue if item[0].get("evt") not in CHANNEL_EVENTS]
        self._queue.clear()
        self._queue.extend(keep)

    def _update_status(self) -> None:
        with self._lock:
            channel_id, name, count = self._channel_id, self._channel_name, len(self._members)
            is_guild = self._channel_is_guild
        if channel_id is None:
            self._set_status(MSG_NO_CHANNEL)
            return
        label = f"#{name}" if is_guild else str(name)
        people = "person" if count == 1 else "people"
        self._set_status(f"Connected: {label} ({count} {people})")

    # --------------------------------------------------------------- events --

    def _handle(self, frame: dict, received: float) -> None:
        if frame.get("cmd") != "DISPATCH":
            if frame.get("evt") == "ERROR":
                info = frame.get("data") if isinstance(frame.get("data"), dict) else {}
                log.warning("Discord RPC error: %s (%s)", info.get("message"), info.get("code"))
            return  # e.g. a late reply to a request that already timed out
        evt = frame.get("evt")
        data = frame.get("data") if isinstance(frame.get("data"), dict) else {}
        if evt == "SPEAKING_START" or evt == "SPEAKING_STOP":
            self._on_speaking(data, evt == "SPEAKING_START", received)
        elif evt == "VOICE_STATE_CREATE" or evt == "VOICE_STATE_UPDATE":
            self._on_voice_state(data, present=True)
        elif evt == "VOICE_STATE_DELETE":
            self._on_voice_state(data, present=False)
        elif evt == "VOICE_CHANNEL_SELECT":
            if _snowflake(data.get("channel_id")) != self._channel_id:
                self._sync_channel()

    def _on_speaking(self, data: dict, speaking: bool, received: float) -> None:
        user_id = _snowflake(data.get("user_id"))
        channel = self._channel_id
        if user_id is None or channel is None:
            return
        event_channel = _snowflake(data.get("channel_id"))
        if event_channel is not None and event_channel != channel:
            return  # left over from a channel we already left
        if user_id == self._user_id and not self.include_self:
            return
        if speaking:
            if user_id in self._speaking:
                return
            name = self._lookup_name(user_id)
            if self._channel_id != channel:
                return  # the lookup found we've moved on to another channel
            self._speaking[user_id] = name
        else:
            name = self._speaking.pop(user_id, None)
            if name is None:
                return
        self._emit(SpeakingEvent(user_id, name, speaking, received))

    def _lookup_name(self, user_id: str) -> str:
        with self._lock:
            name = self._members.get(user_id)
        now = time.monotonic()
        if name is None and now - self._last_unknown_sync >= self.UNKNOWN_RESYNC_INTERVAL:
            self._last_unknown_sync = now
            self._sync_channel()
            with self._lock:
                name = self._members.get(user_id)
        return name or _fallback_name(user_id)

    def _on_voice_state(self, data: dict, present: bool) -> None:
        parsed = _parse_voice_state(data)
        if parsed is None or self._channel_id is None:
            return
        user_id, name = parsed
        if present:
            with self._lock:
                self._members[user_id] = name
            if user_id in self._speaking:
                self._speaking[user_id] = name
        else:
            with self._lock:
                self._members.pop(user_id, None)
            spoken_as = self._speaking.pop(user_id, None)
            if spoken_as is not None:
                self._emit(SpeakingEvent(user_id, spoken_as, False, time.monotonic()))
            if user_id == self._user_id:
                # We left or were moved; VOICE_CHANNEL_SELECT doesn't always fire for moves.
                self._sync_channel()
                return
        self._update_status()


# ------------------------------------------------------------------ timeline --


class SpeakingTimeline:
    """Who was speaking when, built from SpeakingEvents. Thread-safe.

    Discord's speaking indicator flaps (stop/start a few frames apart inside one
    sentence), so a start within merge_gap seconds of the same user's last stop
    continues that interval instead of opening a new one.
    """

    def __init__(
        self, merge_gap: float = 0.25, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.merge_gap = merge_gap
        self._clock = clock
        self._lock = threading.Lock()
        self._closed: dict[str, list[tuple[float, float]]] = {}  # sorted by start
        self._open: dict[str, float] = {}  # user_id -> start of the running interval
        self._names: dict[str, str] = {}

    def add(self, ev: SpeakingEvent) -> None:
        with self._lock:
            if ev.name:
                self._names[ev.user_id] = ev.name
            if ev.speaking:
                if ev.user_id in self._open:
                    return
                start = ev.t
                spans = self._closed.get(ev.user_id)
                if spans and ev.t - spans[-1][1] < self.merge_gap:
                    start = min(spans.pop()[0], ev.t)
                    if not spans:
                        del self._closed[ev.user_id]
                self._open[ev.user_id] = start
            else:
                start = self._open.pop(ev.user_id, None)
                if start is not None:
                    self._closed.setdefault(ev.user_id, []).append((start, max(start, ev.t)))

    def overlap(self, t0: float, t1: float) -> list[tuple[str, str, float]]:
        """[(user_id, name, seconds spoken within [t0, t1])], most talkative first.

        An interval that is still open counts as ending now.
        """
        if t1 < t0:
            t0, t1 = t1, t0
        totals: dict[str, float] = {}
        with self._lock:
            now = self._clock()
            for user_id, spans in self._closed.items():
                total = 0.0
                for start, end in spans:
                    if start >= t1:
                        break
                    if end > t0:
                        total += min(end, t1) - max(start, t0)
                if total > 0:
                    totals[user_id] = total
            for user_id, start in self._open.items():
                seconds = min(now, t1) - max(start, t0)
                if seconds > 0:
                    totals[user_id] = totals.get(user_id, 0.0) + seconds
            result = [(uid, self._names.get(uid, uid), secs) for uid, secs in totals.items()]
        result.sort(key=lambda row: (-row[2], row[0]))
        return result

    def prune(self, older_than: float) -> None:
        """Forget intervals that ended before older_than (open ones are kept)."""
        with self._lock:
            for user_id in list(self._closed):
                spans = [span for span in self._closed[user_id] if span[1] >= older_than]
                if spans:
                    self._closed[user_id] = spans
                else:
                    del self._closed[user_id]
            for user_id in list(self._names):
                if user_id not in self._closed and user_id not in self._open:
                    del self._names[user_id]
