"""Discord RPC speaking client against an in-memory fake Discord (no pipe, no network)."""

from __future__ import annotations

import copy
import json
import queue
import struct
import sys
import threading
import time
import uuid

import pytest

from voicerhino.speakers.discord_rpc import (
    CHANNEL_EVENTS,
    MSG_DECLINED,
    MSG_NOT_ALLOWED,
    MSG_NOT_RUNNING,
    MSG_STOPPED,
    MSG_WAITING,
    OP_CLOSE,
    OP_FRAME,
    OP_HANDSHAKE,
    OP_PING,
    OP_PONG,
    TOKEN_URL,
    DiscordNotRunning,
    DiscordSpeakingClient,
    FrameDecoder,
    PipeTransport,
    ProtocolError,
    SpeakingEvent,
    SpeakingTimeline,
    TransportClosed,
    encode_frame,
)

CLIENT_ID = "123456789012345678"
SECRET = "s3cret-value"
ME = "1"

# ---------------------------------------------------------------- fake Discord --

_EOF = object()


def voice_state(user_id, username, nick=None, global_name=None):
    return {
        "nick": nick,
        "mute": False,
        "volume": 100,
        "pan": {"left": 1.0, "right": 1.0},
        "voice_state": {"mute": False, "deaf": False, "self_mute": False, "self_deaf": False,
                        "suppress": False},
        "user": {"id": user_id, "username": username, "discriminator": "0",
                 "global_name": global_name, "avatar": None, "bot": False},
    }


def channel(channel_id, name, states, kind=2):
    return {"id": channel_id, "name": name, "type": kind, "guild_id": "900", "bitrate": 64000,
            "user_limit": 0, "position": 0, "voice_states": states}


class FakeTransport:
    """One IPC connection to FakeDiscord, frame-level (what PipeTransport would decode)."""

    def __init__(self, discord: FakeDiscord) -> None:
        self.discord = discord
        self.inbox: queue.Queue = queue.Queue()
        self.subs: set[tuple[str, str | None]] = set()
        self.authed = False
        self.closed = False
        self.pongs: list = []

    # Transport protocol
    def send(self, op, payload):
        if self.closed:
            raise TransportClosed("fake closed")
        json.dumps(payload)  # must be serialisable, like the real thing
        if op == OP_HANDSHAKE:
            self._handshake(payload)
        elif op == OP_FRAME:
            self._command(payload)
        elif op == OP_PONG:
            self.pongs.append(payload)

    def recv(self, timeout):
        try:
            item = self.inbox.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is _EOF:
            self.inbox.put(_EOF)
            raise TransportClosed("fake pipe closed")
        return item

    def close(self):
        self.closed = True

    # Discord side
    def push(self, op, payload):
        self.inbox.put((op, payload))

    def dispatch(self, evt, data):
        self.push(OP_FRAME, {"cmd": "DISPATCH", "evt": evt, "data": data, "nonce": None})

    def dispatch_channel_event(self, evt, channel_id, data):
        if (evt, channel_id) in self.subs:
            self.dispatch(evt, data)

    def kill(self):
        self.inbox.put(_EOF)

    def _reply(self, cmd, nonce, data):
        self.push(OP_FRAME, {"cmd": cmd, "data": data, "evt": None, "nonce": nonce})

    def _error(self, cmd, nonce, code, message):
        self.push(OP_FRAME, {"cmd": cmd, "data": {"code": code, "message": message},
                             "evt": "ERROR", "nonce": nonce})

    def _handshake(self, payload):
        d = self.discord
        d.handshakes.append(payload)
        if d.handshake_close is not None:
            self.push(OP_CLOSE, d.handshake_close)
            self.kill()
            return
        self.dispatch("READY", {"v": 1, "config": {"cdn_host": "cdn.discordapp.com",
                                                   "api_endpoint": "//discord.com/api",
                                                   "environment": "production"},
                                "user": d.local_user})

    def _command(self, msg):
        d = self.discord
        with d.lock:
            d.commands.append(copy.deepcopy(msg))
        cmd, nonce, args = msg["cmd"], msg.get("nonce"), msg.get("args") or {}
        if cmd == "AUTHORIZE":
            if d.authorize_error is not None:
                return self._error(cmd, nonce, *d.authorize_error)
            if "redirect_uri" in args:
                return self._error(cmd, nonce, 5000, "Redirect URI cannot be used in the RPC "
                                                     "OAuth2 Authorization flow")
            if d.reject_prompt_none and args.get("prompt") == "none":
                return self._error(cmd, nonce, 5000, "OAuth2 Error: consent_required")
            return self._reply(cmd, nonce, {"code": d.code})
        if cmd == "AUTHENTICATE":
            token = args.get("access_token")
            if token not in d.valid_tokens:
                return self._error(cmd, nonce, 4009, "Invalid access token")
            self.authed = True
            return self._reply(cmd, nonce, {
                "access_token": token, "user": d.local_user, "scopes": ["rpc", "rpc.voice.read"],
                "expires": "2030-01-01T00:00:00.000000+00:00",
                "application": {"id": CLIENT_ID, "name": "VoiceRhino", "description": "",
                                "icon": None, "rpc_origins": []},
            })
        if not self.authed:
            return self._error(cmd, nonce, 4006, "Not authenticated or invalid scope")
        if cmd == "GET_SELECTED_VOICE_CHANNEL":
            with d.lock:
                data = copy.deepcopy(d.channels.get(d.selected)) if d.selected else None
            return self._reply(cmd, nonce, data)
        if cmd == "SUBSCRIBE":
            self.subs.add((msg["evt"], args.get("channel_id")))
            return self._reply(cmd, nonce, {"evt": msg["evt"]})
        if cmd == "UNSUBSCRIBE":
            self.subs.discard((msg["evt"], args.get("channel_id")))
            return self._reply(cmd, nonce, {"evt": msg["evt"]})
        return self._error(cmd, nonce, 4002, "Invalid command")


