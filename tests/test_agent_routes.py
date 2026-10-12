import asyncio
import json

import pytest

from ComfyTV.api import agent_messages
from ComfyTV.bot.providers import BotEvent


class TestAgentMessages:
    def test_tool_pair_merges_into_one_call(self):
        blocks = [
            {"type": "text", "text": "hi"},
            {"type": "tool_use", "id": "c1", "name": "get_canvas", "input": {"a": 1}},
            {"type": "tool_result", "id": "c1", "name": "get_canvas", "text": "ok",
             "status": "success", "duration_ms": 12},
            {"type": "notice", "level": "warn", "text": "n"},
        ]
        out = agent_messages.assistant_content(blocks)
        assert out["text"] == "hi"
        calls = [b for b in out["blocks"] if b["type"] == "tool_call"]
        assert calls == [{"type": "tool_call", "tool_call_id": "c1", "tool_name": "get_canvas",
                          "args": {"a": 1}, "status": "success", "result": "ok",
                          "duration_ms": 12}]

    def test_pending_run_approval_becomes_pending_ask(self):
        blocks = [{"type": "ask", "ask_id": "a1", "status": "pending", "kind": "run_approval",
                   "prompt": "run_stage 3", "options": [{"id": "run", "label": "Run"}],
                   "min_selections": 1, "max_selections": 1, "allow_other": False}]
        row = {"id": "m1", "chat_id": "c", "parent_id": "u1", "role": "assistant",
               "status": "streaming", "content": json.dumps(blocks), "created_at": None}
        msg = agent_messages.to_agent_message(row, 1)
        assert msg["turn_id"] == "u1"
        assert msg["status"] == "streaming"
        assert msg["pending_ask"]["ask_id"] == "a1"
        assert msg["pending_ask"]["context"] == {"workflow_name": "run_stage 3"}

    def test_user_row_keeps_input_attachments(self):
        blocks = [{"type": "image", "url": "/api/view?filename=a.png&type=input",
                   "input_name": "a.png"},
                  {"type": "text", "text": "look"}]
        row = {"id": "u1", "chat_id": "c", "parent_id": None, "role": "user",
               "status": "done", "content": json.dumps(blocks), "created_at": None}
        msg = agent_messages.to_agent_message(row, 0)
        assert msg["turn_id"] == "u1"
        assert msg["content"] == {
            "text": "look", "attachments": ["a.png"],
            "attachment_previews": {"a.png": "/api/view?filename=a.png&type=input"},
            "attachment_labels": {}}

    def test_user_row_keeps_library_assets(self):
        blocks = [{"type": "image", "url": "/comfytv/assets/7/payload", "asset_id": 7,
                   "name": "cat.png"}, {"type": "text", "text": "look"}]
        row = {"id": "u1", "chat_id": "c", "parent_id": None, "role": "user",
               "status": "done", "content": json.dumps(blocks), "created_at": None}
        content = agent_messages.to_agent_message(row, 0)["content"]
        assert content["attachments"] == ["asset:7"]
        assert content["attachment_previews"] == {"asset:7": "/comfytv/assets/7/payload"}
        assert content["attachment_labels"] == {"asset:7": "cat.png"}


