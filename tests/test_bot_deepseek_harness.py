"""Tests for the DeepSeek Harness desktop provider (ACP over stdio).

Two layers:

* pure unit tests for the overlay/protocol helpers, and
* protocol tests that drive :class:`AcpTransport` against a *fake ACP agent*
  subprocess, which is the only way to cover the parts that matter and cannot
  be proven by unit tests alone: interleaved notifications, an agent-initiated
  permission request while a prompt is in flight (the deadlock case), frame
  ordering, EOF mid-turn and cancellation.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys

import pytest
import yaml

from ComfyTV.bot import deepseek_harness as dh
from ComfyTV.bot._acp import AcpError, AcpTransport
from ComfyTV.bot.providers import (
    AgentProvider,
    BotEvent,
    ProviderCaps,
    ProviderStatus,
    TurnHandle,
    TurnRequest,
    TurnResult,
    register_provider,
)
from conftest import wait_bot_done as _wait_done

# --------------------------------------------------------------------------
# fake ACP agent
# --------------------------------------------------------------------------

#: Event-driven fake agent.  Behaviour is driven by the JSON in
#: ``FAKE_ACP_CONFIG``; every frame it receives or sends that a test cares about
#: is appended to the file named by ``FAKE_ACP_SEEN``.
_FAKE_AGENT = r'''
import json, os, sys, uuid

def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def seen(kind, payload):
    path = os.environ.get("FAKE_ACP_SEEN")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(kind + ":" + json.dumps(payload) + "\n")

cfg = json.loads(os.environ.get("FAKE_ACP_CONFIG") or "{}")
session_id = cfg.get("sessionId") or uuid.uuid4().hex
if cfg.get("noise"):
    sys.stdout.write("this is not json\n")
    sys.stdout.flush()

pending_prompt = None
pending_permission = None

def notify(update):
    out({"jsonrpc": "2.0", "method": "session/update",
         "params": {"sessionId": session_id, "update": update}})

def settle_prompt():
    global pending_prompt
    if pending_prompt is None:
        return
    out({"jsonrpc": "2.0", "id": pending_prompt,
         "result": {"stopReason": cfg.get("stopReason", "end_turn")}})
    pending_prompt = None

for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        out({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": 1,
            "agentInfo": {"name": "fake-acp", "version": "0.0.1"},
            "agentCapabilities": {
                "mcpCapabilities": {"http": True},
                "promptCapabilities": {
                    "image": bool(cfg.get("image")),
                    "audio": False, "embeddedContext": False},
                "sessionCapabilities": {"close": {}, "list": {}, "resume": {}},
            },
            "authMethods": [],
        }})
        if cfg.get("eofAfterInitialize"):
            sys.exit(0)
        continue

    if method == "session/new":
        seen("new", params)
        if cfg.get("newError"):
            out({"jsonrpc": "2.0", "id": mid,
                 "error": {"code": -32602, "message": cfg["newError"]}})
            continue
        out({"jsonrpc": "2.0", "id": mid, "result": {
            "sessionId": session_id,
            "configOptions": cfg.get("configOptions") or []}})
        continue

    if method == "session/resume":
        seen("resume", params)
        if cfg.get("resumeError"):
            out({"jsonrpc": "2.0", "id": mid,
                 "error": {"code": -32602, "message": cfg["resumeError"]}})
            continue
        out({"jsonrpc": "2.0", "id": mid, "result": {
            "configOptions": cfg.get("configOptions") or []}})
        continue

    if method == "session/set_config_option":
        seen("set_config_option", params)
        out({"jsonrpc": "2.0", "id": mid, "result": {
            "configOptions": cfg.get("configOptions") or []}})
        continue

    if method == "session/close":
        seen("close", params)
        out({"jsonrpc": "2.0", "id": mid, "result": {}})
        continue

    if method == "session/prompt":
        seen("prompt", params)
        for update in cfg.get("updates") or []:
            notify(update)
        pending_prompt = mid
        permission = cfg.get("permission")
        if permission:
            pending_permission = 9001
            out({"jsonrpc": "2.0", "id": 9001,
                 "method": "session/request_permission",
                 "params": permission})
        elif not cfg.get("hangOnPrompt"):
            settle_prompt()
        continue

    if method == "session/cancel":
        seen("cancel", params)
        if cfg.get("stopReason") is None:
            cfg["stopReason"] = "cancelled"
        settle_prompt()
        continue

    # response to the permission request we sent
    if mid == pending_permission:
        seen("permission_response", msg)
        pending_permission = None
        if cfg.get("updatesAfterPermission"):
            for update in cfg["updatesAfterPermission"]:
                notify(update)
        settle_prompt()
        continue

    if mid is not None:
        out({"jsonrpc": "2.0", "id": mid,
             "error": {"code": -32601, "message": "method not found"}})
        continue
'''


def _write_fake_agent(tmp_path) -> str:
    path = tmp_path / "fake_acp_agent.py"
    path.write_text(_FAKE_AGENT, encoding="utf-8")
    return str(path)


def _config_options(account: bool = True, official: bool = True) -> list[dict]:
    groups = []
    if official:
        groups.append({
            "group": "deepseek-official", "name": "DeepSeek",
            "options": [
                {"value": '["deepseek-official","deepseek-v4-flash"]',
                 "name": "deepseek-v4-flash"},
                {"value": '["deepseek-official","deepseek-v4-pro"]',
                 "name": "DeepSeek-V4-Pro"},
            ],
        })
    if account:
        groups.append({
            "group": "deepseek-account", "name": "DeepSeek Account",
            "options": [
                {"value": '["deepseek-account","deepseek-flash"]',
                 "name": "DeepSeek-V41-Flash"},
                {"value": '["deepseek-account","deepseek-v4-pro"]',
                 "name": "DeepSeek-V4-Pro"},
            ],
        })
    return [
        {"id": "model", "name": "Model", "category": "model", "type": "select",
         "currentValue": '["deepseek-official","deepseek-v4-flash"]',
         "options": groups},
        {"id": "reasoning_effort", "name": "Reasoning effort",
         "category": "thought_level", "type": "select", "currentValue": "high",
         "options": [{"value": "off", "name": "Off"},
                     {"value": "high", "name": "High"}]},
    ]


@pytest.fixture()
def fake_runtime(tmp_path, monkeypatch):
    """Point the provider at the fake agent instead of the desktop app."""
    agent = _write_fake_agent(tmp_path)
    seen = tmp_path / "seen.txt"
    monkeypatch.setattr(dh, "resolve_launcher",
                        lambda: ([sys.executable, agent], ""))

    async def _no_profile(_launcher):
        return ""

    monkeypatch.setattr(dh, "ensure_profile", _no_profile)
    # Keep every provider write inside tmp_path instead of the repo/test user dir.
    monkeypatch.setattr(dh, "bot_home", lambda: tmp_path / "bot-home")

    def configure(monkeypatch, **cfg):
        import os
        # The real runtime always answers session/new with a model catalog, so
        # default to one unless a test is specifically probing its absence.
        cfg.setdefault("configOptions", _config_options())
        monkeypatch.setenv("FAKE_ACP_CONFIG", json.dumps(cfg))
        monkeypatch.setenv("FAKE_ACP_SEEN", str(seen))
        return seen

    return configure


def _events_collector():
    events: list[BotEvent] = []

    async def emit(ev: BotEvent) -> None:
        events.append(ev)

    return events, emit


async def _run_turn(provider, turn, emit, handle=None):
    handle = handle or TurnHandle()
    return await provider.send(turn, emit, handle), handle


def _seen_entries(path) -> list[tuple[str, dict]]:
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        kind, _, payload = line.partition(":")
        out.append((kind, json.loads(payload)))
    return out


# --------------------------------------------------------------------------
# overlay rendering (the overlay is a hard runtime dependency, so the emitter
# is validated by parsing its own output)
# --------------------------------------------------------------------------


class TestOverlay:
    def test_round_trips_through_yaml(self):
        text = dh.render_overlay(("deepseek-account", "deepseek-v4-pro"))
        parsed = yaml.safe_load(text)
        assert isinstance(parsed, list)
        by_id = {entry["id"]: entry for entry in parsed}
        assert by_id["acp"]["config"] == {
            "provider": "deepseek-account", "model": "deepseek-v4-pro"}
        assert by_id["permission"]["config"]["defaultPreset"] == \
            dh.BOT_PERMISSION_PRESET
        preset = by_id["permission"]["config"]["presets"][dh.BOT_PERMISSION_PRESET]
        assert preset == {"sandbox": "danger-full-access", "approval": "never"}

    def test_disables_every_builtin_tool_entry(self):
        parsed = yaml.safe_load(dh.render_overlay(None))
        disabled = {entry["id"] for entry in parsed if entry.get("disabled")}
        assert disabled == set(dh._DISABLED_ENTRY_IDS)
        # The MCP client is mounted by dsh-acp itself, so no entry here may
        # disable the agent loop or the tool service.
        for keep in ("acp", "tools", "agent-loop", "mcp-resources"):
            assert keep not in disabled

    def test_route_is_optional(self):
        parsed = yaml.safe_load(dh.render_overlay(None))
        assert "acp" not in {entry["id"] for entry in parsed}

    def test_scalars_are_quoted_when_needed(self):
        assert dh._yaml_scalar("plain-value") == "plain-value"
        assert dh._yaml_scalar(True) == "true"
        assert dh._yaml_scalar("has space") == '"has space"'


# --------------------------------------------------------------------------
# model values, usage, tool content
# --------------------------------------------------------------------------


class TestModelValues:
    def test_parses_the_observed_runtime_value(self):
        assert dh.parse_model_value('["deepseek-official","deepseek-v4-flash"]') \
            == ("deepseek-official", "deepseek-v4-flash")

    @pytest.mark.parametrize("value", [
        "", "nonsense", '{"a":1}', '["only-one"]', '["a","b","c"]',
        '["","b"]', '["a",""]', "[1,2]", "null",
    ])
    def test_rejects_anything_not_a_provider_model_pair(self, value):
        assert dh.parse_model_value(value) is None

    def test_flattens_grouped_and_flat_options(self):
        options = dh._model_options_from(_config_options())
        assert [o.value for o in options] == [
            '["deepseek-official","deepseek-v4-flash"]',
            '["deepseek-official","deepseek-v4-pro"]',
            '["deepseek-account","deepseek-flash"]',
            '["deepseek-account","deepseek-v4-pro"]',
        ]
        assert options[-1].group == "deepseek-account"
        assert options[-1].label == "DeepSeek-V4-Pro"

    def test_tolerates_missing_or_odd_config_options(self):
        assert dh._model_options_from(None) == []
        assert dh._model_options_from([{"id": "other"}]) == []

    def test_usage_maps_acp_counters(self):
        assert dh._usage_from_acp({"inputTokens": 5, "outputTokens": 7,
                                   "totalTokens": 12}) == {
            "input_tokens": 5, "output_tokens": 7, "total_tokens": 12}
        assert dh._usage_from_acp(None) is None
        assert dh._usage_from_acp({}) is None

    def test_tool_result_text_handles_text_diffs_and_bare_blocks(self):
        assert dh._tool_result_text([
            {"type": "content", "content": {"type": "text", "text": "done"}},
        ]) == "done"
        assert dh._tool_result_text([{"type": "diff", "path": "a.json"}]) == \
            "[diff a.json]"
        assert dh._tool_result_text([{"type": "text", "text": "bare"}]) == "bare"
        assert dh._tool_result_text("plain") == "plain"
        assert dh._tool_result_text(None) == ""


# --------------------------------------------------------------------------
# auth routing: the account route must never silently become the API key
# --------------------------------------------------------------------------


class TestRouting:
    def _provider(self, monkeypatch, mode: str, catalog=()):
        monkeypatch.setattr(dh, "_setting",
                            lambda key, default="": mode
                            if key == dh.SETTING_AUTH_MODE else default)
        provider = dh.DeepSeekHarnessProvider()
        provider._catalog = list(catalog)
        return provider

    def test_account_mode_rejects_a_saved_official_value(self, monkeypatch):
        provider = self._provider(monkeypatch, dh.AUTH_DESKTOP)
        route, error = provider._resolve_route(
            '["deepseek-official","deepseek-v4-pro"]')
        assert route is None
        assert "auth mode" in error

    def test_api_key_mode_accepts_the_official_value(self, monkeypatch):
        provider = self._provider(monkeypatch, dh.AUTH_API_KEY)
        assert provider._resolve_route('["deepseek-official","deepseek-v4-pro"]') \
            == (("deepseek-official", "deepseek-v4-pro"), "")

    def test_stale_value_is_reported_not_swapped(self, monkeypatch):
        catalog = dh._model_options_from(_config_options())
        provider = self._provider(monkeypatch, dh.AUTH_DESKTOP, catalog)
        route, error = provider._resolve_route('["deepseek-account","gone"]')
        assert route is None
        assert "no longer offered" in error

    def test_empty_value_means_let_the_catalog_decide(self, monkeypatch):
        provider = self._provider(monkeypatch, dh.AUTH_DESKTOP)
        assert provider._resolve_route("") == (None, "")

    def test_default_route_picks_the_account_group(self, monkeypatch):
        catalog = dh._model_options_from(_config_options())
        provider = self._provider(monkeypatch, dh.AUTH_DESKTOP, catalog)
        assert provider._default_route() == \
            (("deepseek-account", "deepseek-flash"), "")

    def test_missing_account_group_fails_instead_of_using_official(self, monkeypatch):
        catalog = dh._model_options_from(_config_options(account=False))
        provider = self._provider(monkeypatch, dh.AUTH_DESKTOP, catalog)
        route, error = provider._default_route()
        assert route is None
        assert "will not fall back" in error
        # and the official route is right there and still not used
        assert any(o.group == "deepseek-official" for o in catalog)


# --------------------------------------------------------------------------
# protocol, against the fake agent
# --------------------------------------------------------------------------


class TestProtocol:
    async def test_happy_path_streams_text_and_tool_lifecycle(
            self, tmp_path, fake_runtime, monkeypatch):
        fake_runtime(monkeypatch, configOptions=_config_options(), updates=[
            {"sessionUpdate": "agent_message_chunk",
             "content": {"type": "text", "text": "hello "}},
            {"sessionUpdate": "tool_call", "toolCallId": "t1",
             "title": "server_info", "name": "mcp__comfytv__server_info",
             "rawInput": {"a": 1}},
            {"sessionUpdate": "tool_call_update", "toolCallId": "t1",
             "status": "completed",
             "content": [{"type": "content",
                          "content": {"type": "text", "text": "ok"}}]},
            {"sessionUpdate": "agent_message_chunk",
             "content": {"type": "text", "text": "world"}},
        ])
        provider = dh.DeepSeekHarnessProvider()
        events, emit = _events_collector()
        result, _handle = await _run_turn(
            provider,
            TurnRequest(chat_id="c1", user_text="hi",
                        model='["deepseek-account","deepseek-v4-pro"]'),
            emit)
        assert result.error == ""
        assert result.resume_token
        # the internal session event always precedes anything the prompt emits
        assert [(e.t, e.text or e.name) for e in events] == [
            ("session", ""),
            ("delta", "hello "),
            ("tool_use", "mcp__comfytv__server_info"),
            ("tool_result", "ok"),
            ("delta", "world"),
        ]
        tool_use = next(e for e in events if e.t == "tool_use")
        assert tool_use.input == {"a": 1}
        assert tool_use.id == "t1"
        tool_result = next(e for e in events if e.t == "tool_result")
        assert tool_result.text == "ok"
        assert tool_result.is_error is False

    async def test_tool_call_update_without_a_prior_tool_call_still_reports(
            self, tmp_path, fake_runtime, monkeypatch):
        fake_runtime(monkeypatch, updates=[
            {"sessionUpdate": "tool_call_update", "toolCallId": "t9",
             "name": "mcp__comfytv__get_canvas", "status": "completed",
             "content": [{"type": "content",
                          "content": {"type": "text", "text": "canvas"}}]},
        ])
        provider = dh.DeepSeekHarnessProvider()
        events, emit = _events_collector()
        await _run_turn(provider, TurnRequest(chat_id="c1", user_text="hi"), emit)
        assert [e.t for e in events] == ["session", "tool_result"]
        assert events[1].name == "mcp__comfytv__get_canvas"

    async def test_failed_tool_is_flagged(self, tmp_path, fake_runtime, monkeypatch):
        fake_runtime(monkeypatch, updates=[
            {"sessionUpdate": "tool_call", "toolCallId": "t2",
             "name": "mcp__comfytv__run_stage"},
            {"sessionUpdate": "tool_call_update", "toolCallId": "t2",
             "status": "failed",
             "content": [{"type": "content",
                          "content": {"type": "text", "text": "boom"}}]},
        ])
        provider = dh.DeepSeekHarnessProvider()
        events, emit = _events_collector()
        await _run_turn(provider, TurnRequest(chat_id="c1", user_text="hi"), emit)
        result_event = next(e for e in events if e.t == "tool_result")
        assert result_event.is_error is True
        assert result_event.text == "boom"

    async def test_permission_request_is_answered_without_deadlock(
            self, tmp_path, fake_runtime, monkeypatch):
        """The agent blocks its prompt on our reply, so a passing turn proves
        the reader stayed alive while the prompt future was pending."""
        seen = fake_runtime(monkeypatch, configOptions=_config_options(),
                            updates=[
            {"sessionUpdate": "tool_call", "toolCallId": "t3",
             "name": "mcp__comfytv__run_stage"},
        ], permission={
            "sessionId": "s", "toolCall": {"toolCallId": "t3"},
            "options": [
                {"optionId": "allow-1", "name": "Allow", "kind": "allow_once"},
                {"optionId": "deny-1", "name": "Deny", "kind": "reject_once"},
            ],
        })
        provider = dh.DeepSeekHarnessProvider()
        events, emit = _events_collector()
        result, _handle = await _run_turn(
            provider, TurnRequest(chat_id="c1", user_text="hi"), emit)
        assert result.error == ""
        responses = [p for kind, p in _seen_entries(seen)
                     if kind == "permission_response"]
        assert responses, "the agent never received a permission reply"
        assert responses[0]["result"]["outcome"] == {
            "outcome": "selected", "optionId": "allow-1"}

    async def test_unknown_tool_permission_is_refused(self, tmp_path,
                                                      fake_runtime, monkeypatch):
        seen = fake_runtime(monkeypatch, updates=[
            {"sessionUpdate": "tool_call", "toolCallId": "t4",
             "name": "some_other_tool"},
        ], permission={
            "sessionId": "s", "toolCall": {"toolCallId": "t4"},
            "options": [
                {"optionId": "allow-1", "name": "Allow", "kind": "allow_once"},
                {"optionId": "deny-1", "name": "Deny", "kind": "reject_once"},
            ],
        })
        provider = dh.DeepSeekHarnessProvider()
        _events, emit = _events_collector()
        await _run_turn(provider, TurnRequest(chat_id="c1", user_text="hi"), emit)
        responses = [p for kind, p in _seen_entries(seen)
                     if kind == "permission_response"]
        assert responses[0]["result"]["outcome"] == {
            "outcome": "selected", "optionId": "deny-1"}

    async def test_permission_without_a_known_tool_call_is_refused(
            self, tmp_path, fake_runtime, monkeypatch):
        """Fail closed: a permission request naming a callId we never saw must
        not be auto-allowed."""
        seen = fake_runtime(monkeypatch, permission={
            "sessionId": "s", "toolCall": {"toolCallId": "ghost"},
            "options": [
                {"optionId": "allow-1", "name": "Allow", "kind": "allow_once"},
                {"optionId": "deny-1", "name": "Deny", "kind": "reject_once"},
            ],
        })
        provider = dh.DeepSeekHarnessProvider()
        _events, emit = _events_collector()
        await _run_turn(provider, TurnRequest(chat_id="c1", user_text="hi"), emit)
        responses = [p for kind, p in _seen_entries(seen)
                     if kind == "permission_response"]
        assert responses[0]["result"]["outcome"]["optionId"] == "deny-1"

    async def test_non_protocol_stdout_does_not_break_the_reader(
            self, tmp_path, fake_runtime, monkeypatch):
        fake_runtime(monkeypatch, noise=True, updates=[
            {"sessionUpdate": "agent_message_chunk",
             "content": {"type": "text", "text": "still here"}},
        ])
        provider = dh.DeepSeekHarnessProvider()
        events, emit = _events_collector()
        result, _handle = await _run_turn(
            provider, TurnRequest(chat_id="c1", user_text="hi"), emit)
        assert result.error == ""
        assert [e.text for e in events if e.t == "delta"] == ["still here"]

    async def test_eof_mid_turn_is_an_error_not_a_success(
            self, tmp_path, fake_runtime, monkeypatch):
        fake_runtime(monkeypatch, eofAfterInitialize=True)
        provider = dh.DeepSeekHarnessProvider()
        _events, emit = _events_collector()
        result, _handle = await _run_turn(
            provider, TurnRequest(chat_id="c1", user_text="hi"), emit)
        assert result.error
        assert not result.aborted
        assert "session/new" in result.error

    async def test_resume_failure_does_not_silently_start_a_new_session(
            self, tmp_path, fake_runtime, monkeypatch):
        seen = fake_runtime(monkeypatch,
                            resumeError="session/resume cwd mismatch")
        provider = dh.DeepSeekHarnessProvider()
        _events, emit = _events_collector()
        result, _handle = await _run_turn(
            provider,
            TurnRequest(chat_id="c1", user_text="hi", resume_token="old-session"),
            emit)
        assert "could not be restored" in result.error
        kinds = [kind for kind, _ in _seen_entries(seen)]
        assert "resume" in kinds
        assert "prompt" not in kinds      # nothing ran
        assert "new" not in kinds          # and no silent fresh session

    async def test_saved_model_is_applied_before_prompting(
            self, tmp_path, fake_runtime, monkeypatch):
        seen = fake_runtime(monkeypatch, configOptions=_config_options())
        provider = dh.DeepSeekHarnessProvider()
        _events, emit = _events_collector()
        result, _handle = await _run_turn(
            provider,
            TurnRequest(chat_id="c1", user_text="hi",
                        model='["deepseek-account","deepseek-v4-pro"]'),
            emit)
        assert result.error == ""
        entries = _seen_entries(seen)
        kinds = [kind for kind, _ in entries]
        assert kinds.index("set_config_option") < kinds.index("prompt")
        applied = dict(entries)["set_config_option"]
        assert applied["configId"] == "model"
        assert applied["value"] == '["deepseek-account","deepseek-v4-pro"]'

    async def test_missing_account_route_fails_before_prompting(
            self, tmp_path, fake_runtime, monkeypatch):
        seen = fake_runtime(
            monkeypatch, configOptions=_config_options(account=False))
        provider = dh.DeepSeekHarnessProvider()
        _events, emit = _events_collector()
        result, _handle = await _run_turn(
            provider, TurnRequest(chat_id="c1", user_text="hi"), emit)
        assert "will not fall back" in result.error
        assert "prompt" not in [kind for kind, _ in _seen_entries(seen)]

    async def test_session_token_is_emitted_before_the_prompt(
            self, tmp_path, fake_runtime, monkeypatch):
        fake_runtime(monkeypatch, sessionId="sess-42")
        provider = dh.DeepSeekHarnessProvider()
        events, emit = _events_collector()
        result, _handle = await _run_turn(
            provider, TurnRequest(chat_id="c1", user_text="hi"), emit)
        assert result.resume_token == "sess-42"
        assert [e.id for e in events if e.t == "session"] == ["sess-42"]

    async def test_session_id_persistence_failure_aborts_before_prompting(
            self, tmp_path, fake_runtime, monkeypatch):
        seen = fake_runtime(monkeypatch)
        provider = dh.DeepSeekHarnessProvider()

        async def emit(ev: BotEvent) -> None:
            if ev.t == "session":
                raise RuntimeError("db is down")

        result, _handle = await _run_turn(
            provider, TurnRequest(chat_id="c1", user_text="hi"), emit)
        assert "could not record" in result.error
        assert "prompt" not in [kind for kind, _ in _seen_entries(seen)]

    async def test_cancel_settles_the_turn_as_aborted(
            self, tmp_path, fake_runtime, monkeypatch):
        fake_runtime(monkeypatch, hangOnPrompt=True, updates=[
            {"sessionUpdate": "tool_call", "toolCallId": "t5",
             "name": "mcp__comfytv__run_stage"},
        ])
        monkeypatch.setattr(dh, "CANCEL_GRACE_S", 2.0)
        provider = dh.DeepSeekHarnessProvider()
        started = asyncio.Event()
        events: list[BotEvent] = []

        async def emit(ev: BotEvent) -> None:
            events.append(ev)
            if ev.t == "tool_use":
                started.set()

        handle = TurnHandle()
        task = asyncio.create_task(provider.send(
            TurnRequest(chat_id="c1", user_text="hi"), emit, handle))
        await asyncio.wait_for(started.wait(), timeout=10)
        await provider.stop(handle)
        result = await asyncio.wait_for(task, timeout=20)
        assert result.aborted is True

    async def test_mcp_endpoint_and_cwd_reach_the_runtime(
            self, tmp_path, fake_runtime, monkeypatch):
        seen = fake_runtime(monkeypatch)
        provider = dh.DeepSeekHarnessProvider()
        _events, emit = _events_collector()
        await _run_turn(
            provider,
            TurnRequest(chat_id="chat-abc", user_text="hi",
                        mcp_endpoint="http://127.0.0.1:9999/comfytv/mcp?bot_chat=chat-abc"),
            emit)
        sent = dict(_seen_entries(seen))["new"]
        assert sent["mcpServers"] == [{
            "type": "http", "name": "comfytv",
            "url": "http://127.0.0.1:9999/comfytv/mcp?bot_chat=chat-abc",
            "headers": [],
        }]
        assert sent["cwd"] == str(dh.chat_cwd("chat-abc"))

    async def test_image_is_refused_when_the_runtime_says_no(
            self, tmp_path, fake_runtime, monkeypatch):
        seen = fake_runtime(monkeypatch, image=False)
        provider = dh.DeepSeekHarnessProvider()
        _events, emit = _events_collector()
        result, _handle = await _run_turn(
            provider,
            TurnRequest(chat_id="c1", user_text="look",
                        attachments=[{"media_type": "image/png",
                                      "data": "AAAA"}]),
            emit)
        assert "does not accept image input" in result.error
        assert "prompt" not in [kind for kind, _ in _seen_entries(seen)]

    async def test_image_is_sent_when_supported(self, tmp_path, fake_runtime,
                                                monkeypatch):
        seen = fake_runtime(monkeypatch, image=True)
        provider = dh.DeepSeekHarnessProvider()
        _events, emit = _events_collector()
        result, _handle = await _run_turn(
            provider,
            TurnRequest(chat_id="c1", user_text="look",
                        attachments=[{"media_type": "image/png",
                                      "data": "AAAA"}]),
            emit)
        assert result.error == ""
        blocks = dict(_seen_entries(seen))["prompt"]["prompt"]
        assert blocks == [{"type": "image", "mimeType": "image/png",
                           "data": "AAAA"},
                          {"type": "text", "text": "look"}]

    async def test_non_image_attachment_is_refused(self, tmp_path, fake_runtime,
                                                   monkeypatch):
        fake_runtime(monkeypatch, image=True)
        provider = dh.DeepSeekHarnessProvider()
        _events, emit = _events_collector()
        result, _handle = await _run_turn(
            provider,
            TurnRequest(chat_id="c1", user_text="look",
                        attachments=[{"media_type": "video/mp4",
                                      "data": "AAAA"}]),
            emit)
        assert "text and image prompts" in result.error

    async def test_stop_reason_surfaces_as_an_error(self, tmp_path, fake_runtime,
                                                    monkeypatch):
        fake_runtime(monkeypatch, stopReason="max_tokens")
        provider = dh.DeepSeekHarnessProvider()
        _events, emit = _events_collector()
        result, _handle = await _run_turn(
            provider, TurnRequest(chat_id="c1", user_text="hi"), emit)
        assert "output token limit" in result.error
        assert not result.aborted

    async def test_cancelled_stop_reason_is_aborted_not_an_error(
            self, tmp_path, fake_runtime, monkeypatch):
        fake_runtime(monkeypatch, stopReason="cancelled")
        provider = dh.DeepSeekHarnessProvider()
        _events, emit = _events_collector()
        result, _handle = await _run_turn(
            provider, TurnRequest(chat_id="c1", user_text="hi"), emit)
        assert result.aborted is True
        assert result.error == ""

    async def test_session_is_closed_after_the_turn(self, tmp_path, fake_runtime,
                                                    monkeypatch):
        seen = fake_runtime(monkeypatch, sessionId="sess-close")
        provider = dh.DeepSeekHarnessProvider()
        _events, emit = _events_collector()
        await _run_turn(provider, TurnRequest(chat_id="c1", user_text="hi"), emit)
        closed = [p for kind, p in _seen_entries(seen) if kind == "close"]
        assert closed and closed[0]["sessionId"] == "sess-close"


# --------------------------------------------------------------------------
# transport-level details
# --------------------------------------------------------------------------


class TestTransport:
    async def test_error_response_becomes_AcpError(self, tmp_path, monkeypatch):
        agent = _write_fake_agent(tmp_path)
        monkeypatch.setenv("FAKE_ACP_CONFIG", json.dumps({}))
        transport = AcpTransport([sys.executable, agent], cwd=str(tmp_path),
                                 env={"FAKE_ACP_CONFIG": json.dumps({})})
        await transport.start()
        try:
            await transport.request("initialize", {"protocolVersion": 1})
            with pytest.raises(AcpError) as info:
                await transport.request("session/nope", {})
            assert info.value.code == -32601
        finally:
            await transport.close(5)
            await transport.dispose()

    async def test_out_of_order_responses_resolve_the_right_futures(
            self, tmp_path, monkeypatch):
        """Responses are matched by id, so two in-flight requests cannot swap."""
        agent = _write_fake_agent(tmp_path)
        transport = AcpTransport([sys.executable, agent], cwd=str(tmp_path),
                                 env={"FAKE_ACP_CONFIG": json.dumps({})})
        await transport.start()
        try:
            first, second = await asyncio.gather(
                transport.request("initialize", {"protocolVersion": 1}),
                transport.request("session/new", {"cwd": str(tmp_path),
                                                  "mcpServers": []}),
            )
            assert first["agentInfo"]["name"] == "fake-acp"
            assert second["sessionId"]
        finally:
            await transport.close(5)
            await transport.dispose()

    async def test_unsupported_client_method_is_refused(self):
        """Anything other than a permission request is refused explicitly."""
        provider = dh.DeepSeekHarnessProvider()
        with pytest.raises(AcpError):
            await provider._handle_request("fs/read_text_file", {}, {})


# --------------------------------------------------------------------------
# profile provisioning
# --------------------------------------------------------------------------


class TestProvisioning:
    async def test_provisions_once_and_reports_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dh, "dsh_home", lambda: tmp_path)
        calls = []

        async def _fake_exec(*argv, **kwargs):
            calls.append(argv)

            class _Proc:
                returncode = 0

                async def communicate(self):
                    return b"", b""

            # emulate the runtime creating the profile as a side effect
            (tmp_path / "profiles" / dh.BOT_PROFILE).mkdir(parents=True)
            (tmp_path / "profiles" / dh.BOT_PROFILE / "package.json").write_text(
                "{}", encoding="utf-8")
            return _Proc()

        monkeypatch.setattr(dh.asyncio, "create_subprocess_exec", _fake_exec)
        assert await dh.ensure_profile(["/bin/true"]) == ""
        assert "--from-default-profile" in calls[0]
        assert "--dump-config" in calls[0]
        assert (tmp_path / "profiles" / dh.BOT_PROFILE /
                "cordis.patch.yml").is_file()
        # already provisioned: no second boot
        assert await dh.ensure_profile(["/bin/true"]) == ""
        assert len(calls) == 1

    async def test_missing_profile_is_reported(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dh, "dsh_home", lambda: tmp_path)

        async def _fake_exec(*argv, **kwargs):
            class _Proc:
                returncode = 1

                async def communicate(self):
                    return b"", b"boom"

            return _Proc()

        monkeypatch.setattr(dh.asyncio, "create_subprocess_exec", _fake_exec)
        error = await dh.ensure_profile(["/bin/true"])
        assert dh.BOT_PROFILE in error


# --------------------------------------------------------------------------
# launcher discovery
# --------------------------------------------------------------------------


class TestLauncherDiscovery:
    def test_finds_the_bundled_runtime_and_keeps_spaces(self, tmp_path, monkeypatch):
        # Mirrors the real bundle: the executable is a real file, and the
        # runtime sits inside app.asar, which plain Python cannot stat through.
        app = tmp_path / "DeepSeek Harness.app"
        exe = app / dh.APP_EXECUTABLE_REL
        asar = app / dh.APP_ASAR_REL
        exe.parent.mkdir(parents=True)
        exe.write_text("", encoding="utf-8")
        asar.parent.mkdir(parents=True, exist_ok=True)
        asar.write_text("asar", encoding="utf-8")
        monkeypatch.setattr(dh, "app_candidates", lambda: [app])
        monkeypatch.setattr(dh.shutil, "which", lambda name: None)
        argv, reason = dh.resolve_launcher()
        assert reason == ""
        assert argv == [str(exe), str(app / dh.APP_LAUNCHER_REL)]
        assert " " in argv[0]        # path with spaces survives intact

    def test_reports_a_broken_bundle(self, tmp_path, monkeypatch):
        app = tmp_path / "DeepSeek Harness.app"
        app.mkdir()
        monkeypatch.setattr(dh, "app_candidates", lambda: [app])
        monkeypatch.setattr(dh.shutil, "which", lambda name: None)
        argv, reason = dh.resolve_launcher()
        assert argv is None
        assert "executable" in reason

    def test_reports_a_bundle_without_the_runtime(self, tmp_path, monkeypatch):
        app = tmp_path / "DeepSeek Harness.app"
        exe = app / dh.APP_EXECUTABLE_REL
        exe.parent.mkdir(parents=True)
        exe.write_text("", encoding="utf-8")
        monkeypatch.setattr(dh, "app_candidates", lambda: [app])
        monkeypatch.setattr(dh.shutil, "which", lambda name: None)
        argv, reason = dh.resolve_launcher()
        assert argv is None
        assert "does not bundle" in reason

    def test_tolerates_the_unstattable_asar_launcher_path(self, tmp_path,
                                                          monkeypatch):
        """`app.asar/...` is invisible to Python; discovery must not need it."""
        app = tmp_path / "DeepSeek Harness.app"
        exe = app / dh.APP_EXECUTABLE_REL
        exe.parent.mkdir(parents=True)
        exe.write_text("", encoding="utf-8")
        (app / dh.APP_ASAR_REL).parent.mkdir(parents=True, exist_ok=True)
        (app / dh.APP_ASAR_REL).write_text("asar", encoding="utf-8")
        assert not (app / dh.APP_LAUNCHER_REL).exists()   # the real situation
        monkeypatch.setattr(dh, "app_candidates", lambda: [app])
        monkeypatch.setattr(dh.shutil, "which", lambda name: None)
        argv, reason = dh.resolve_launcher()
        assert reason == ""
        assert argv is not None

    def test_falls_back_to_a_standalone_cli(self, monkeypatch):
        monkeypatch.setattr(dh, "app_candidates", lambda: [])
        monkeypatch.setattr(dh.shutil, "which", lambda name: "/usr/local/bin/dsh")
        assert dh.resolve_launcher() == (["/usr/local/bin/dsh"], "")

    def test_reports_not_found(self, monkeypatch):
        monkeypatch.setattr(dh, "app_candidates", lambda: [])
        monkeypatch.setattr(dh.shutil, "which", lambda name: None)
        argv, reason = dh.resolve_launcher()
        assert argv is None
        assert "not found" in reason


# --------------------------------------------------------------------------
# API integration: status payload, branch refusal, internal session event
# --------------------------------------------------------------------------


class _ModelOptionsProvider(AgentProvider):
    """A provider that publishes explicit value/label model rows."""

    id = "fake-model-options"
    label = "Fake Models"

    async def probe(self) -> ProviderStatus:
        return ProviderStatus(available=True, version="1.2.3")

    def capabilities(self) -> ProviderCaps:
        return ProviderCaps(stateful=True, tools="mcp")

    def model_options(self) -> list[dict]:
        return [{"value": '["deepseek-account","deepseek-v4-pro"]',
                 "label": "DeepSeek-V4-Pro", "group": "deepseek-account"}]

    async def send(self, turn, emit, handle) -> TurnResult:
        return TurnResult()

    async def stop(self, handle) -> None:
        handle.stop_requested = True


class _SessionEventProvider(AgentProvider):
    """Emits the internal session event but reports no token of its own."""

    id = "fake-session-event"
    label = "Session Event Fake"

    async def probe(self) -> ProviderStatus:
        return ProviderStatus(available=True, version="1")

    def capabilities(self) -> ProviderCaps:
        return ProviderCaps(stateful=True, tools="mcp")

    async def send(self, turn, emit, handle) -> TurnResult:
        await emit(BotEvent(t="session", id="sess-internal"))
        await emit(BotEvent(t="delta", text="done"))
        return TurnResult()          # deliberately no resume_token

    async def stop(self, handle) -> None:
        handle.stop_requested = True


@pytest.fixture()
def client(bot_client):
    return bot_client


@pytest.fixture()
def model_options_provider():
    provider = _ModelOptionsProvider()
    register_provider(provider)
    return provider


@pytest.fixture()
def session_event_provider():
    provider = _SessionEventProvider()
    register_provider(provider)
    return provider


class TestApiIntegration:
    async def test_status_publishes_model_options(self, client,
                                                  model_options_provider):
        data = await (await client.get("/comfytv/bot/status")).json()
        entry = next(p for p in data["providers"]
                     if p["id"] == "fake-model-options")
        assert entry["model_options"] == [
            {"value": '["deepseek-account","deepseek-v4-pro"]',
             "label": "DeepSeek-V4-Pro", "group": "deepseek-account"}]
        # `models` keeps its string-array contract for every other consumer
        assert entry["models"] == []

    async def test_status_omits_model_options_for_plain_providers(
            self, client, fake_provider):
        data = await (await client.get("/comfytv/bot/status")).json()
        entry = next(p for p in data["providers"] if p["id"] == "fake-test")
        assert "model_options" not in entry

    async def test_branch_is_refused_for_the_harness_provider(self, client):
        """ComfyTV branching copies resume_token, and ACP has no fork, so two
        branches would share one Harness session.  Built straight from storage
        so the test never drives the real desktop runtime."""
        from ComfyTV import storage

        resp = await client.post("/comfytv/bot/chats",
                                 json={"provider": "deepseek-harness"})
        chat = (await resp.json())["chat"]
        message = storage.create_bot_message(chat_id=chat["id"],
                                             role="assistant", content="[]",
                                             status="done")
        resp = await client.post(
            f"/comfytv/bot/chats/{chat['id']}/branch",
            json={"message_id": message["id"]})
        assert resp.status == 400
        assert "cannot be branched" in (await resp.json())["error"]

    async def test_branch_still_works_for_other_providers(self, client,
                                                          fake_provider):
        resp = await client.post("/comfytv/bot/chats",
                                 json={"provider": "fake-test"})
        chat = (await resp.json())["chat"]
        await client.post(f"/comfytv/bot/chats/{chat['id']}/send",
                          json={"text": "one"})
        data = await _wait_done(client, chat["id"])
        assistant_id = next(m["id"] for m in data["messages"]
                            if m["role"] == "assistant")
        resp = await client.post(
            f"/comfytv/bot/chats/{chat['id']}/branch",
            json={"message_id": assistant_id})
        assert resp.status == 200
        assert (await resp.json())["chat"]["resume_token"] == "tok-1"

    async def test_internal_session_event_is_persisted_and_not_streamed(
            self, client, session_event_provider):
        resp = await client.post("/comfytv/bot/chats",
                                 json={"provider": "fake-session-event"})
        chat = (await resp.json())["chat"]
        await client.post(f"/comfytv/bot/chats/{chat['id']}/send",
                          json={"text": "hi"})
        data = await _wait_done(client, chat["id"])
        # the token is stored on both the chat and the assistant message
        assert data["chat"]["resume_token"] == "sess-internal"
        assistant = next(m for m in data["messages"]
                         if m["role"] == "assistant")
        assert assistant["resume_token_after"] == "sess-internal"
        # and the session event never became a visible message block
        assert "sess-internal" not in json.dumps(assistant["content"])


# --------------------------------------------------------------------------
# model application: values must be echoed verbatim, never rebuilt
# --------------------------------------------------------------------------


class TestModelApplication:
    async def test_default_model_is_applied_verbatim_from_the_catalog(
            self, tmp_path, fake_runtime, monkeypatch):
        """Regression: the live runtime rejected a rebuilt value.

        `json.dumps(["a","b"])` emits `["a", "b"]` with a space, but the runtime
        matches its own option strings character for character, so a
        re-serialized value fails as an unknown model option.
        """
        seen = fake_runtime(monkeypatch, configOptions=_config_options())
        provider = dh.DeepSeekHarnessProvider()
        _events, emit = _events_collector()
        result, _handle = await _run_turn(
            provider, TurnRequest(chat_id="c1", user_text="hi"), emit)
        assert result.error == ""
        applied = dict(_seen_entries(seen))["set_config_option"]
        catalog = {o.value for o in dh._model_options_from(_config_options())}
        assert applied["value"] in catalog
        assert applied["value"] == '["deepseek-account","deepseek-flash"]'
        assert ", " not in applied["value"]
        assert applied["configId"] == "model"

    async def test_stale_saved_model_is_refused_before_booting(
            self, tmp_path, fake_runtime, monkeypatch):
        seen = fake_runtime(monkeypatch, configOptions=_config_options())
        provider = dh.DeepSeekHarnessProvider()
        # as if a previous turn had already cached the catalog
        provider._catalog = dh._model_options_from(_config_options())
        _events, emit = _events_collector()
        result, _handle = await _run_turn(
            provider,
            TurnRequest(chat_id="c1", user_text="hi",
                        model='["deepseek-account","removed-model"]'),
            emit)
        assert "no longer offered" in result.error
        assert [k for k, _ in _seen_entries(seen)] == []   # never even booted

    async def test_model_missing_from_the_fresh_catalog_is_refused(
            self, tmp_path, fake_runtime, monkeypatch):
        """Nothing cached yet, so the check has to happen after session/new."""
        seen = fake_runtime(monkeypatch, configOptions=_config_options())
        provider = dh.DeepSeekHarnessProvider()
        provider._catalog = []          # first ever turn: no catalog to check
        _events, emit = _events_collector()
        result, _handle = await _run_turn(
            provider,
            TurnRequest(chat_id="c1", user_text="hi",
                        model='["deepseek-account","not-in-catalog"]'),
            emit)
        assert "no longer offered" in result.error
        kinds = [k for k, _ in _seen_entries(seen)]
        assert "new" in kinds
        assert "set_config_option" not in kinds
        assert "prompt" not in kinds


# --------------------------------------------------------------------------
# Live acceptance against the installed desktop runtime.
#
# Opt-in: set COMFYTV_LIVE_HARNESS=1.  These tests really launch the bundled
# ACP runtime and spend real model requests on the account configured for the
# provider, so they never run as part of the default suite.  They also create a
# real `comfytv-acp` profile under $DSH_HOME on first use.
# --------------------------------------------------------------------------

LIVE_HARNESS = os.environ.get("COMFYTV_LIVE_HARNESS") == "1"


def _live_available() -> bool:
    argv, _reason = dh.resolve_launcher()
    return argv is not None


@pytest.mark.skipif(not LIVE_HARNESS,
                    reason="set COMFYTV_LIVE_HARNESS=1 to run against the "
                           "installed DeepSeek Harness desktop app")
class TestLiveDesktopRuntime:
    async def _mcp_endpoint(self, client, chat_id: str) -> str:
        return (f"http://127.0.0.1:{client.server.port}/comfytv/mcp"
                f"?bot_chat={chat_id}")

    async def _call_mcp(self, client, body: dict):
        resp = await client.post("/comfytv/mcp?bot_chat=live-acceptance",
                                 json=body)
        return resp.status, await resp.json()

    async def _drive(self, provider, turn, timeout=300.0):
        events: list[BotEvent] = []

        async def emit(ev: BotEvent) -> None:
            events.append(ev)

        result = await asyncio.wait_for(
            provider.send(turn, emit, TurnHandle()), timeout=timeout)
        reply = "".join(e.text for e in events if e.t == "delta")
        return result, reply, events

    async def test_tool_scope_is_only_the_comfytv_mcp_server(self, client):
        """Verify the agent's callable tools, beyond inspecting config files."""
        if not _live_available():
            pytest.skip("DeepSeek Harness desktop app not installed")

        status, body = await self._call_mcp(client, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        assert status == 200
        server_tools = {t["name"] for t in body["result"]["tools"]}
        assert "server_info" in server_tools

        provider = dh.DeepSeekHarnessProvider()
        result, reply, _events = await self._drive(
            provider,
            TurnRequest(
                chat_id="live-tools",
                user_text=("List the exact tool names you can call right now. "
                           "Output only the names, one per line."),
                mcp_endpoint=await self._mcp_endpoint(client, "live-tools"),
                model=""),
        )
        assert result.error == ""
        called = set(re.findall(r"mcp__[A-Za-z0-9_]+__[A-Za-z0-9_]+", reply))
        assert called, f"the agent reported no MCP tools:\n{reply[:500]}"
        # every reported MCP tool is a ComfyTV tool
        assert all(name.startswith("mcp__comfytv__") for name in called)
        # and none of the built-in tool entry points survived the narrowing.
        # The only non-MCP tools left are the MCP *resource* readers, which can
        # only reach servers mounted for this session (ComfyTV's).
        bare = {name for name in re.findall(r"(?mi)^\s*([a-z_][a-z0-9_]*)\s*$",
                                           reply)
                if not name.startswith("mcp__")}
        assert bare <= {"read_mcp_resource", "list_mcp_resources",
                        "list_mcp_resource_templates"}, \
            f"unexpected bare tools: {bare}"

    async def test_two_turns_resume_one_session(self, client):
        if not _live_available():
            pytest.skip("DeepSeek Harness desktop app not installed")

        provider = dh.DeepSeekHarnessProvider()
        endpoint = await self._mcp_endpoint(client, "live-resume")
        secret = "ZQ7X4-KESTREL"

        first, _reply, _events = await self._drive(
            provider,
            TurnRequest(chat_id="live-resume",
                        user_text=f"Remember this token for later: {secret}. "
                                  "Reply with just: OK",
                        mcp_endpoint=endpoint, model=""))
        assert first.error == ""
        assert first.resume_token, "no session id was reported back"

        second, reply, _events = await self._drive(
            provider,
            TurnRequest(chat_id="live-resume",
                        user_text="What token did I ask you to remember? "
                                  "Reply with just the token.",
                        mcp_endpoint=endpoint,
                        resume_token=first.resume_token, model=""))
        assert second.error == ""
        # a fresh process resumed the persisted session and still knew the token
        assert second.resume_token == first.resume_token
        assert secret in reply