class FakeDiscord:
    """The Discord desktop client: state shared by every connection to it."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = True
        self.handshake_close: dict | None = None
        self.authorize_error: tuple[int, str] | None = None
        self.reject_prompt_none = False
        self.code = "auth-code"
        self.valid_tokens: set[str] = set()
        self.local_user = {"id": ME, "username": "me", "discriminator": "0", "global_name": "Me",
                           "avatar": None}
        self.channels = {
            "100": channel("100", "General", [
                voice_state(ME, "me", nick="Me"),
                voice_state("2", "alice", global_name="Alice A"),
                voice_state("3", "bob"),
            ]),
            "200": channel("200", "Raid", [
                voice_state(ME, "me", nick="Me"),
                voice_state("4", "carol", nick="Carol (server nick)", global_name="Carol"),
            ]),
        }
        self.selected: str | None = "100"
        self.transports: list[FakeTransport] = []
        self.handshakes: list[dict] = []
        self.commands: list[dict] = []

    def connect(self) -> FakeTransport:
        if not self.running:
            raise DiscordNotRunning("fake Discord is not running")
        transport = FakeTransport(self)
        self.transports.append(transport)
        return transport

    @property
    def conn(self) -> FakeTransport:
        return self.transports[-1]

    def cmds(self, name, evt=None):
        with self.lock:
            return [c for c in self.commands
                    if c["cmd"] == name and (evt is None or c.get("evt") == evt)]

    def speak(self, user_id, on=True, channel_id=None):
        channel_id = channel_id or self.selected
        evt = "SPEAKING_START" if on else "SPEAKING_STOP"
        data = {"channel_id": channel_id, "user_id": user_id}
        for t in self.transports:
            t.dispatch_channel_event(evt, channel_id, data)

    def select(self, channel_id):
        with self.lock:
            self.selected = channel_id
        for t in self.transports:
            if ("VOICE_CHANNEL_SELECT", None) in t.subs:
                t.dispatch("VOICE_CHANNEL_SELECT",
                           {"channel_id": channel_id, "guild_id": "900" if channel_id else None})

    def join(self, channel_id, state, announce=True):
        with self.lock:
            self.channels[channel_id]["voice_states"].append(state)
        if announce:
            for t in self.transports:
                t.dispatch_channel_event("VOICE_STATE_CREATE", channel_id, state)

    def leave(self, channel_id, user_id):
        with self.lock:
            states = self.channels[channel_id]["voice_states"]
            state = next(s for s in states if s["user"]["id"] == user_id)
            states.remove(state)
        for t in self.transports:
            t.dispatch_channel_event("VOICE_STATE_DELETE", channel_id, state)

    def quit(self):
        self.running = False
        for t in self.transports:
            t.kill()


class FakeTokenEndpoint:
    """https://discord.com/api/oauth2/token, as the client's http_post sees it."""

    def __init__(self, discord: FakeDiscord, redirect_uri="http://localhost") -> None:
        self.discord = discord
        self.redirect_uri = redirect_uri
        self.calls: list[tuple[str, dict]] = []
        self.valid_refresh: set[str] = set()
        self.issued = 0
        self.fail: tuple[int, dict] | None = None

    def __call__(self, url, form):
        self.calls.append((url, dict(form)))
        if self.fail is not None:
            return self.fail
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != SECRET:
            return 401, {"error": "invalid_client"}
        grant = form.get("grant_type")
        if grant == "authorization_code":
            if form.get("code") != self.discord.code:
                return 400, {"error": "invalid_grant",
                             "error_description": "Invalid \"code\" in request."}
            if form.get("redirect_uri") != self.redirect_uri:
                return 400, {"error": "invalid_grant",
                             "error_description": "Invalid \"redirect_uri\" in request."}
        elif grant == "refresh_token":
            if form.get("refresh_token") not in self.valid_refresh:
                return 400, {"error": "invalid_grant"}
            self.valid_refresh.discard(form["refresh_token"])  # rotated
        else:
            return 400, {"error": "unsupported_grant_type"}
        return 200, self.issue()

    def issue(self):
        self.issued += 1
        access, refresh = f"access-{self.issued}", f"refresh-{self.issued}"
        self.discord.valid_tokens.add(access)
        self.valid_refresh.add(refresh)
        return {"access_token": access, "token_type": "Bearer", "expires_in": 604800,
                "refresh_token": refresh, "scope": "rpc rpc.voice.read identify"}

    def grants(self):
        return [form["grant_type"] for _, form in self.calls]


