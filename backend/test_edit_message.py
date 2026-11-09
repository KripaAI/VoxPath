"""Editing an earlier message: the agent forgets the old version and what followed."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

import agent
import main
import services


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self

    def get_num_tokens_from_messages(self, messages, tools=None):
        return 10


def test_editing_a_message_rewinds_the_saved_conversation(monkeypatch):
    seen = []

    class Recording(ScriptedModel):
        def _generate(self, messages, *args, **kwargs):
            seen.append([m.content for m in messages if m.type == "human"])
            return super()._generate(messages, *args, **kwargs)

    answers = ["Answer 1", "Answer 2", "Answer 3", "Answer 4"]
    monkeypatch.setattr(services, "build_llm", lambda: Recording(responses=[AIMessage(content=a) for a in answers]))

    async def scenario():
        await agent.init_agent(InMemorySaver(), None)
        chat = lambda text, **edit: agent.agent_chat(text, thread_id="t-edit", user_id="u-1", **edit)
        await chat("political news from Bihar")
        await chat("and from UP?")
        assert await chat("sports news from Bihar", edit_index=0, edit_original="political news from Bihar") == "Answer 3"
        after_edit = await agent.get_messages("t-edit")

        # Wrong position, or text that doesn't match what was saved: refused, nothing changes.
        with pytest.raises(agent.EditConflict):
            await chat("x", edit_index=1)
        with pytest.raises(agent.EditConflict):
            await chat("x", edit_index=0, edit_original="political news from Bihar")

        await chat("thanks")
        return after_edit, await agent.get_messages("t-edit")

    after_edit, final = asyncio.run(scenario())
    assert after_edit == [
        {"role": "user", "content": "sports news from Bihar"},
        {"role": "assistant", "content": "Answer 3"},
    ]
    # The model answering the edit never saw the old question or the turn after it.
    assert seen[2] == ["sports news from Bihar"]
    assert seen[3] == ["sports news from Bihar", "thanks"]
    assert [m["content"] for m in final] == ["sports news from Bihar", "Answer 3", "thanks", "Answer 4"]


def _signed_in(monkeypatch, chat):
    monkeypatch.setattr(agent, "agent_chat", chat)
    client = TestClient(main.app)
    token = client.post("/auth/register", json={"username": "alice", "password": "password123"}).json()["access_token"]
    return client, {"Authorization": f"Bearer {token}"}


def test_chat_endpoint_passes_edits_through_and_reports_conflicts(fake_db, monkeypatch):
    calls = []

    async def chat(message, **kwargs):
        calls.append(kwargs)
        if kwargs.get("edit_index") == 7:
            raise agent.EditConflict("not found")
        return "ok"

    client, headers = _signed_in(monkeypatch, chat)
    post = lambda **body: client.post("/chat", json={"message": "hi", "thread_id": "t-1", **body}, headers=headers)

    assert post().status_code == 200
    assert "edit_index" not in calls[-1]  # a plain message is not an edit

    assert post(edit_index=0, edit_original="hello").status_code == 200
    assert (calls[-1]["edit_index"], calls[-1]["edit_original"]) == (0, "hello")

    conflict = post(edit_index=7)
    assert conflict.status_code == 409 and "Reopen the chat" in conflict.json()["error"]
    assert post(edit_index=-1).status_code == 422