async def _wait_done(client, thread_id, timeout=5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/comfytv/agent/threads/{thread_id}/messages")
        rows = await resp.json()
        if rows and all(r["status"] != "streaming" for r in rows):
            return rows
        await asyncio.sleep(0.02)
    raise AssertionError("turn did not finish")


class TestAgentRoutes:
    async def test_empty_thread_list_has_pagination(self, bot_client):
        resp = await bot_client.get("/comfytv/agent/threads")
        data = await resp.json()
        assert data["threads"] == []
        assert data["pagination"] == {"has_more": False, "limit": 1, "offset": 0, "total": 0}

    async def test_turn_roundtrip(self, bot_client, fake_provider):
        fake_provider.script = [
            BotEvent(t="delta", text="hello "),
            BotEvent(t="tool_use", id="t1", name="get_canvas", input={}),
            BotEvent(t="tool_result", id="t1", name="get_canvas", text="{}"),
            BotEvent(t="delta", text="world"),
        ]
        resp = await bot_client.post(
            "/comfytv/agent/threads/new/messages",
            json={"content": "hi there", "provider": fake_provider.id,
                  "selection": {"node_ids": ["7"]}, "workflow_id": "wf-1"})
        assert resp.status == 202
        ack = await resp.json()
        assert set(ack) == {"thread_id", "message_id", "workflow_id"}
        rows = await _wait_done(bot_client, ack["thread_id"])
        assert [r["role"] for r in rows] == ["user", "assistant"]
        assert rows[0]["content"]["text"] == "hi there"
        assert rows[0]["content"]["refs"][0]["graph_node_id"] == "7"
        assert rows[1]["id"] == ack["message_id"]
        assert rows[1]["status"] == "complete"
        assert rows[1]["turn_id"] == rows[0]["id"]
        assert rows[1]["content"]["text"] == "hello \nworld"
        assert "Referenced stage" in fake_provider.last_turn.user_text
        listed = await (await bot_client.get("/comfytv/agent/threads")).json()
        assert listed["threads"][0]["id"] == ack["thread_id"]
        assert listed["threads"][0]["message_count"] == 2
        assert listed["threads"][0]["status"] == "active"
        for key in ("created_at", "updated_at", "last_message_at", "workflow_id", "preview"):
            assert isinstance(listed["threads"][0][key], str)

    async def test_second_message_on_busy_thread_is_rejected(self, bot_client, fake_provider):
        fake_provider.gate = asyncio.Event()
        resp = await bot_client.post(
            "/comfytv/agent/threads/new/messages",
            json={"content": "one", "provider": fake_provider.id})
        ack = await resp.json()
        await asyncio.sleep(0.15)
        busy = await bot_client.post(
            f"/comfytv/agent/threads/{ack['thread_id']}/messages", json={"content": "two"})
        assert busy.status == 409
        cancel = await bot_client.post(
            f"/comfytv/agent/threads/{ack['thread_id']}/messages/{ack['message_id']}/cancel")
        assert cancel.status == 202
        assert (await cancel.json()) == {"status": "cancelling"}
        rows = await _wait_done(bot_client, ack["thread_id"])
        assert rows[1]["status"] == "interrupted"

    async def test_leading_slash_picks_a_skill(self, bot_client, fake_provider, tmp_path, monkeypatch):
        from ComfyTV import skill_store
        from test_skill_store import make_skill
        builtin = tmp_path / "builtin-skills"
        user = tmp_path / "user-skills"
        builtin.mkdir()
        user.mkdir()
        monkeypatch.setattr(skill_store, "BUILTIN_SKILLS_DIR", builtin)
        monkeypatch.setattr(skill_store, "user_skills_dir", lambda: user)
        make_skill(builtin, "trailer-cutter")
        resp = await bot_client.post(
            "/comfytv/agent/threads/new/messages",
            json={"content": "/trailer-cutter cut a trailer", "provider": fake_provider.id})
        assert resp.status == 202
        ack = await resp.json()
        rows = await _wait_done(bot_client, ack["thread_id"])
        assert rows[0]["content"]["text"] == "cut a trailer"
        assert "skill 'trailer-cutter'" in fake_provider.last_turn.user_text
        unknown = await bot_client.post(
            f"/comfytv/agent/threads/{ack['thread_id']}/messages",
            json={"content": "/nope keep the slash"})
        assert unknown.status == 202
        rows = await _wait_done(bot_client, ack["thread_id"])
        assert rows[2]["content"]["text"] == "/nope keep the slash"

    async def test_unknown_library_asset_is_rejected(self, bot_client, fake_provider):
        resp = await bot_client.post(
            "/comfytv/agent/threads/new/messages",
            json={"content": "look", "provider": fake_provider.id, "attachments": ["asset:999999"]})
        assert resp.status == 400
        assert "999999" in (await resp.json())["error"]

    async def test_new_thread_defaults_to_first_available_provider(self, bot_client, fake_provider, monkeypatch):
        from ComfyTV.api import agent_routes
        from ComfyTV.bot.providers import ProviderStatus

        class Missing:
            id = "missing"

            async def probe(self):
                return ProviderStatus(available=False, detail="not installed")

        monkeypatch.setattr(agent_routes, "list_providers", lambda: [Missing(), fake_provider])
        resp = await bot_client.post("/comfytv/agent/threads/new/messages", json={"content": "hi"})
        assert resp.status == 202
        ack = await resp.json()
        await _wait_done(bot_client, ack["thread_id"])
        assert fake_provider.last_turn is not None

    async def test_run_mode_roundtrip(self, bot_client):
        assert (await (await bot_client.get("/comfytv/agent/run-mode")).json()) == \
            {"mode": "ask_approval", "credit_limit": None}
        resp = await bot_client.put("/comfytv/agent/run-mode",
                                    json={"mode": "auto_limited", "credit_limit": 5})
        assert (await resp.json()) == {"mode": "auto", "credit_limit": None}
        bad = await bot_client.put("/comfytv/agent/run-mode", json={"mode": "nope"})
        assert bad.status == 400

    async def test_workflow_index_shape(self, bot_client):
        data = await (await bot_client.get("/comfytv/agent/workflows")).json()
        assert set(data) == {"data", "pagination"}
        assert data["pagination"]["has_more"] is False