# --------------------------------------------------------------------- helpers --


def wait_for(predicate, timeout=3.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


class Harness:
    def __init__(self, tmp_path, **kwargs):
        self.discord = FakeDiscord()
        self.http = FakeTokenEndpoint(self.discord)
        self.token_path = tmp_path / "discord_token.json"
        self.events: list[SpeakingEvent] = []
        self.statuses: list[str] = []
        kwargs.setdefault("token_path", self.token_path)
        self.client = DiscordSpeakingClient(
            kwargs.pop("client_id", CLIENT_ID),
            kwargs.pop("client_secret", SECRET),
            on_event=self.events.append,
            on_status=self.statuses.append,
            transport_factory=self.discord.connect,
            http_post=self.http,
            **kwargs,
        )
        self.client.NOT_RUNNING_RETRY = 0.02
        self.client.RETRY_DELAYS = (0.02,)
        self.client.NETWORK_RETRY = self.client.AUTH_RETRY = self.client.RENEW_RETRY = 0.02

    def write_token(self, access="cached-access", refresh="cached-refresh", expires_in=3 * 86400,
                    valid=True, **extra):
        token = {"client_id": CLIENT_ID, "scopes": list(self.client.scopes),
                 "access_token": access, "refresh_token": refresh,
                 "expires_at": time.time() + expires_in, "scope": "rpc rpc.voice.read identify"}
        token.update(extra)
        self.token_path.write_text(json.dumps(token), encoding="utf-8")
        if valid:
            self.discord.valid_tokens.add(access)
        self.http.valid_refresh.add(refresh)

    def read_token(self):
        return json.loads(self.token_path.read_text(encoding="utf-8"))

    def wait_status(self, text, timeout=3.0):
        wait_for(lambda: text in self.client.status, timeout, f"status {text!r} "
                 f"(now {self.client.status!r})")

    def wait_events(self, count, timeout=3.0):
        wait_for(lambda: len(self.events) >= count, timeout,
                 f"{count} events (have {self.events})")

    def simple_events(self):
        return [(e.user_id, e.name, e.speaking) for e in self.events]


@pytest.fixture
def harness(tmp_path):
    made: list[Harness] = []

    def make(**kwargs):
        h = Harness(tmp_path, **kwargs)
        made.append(h)
        return h

    yield make
    for h in made:
        h.client.stop()


def connected(h, status="Connected: #General (3 people)"):
    h.client.start()
    h.wait_status(status)


# ------------------------------------------------------------------ framing --


def test_frames_roundtrip_through_arbitrary_chunks():
    frames = [(OP_FRAME, {"cmd": "DISPATCH", "evt": "READY", "data": {"v": 1, "name": "Zoë"}}),
              (OP_PING, None), (OP_CLOSE, {"code": 4000, "message": "Invalid Client ID"})]
    stream = b"".join(encode_frame(op, payload) for op, payload in frames)
    first_body = json.dumps(frames[0][1], separators=(",", ":")).encode("utf-8")
    assert stream[:8] == struct.pack("<II", OP_FRAME, len(first_body))  # LE opcode, LE length
    assert stream[8:8 + len(first_body)] == first_body
    decoder = FrameDecoder()
    got = []
    for i in range(0, len(stream), 3):
        got.extend(decoder.feed(stream[i:i + 3]))
    assert got == frames


def test_decoder_rejects_garbage():
    with pytest.raises(ProtocolError):
        FrameDecoder().feed((1).to_bytes(4, "little") + (4).to_bytes(4, "little") + b"nope")
    with pytest.raises(ProtocolError):
        FrameDecoder().feed((1).to_bytes(4, "little") + (1 << 30).to_bytes(4, "little"))


# ------------------------------------------------------------------- session --


def test_first_run_authorizes_then_reports_named_speakers(harness):
    h = harness()
    connected(h)
    d, client = h.discord, h.client

    assert d.handshakes == [{"v": 1, "client_id": CLIENT_ID}]
    authorize = d.cmds("AUTHORIZE")
    assert len(authorize) == 1
    assert authorize[0]["args"]["client_id"] == CLIENT_ID
    assert {"rpc", "rpc.voice.read"} <= set(authorize[0]["args"]["scopes"])
    assert "redirect_uri" not in authorize[0]["args"]  # only the token exchange sends it
    assert MSG_WAITING in h.statuses

    assert h.http.calls == [(TOKEN_URL, {
        "grant_type": "authorization_code", "code": "auth-code",
        "redirect_uri": "http://localhost", "client_id": CLIENT_ID, "client_secret": SECRET,
    })]
    assert [c["args"] for c in d.cmds("AUTHENTICATE")] == [{"access_token": "access-1"}]
    assert d.cmds("SUBSCRIBE", "VOICE_CHANNEL_SELECT")[0]["args"] == {}
    assert {(c["evt"], c["args"]["channel_id"]) for c in d.cmds("SUBSCRIBE")
            if c["evt"] != "VOICE_CHANNEL_SELECT"} == {(e, "100") for e in CHANNEL_EVENTS}

    # Display name: nick, else global_name, else username.
    assert client.members == {ME: "Me", "2": "Alice A", "3": "bob"}
    assert client.channel_name == "General"
    assert client.channel_id == "100"
    assert client.user_id == ME

    token = h.read_token()
    assert token["access_token"] == "access-1" and token["refresh_token"] == "refresh-1"
    assert token["client_id"] == CLIENT_ID and token["expires_at"] > time.time() + 6 * 86400

    before = time.monotonic()
    d.speak("2", True)
    d.speak("3", True)
    d.speak("2", False)
    d.speak("3", False)
    h.wait_events(4)
    assert h.simple_events() == [("2", "Alice A", True), ("3", "bob", True),
                                 ("2", "Alice A", False), ("3", "bob", False)]
    times = [e.t for e in h.events]
    assert times == sorted(times) and before <= times[0] <= time.monotonic()

    # Secrets never surface in statuses.
    assert not any(SECRET in s or "access-1" in s or "refresh-1" in s for s in h.statuses)


def test_own_speaking_is_skipped_unless_requested(harness):
    h = harness()
    connected(h)
    h.discord.speak(ME, True)
    h.discord.speak("2", True)
    h.wait_events(1)
    time.sleep(0.05)
    assert h.simple_events() == [("2", "Alice A", True)]

    h2 = harness(include_self=True, token_path=h.token_path)
    h2.discord.valid_tokens.update(h.discord.valid_tokens)
    connected(h2)
    h2.discord.speak(ME, True)
    h2.wait_events(1)
    assert h2.simple_events() == [(ME, "Me", True)]


def test_duplicate_starts_and_orphan_stops_are_dropped(harness):
    h = harness()
    connected(h)
    d = h.discord
    d.speak("2", False)  # never started
    d.speak("2", True)
    d.speak("2", True)
    d.speak("2", False)
    h.wait_events(2)
    time.sleep(0.05)
    assert h.simple_events() == [("2", "Alice A", True), ("2", "Alice A", False)]


def test_cached_token_skips_the_approval_popup(harness):
    h = harness()
    h.write_token()
    connected(h)
    assert h.discord.cmds("AUTHORIZE") == []
    assert h.http.calls == []
    authenticate = h.discord.cmds("AUTHENTICATE")
    assert [c["args"] for c in authenticate] == [{"access_token": "cached-access"}]
    assert MSG_WAITING not in h.statuses


def test_expiring_token_is_refreshed_before_authenticating(harness):
    h = harness()
    h.write_token(expires_in=60)
    connected(h)
    assert h.http.grants() == ["refresh_token"]
    assert h.http.calls[0][1]["refresh_token"] == "cached-refresh"
    assert [c["args"]["access_token"] for c in h.discord.cmds("AUTHENTICATE")] == ["access-1"]
    assert h.discord.cmds("AUTHORIZE") == []
    assert h.read_token()["refresh_token"] == "refresh-1"


def test_rejected_token_is_refreshed_on_a_new_connection(harness):
    h = harness()
    h.write_token(valid=False)  # e.g. revoked server-side but not expired locally
    connected(h)
    assert h.http.grants() == ["refresh_token"]
    assert [c["args"]["access_token"] for c in h.discord.cmds("AUTHENTICATE")] == [
        "cached-access", "access-1"]
    assert len(h.discord.transports) == 2  # never AUTHENTICATE twice on one connection
    assert h.discord.cmds("AUTHORIZE") == []


def test_dead_refresh_token_falls_back_to_the_approval_popup(harness):
    h = harness()
    h.write_token(expires_in=-10)
    h.http.valid_refresh.clear()
    connected(h)
    assert h.http.grants() == ["refresh_token", "authorization_code"]
    assert len(h.discord.cmds("AUTHORIZE")) == 1
    assert h.read_token()["access_token"] == "access-1"


def test_token_for_another_app_is_ignored(harness):
    h = harness()
    h.write_token(client_id="999")
    connected(h)
    assert len(h.discord.cmds("AUTHORIZE")) == 1
    assert h.read_token()["client_id"] == CLIENT_ID


def test_channel_switch_resubscribes_and_reloads_members(harness):
    h = harness()
    connected(h)
    d, client = h.discord, h.client
    d.speak("2", True)
    h.wait_events(1)

    d.select("200")
    h.wait_status("Connected: #Raid (2 people)")
    assert client.members == {ME: "Me", "4": "Carol (server nick)"}
    assert client.channel_name == "Raid"
    # Alice's open interval is closed when we leave her channel.
    h.wait_events(2)
    assert h.simple_events()[1] == ("2", "Alice A", False)

    assert {(c["evt"], c["args"]["channel_id"]) for c in d.cmds("UNSUBSCRIBE")} == {
        (e, "100") for e in CHANNEL_EVENTS}
    assert {(e, "200") for e in CHANNEL_EVENTS} <= d.conn.subs
    assert not any(cid == "100" for _, cid in d.conn.subs)

    d.speak("4", True)
    h.wait_events(3)
    assert h.simple_events()[2] == ("4", "Carol (server nick)", True)

    # Leaving voice entirely.
    d.select(None)
    h.wait_status("not in a voice channel")
    assert client.members == {} and client.channel_name is None and client.channel_id is None
    h.wait_events(4)
    assert h.simple_events()[3] == ("4", "Carol (server nick)", False)
    assert not any(cid == "200" for _, cid in d.conn.subs)

    d.select("100")
    h.wait_status("Connected: #General (3 people)")


def test_voice_state_events_keep_members_fresh(harness):
    h = harness()
    connected(h)
    d, client = h.discord, h.client
    d.join("100", voice_state("5", "dave", nick="Dave"))
    h.wait_status("Connected: #General (4 people)")
    assert client.members["5"] == "Dave"

    renamed = voice_state("5", "dave", nick="Dave the Brave")
    d.conn.dispatch_channel_event("VOICE_STATE_UPDATE", "100", renamed)
    wait_for(lambda: client.members.get("5") == "Dave the Brave", what="rename")

    d.speak("5", True)
    h.wait_events(1)
    assert h.simple_events() == [("5", "Dave the Brave", True)]
    d.leave("100", "5")  # leaves mid-sentence: interval closed for them
    h.wait_status("Connected: #General (3 people)")
    h.wait_events(2)
    assert h.simple_events()[1] == ("5", "Dave the Brave", False)
    assert "5" not in client.members


def test_unknown_speaker_triggers_a_member_reload(harness):
    h = harness()
    connected(h)
    h.discord.join("100", voice_state("6", "erin", global_name="Erin"), announce=False)
    h.discord.speak("6", True)
    h.wait_events(1)
    assert h.simple_events() == [("6", "Erin", True)]
    assert h.client.members["6"] == "Erin"


def test_moved_by_a_moderator_without_channel_select(harness):
    h = harness()
    connected(h)
    d = h.discord
    with d.lock:
        d.selected = "200"
    d.leave("100", ME)  # only our own VOICE_STATE_DELETE arrives
    h.wait_status("Connected: #Raid (2 people)")


def test_ping_gets_a_pong(harness):
    h = harness()
    connected(h)
    h.discord.conn.push(OP_PING, {"n": 7})
    wait_for(lambda: h.discord.conn.pongs == [{"n": 7}], what="pong")


def test_reconnects_when_discord_restarts(harness):
    h = harness()
    h.discord.running = False
    h.client.start()
    h.wait_status(MSG_NOT_RUNNING)
    h.discord.running = True
    h.wait_status("Connected: #General (3 people)")

    h.discord.speak("3", True)
    h.wait_events(1)
    h.discord.quit()
    h.wait_events(2)  # bob's interval is closed when the connection drops
    assert h.simple_events()[1] == ("3", "bob", False)
    h.wait_status(MSG_NOT_RUNNING)

    h.discord.running = True
    h.wait_status("Connected: #General (3 people)")
    assert len(h.discord.cmds("AUTHORIZE")) == 1  # second session used the cached token
    assert [c["args"]["access_token"] for c in h.discord.cmds("AUTHENTICATE")] == [
        "access-1", "access-1"]


def test_invalid_client_id_is_reported_and_not_retried(harness):
    h = harness()
    h.discord.handshake_close = {"code": 4000, "message": "Invalid Client ID"}
    h.client.start()
    h.wait_status("client ID")
    wait_for(lambda: not h.client._thread.is_alive(), what="thread exit")
    assert len(h.discord.transports) == 1


@pytest.mark.parametrize("message, status", [
    ("OAuth2 Error: access_denied: The resource owner or authorization server denied the request.",
     MSG_DECLINED),
    ("OAuth2 Error: invalid_scope: The requested scope is invalid, unknown, or malformed.",
     MSG_NOT_ALLOWED),
])
def test_authorize_failures_need_the_user(harness, message, status):
    h = harness()
    h.discord.authorize_error = (5000, message)
    h.client.start()
    h.wait_status(status)
    wait_for(lambda: not h.client._thread.is_alive(), what="thread exit")
    assert h.http.calls == []
    assert len(h.discord.cmds("AUTHORIZE")) == 1


def test_authorize_retries_without_prompt_none_if_discord_insists(harness):
    h = harness()
    h.discord.reject_prompt_none = True
    connected(h)
    prompts = [c["args"].get("prompt") for c in h.discord.cmds("AUTHORIZE")]
    assert prompts == ["none", None]
    assert len(h.discord.transports) == 1


def test_missing_portal_redirect_is_reported(harness):
    h = harness()
    h.discord.authorize_error = (5000, "OAuth2 Error: invalid_request: No redirect uri")
    h.client.start()
    h.wait_status("redirect URI")
    wait_for(lambda: not h.client._thread.is_alive(), what="thread exit")
    assert "http://localhost" in h.client.status
    assert h.http.calls == []


def test_wrong_secret_is_reported(harness):
    h = harness(client_secret="wrong")
    h.client.start()
    h.wait_status("client secret")
    wait_for(lambda: not h.client._thread.is_alive(), what="thread exit")
    assert "wrong" not in h.client.status


def test_unregistered_redirect_uri_is_reported(harness):
    h = harness(redirect_uri="http://localhost:1234")
    h.client.start()
    h.wait_status("redirect URI")
    assert "http://localhost:1234" in h.client.status


def test_network_trouble_retries_without_losing_the_token(harness):
    h = harness()
    h.write_token(expires_in=60)
    calls = []

    def offline(url, form):
        calls.append(form["grant_type"])
        raise OSError("network is down")

    h.client._http_post = offline
    h.client.start()
    h.wait_status("Can't reach discord.com")
    assert h.read_token()["refresh_token"] == "cached-refresh"
    h.client._http_post = h.http
    h.wait_status("Connected: #General (3 people)")


def test_missing_settings_are_reported(harness):
    h = harness(client_id="")
    h.client.start()
    h.wait_status("client ID not set")
    h = harness(client_id="my app")
    h.client.start()
    h.wait_status("numeric")
    h = harness(client_secret="")
    h.client.start()
    h.wait_status("secret not set")


def test_stop_is_idempotent_and_restartable(harness):
    h = harness()
    h.client.stop()  # before start
    assert h.client.status == MSG_STOPPED
    connected(h)
    h.discord.speak("2", True)
    h.wait_events(1)
    h.client.stop()
    h.client.stop()
    assert h.client.status == MSG_STOPPED
    assert not h.client._thread.is_alive()
    assert h.simple_events()[-1] == ("2", "Alice A", False)
    assert h.client.members == {}
    assert h.discord.transports[-1].closed

    h.client.start()
    h.wait_status("Connected: #General (3 people)")
    assert len(h.discord.cmds("AUTHORIZE")) == 1


def test_callbacks_that_raise_do_not_kill_the_client(harness):
    h = harness()

    def boom(_):
        raise RuntimeError("callback bug")

    h.client._on_event = boom
    connected(h)
    h.discord.speak("2", True)
    h.discord.join("100", voice_state("5", "dave"))
    h.wait_status("Connected: #General (4 people)")


def test_direct_message_call_label(harness):
    h = harness()
    h.discord.channels["300"] = channel("300", "", [voice_state(ME, "me"),
                                                    voice_state("2", "alice")], kind=1)
    h.discord.selected = "300"
    connected(h, status="Connected: Direct call (2 people)")
    assert h.client.channel_name == "Direct call"


# ------------------------------------------------------------------ timeline --


def ev(user, t, speaking=True, name=None):
    return SpeakingEvent(user, name or user.upper(), speaking, t)


def test_timeline_overlap_ranks_by_seconds():
    tl = SpeakingTimeline(clock=lambda: 100.0)
    for e in (ev("a", 10.0), ev("a", 12.0, False), ev("b", 11.0), ev("b", 11.5, False),
              ev("c", 20.0), ev("c", 21.0, False)):
        tl.add(e)
    assert tl.overlap(10.5, 13.0) == [("a", "A", 1.5), ("b", "B", 0.5)]
    assert tl.overlap(13.0, 10.5) == tl.overlap(10.5, 13.0)
    assert tl.overlap(12.0, 20.0) == []
    assert tl.overlap(0.0, 50.0) == [("a", "A", 2.0), ("c", "C", 1.0), ("b", "B", 0.5)]


def test_timeline_merges_flapping_but_not_real_pauses():
    tl = SpeakingTimeline(clock=lambda: 100.0)
    for e in (ev("a", 1.0), ev("a", 2.0, False), ev("a", 2.1), ev("a", 3.0, False),  # flap
              ev("a", 4.0), ev("a", 5.0, False)):  # 1 s pause: separate
        tl.add(e)
    assert tl.overlap(0.0, 10.0) == [("a", "A", 3.0)]  # 1.0-3.0 merged + 4.0-5.0
    assert tl.overlap(2.0, 2.1)[0][2] == pytest.approx(0.1)  # the gap counts as speech
    assert tl.overlap(3.0, 4.0) == []


def test_timeline_open_interval_runs_until_now():
    now = [10.0]
    tl = SpeakingTimeline(clock=lambda: now[0])
    tl.add(ev("a", 8.0))
    assert tl.overlap(0.0, 100.0) == [("a", "A", 2.0)]
    now[0] = 12.0
    assert tl.overlap(9.0, 11.0) == [("a", "A", 2.0)]
    assert tl.overlap(0.0, 100.0) == [("a", "A", 4.0)]
    tl.add(ev("a", 9.0))  # duplicate start is ignored
    assert tl.overlap(0.0, 100.0) == [("a", "A", 4.0)]
    tl.add(ev("b", 1.0, False))  # stop without start is ignored
    assert [row[0] for row in tl.overlap(0.0, 100.0)] == ["a"]


def test_timeline_prune_keeps_recent_and_open_intervals():
    tl = SpeakingTimeline(clock=lambda: 100.0)
    for e in (ev("a", 1.0), ev("a", 2.0, False), ev("b", 5.0), ev("b", 6.0, False),
              ev("c", 3.0)):
        tl.add(e)
    tl.prune(older_than=4.0)
    assert {row[0] for row in tl.overlap(0.0, 100.0)} == {"b", "c"}
    tl.prune(older_than=50.0)
    assert [row[0] for row in tl.overlap(0.0, 100.0)] == ["c"]
    tl.add(ev("c", 60.0, False))
    tl.prune(older_than=70.0)
    assert tl.overlap(0.0, 100.0) == []


def test_timeline_uses_latest_name_and_is_thread_safe():
    tl = SpeakingTimeline()
    tl.add(ev("a", 1.0, name="Old"))
    tl.add(ev("a", 2.0, False, name="New"))
    assert tl.overlap(0.0, 5.0) == [("a", "New", 1.0)]

    def writer(user):
        for i in range(500):
            tl.add(ev(user, 10.0 + i, True))
            tl.add(ev(user, 10.5 + i, False))

    threads = [threading.Thread(target=writer, args=(u,)) for u in "xyz"]
    for t in threads:
        t.start()
    for _ in range(200):
        tl.overlap(0.0, 1000.0)
        tl.prune(0.0)
    for t in threads:
        t.join()
    totals = {uid: secs for uid, _, secs in tl.overlap(0.0, 1000.0)}
    assert totals == {"a": 1.0, "x": 250.0, "y": 250.0, "z": 250.0}


# ------------------------------------------------------------ real pipe I/O --


@pytest.mark.skipif(sys.platform != "win32", reason="named pipes are Windows-only")
def test_pipe_transport_against_a_local_pipe_server():
    from multiprocessing.connection import Listener

    address = rf"\\.\pipe\voicerhino-test-{uuid.uuid4().hex}"
    listener = Listener(address, family="AF_PIPE")
    big = {"cmd": "DISPATCH", "evt": "BIG", "data": {"blob": "x" * 200_000}, "nonce": None}
    seen = {}

    def serve():
        conn = listener.accept()
        seen["handshake"] = conn.recv_bytes()
        conn.send_bytes(encode_frame(OP_FRAME, {"cmd": "DISPATCH", "evt": "READY", "data": {}}))
        frame = encode_frame(OP_FRAME, big)
        conn.send_bytes(frame[:5])  # split mid-header and mid-body
        conn.send_bytes(frame[5:70_000])
        conn.send_bytes(frame[70_000:])
        seen["pong"] = conn.recv_bytes()
        conn.close()

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    transport = PipeTransport.open(address)
    try:
        transport.send(OP_HANDSHAKE, {"v": 1, "client_id": CLIENT_ID})
        assert transport.recv(5.0) == (OP_FRAME, {"cmd": "DISPATCH", "evt": "READY", "data": {}})
        assert transport.recv(5.0) == (OP_FRAME, big)
        assert transport.recv(0.05) is None
        transport.send(OP_PONG, {"n": 1})
        server.join(5.0)
        with pytest.raises(TransportClosed):
            for _ in range(100):
                transport.recv(0.05)
    finally:
        transport.close()
        transport.close()
        listener.close()
    handshake = (OP_HANDSHAKE, {"v": 1, "client_id": CLIENT_ID})
    assert FrameDecoder().feed(seen["handshake"]) == [handshake]
    assert FrameDecoder().feed(seen["pong"]) == [(OP_PONG, {"n": 1})]


@pytest.mark.skipif(sys.platform != "win32", reason="named pipes are Windows-only")
def test_pipe_transport_reports_missing_pipe():
    with pytest.raises(DiscordNotRunning):
        PipeTransport.open(rf"\\.\pipe\voicerhino-missing-{uuid.uuid4().hex}")
