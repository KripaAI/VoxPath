"""News for an English question is English: the agent fills in the user's language."""

import asyncio

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

import agent
import services


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self

    def get_num_tokens_from_messages(self, messages, tools=None):
        return 10


def _language_used(monkeypatch, message, tool_args):
    seen = {}

    def fake_news(**kwargs):
        seen.update(kwargs)
        return {"headlines": [], "count": 0}

    monkeypatch.setattr(services, "get_latest_news", fake_news)
    monkeypatch.setattr(services, "build_llm", lambda: ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "get_latest_news", "args": tool_args, "id": "c1"}]),
        AIMessage(content="Here are the headlines."),
    ]))

    async def run():
        await agent.init_agent(None, None)
        await agent.agent_chat(message, thread_id="t-lang", user_id="u-1")

    asyncio.run(run())
    return seen.get("language")


def test_english_question_gets_english_news(monkeypatch):
    # The real trace: "top 10 news" called with {"limit": 10} only, and Hindi headlines came back.
    assert _language_used(monkeypatch, "pls give me top 10 news for today?", {"limit": 10}) == "en"


def test_hindi_question_gets_hindi_news(monkeypatch):
    assert _language_used(monkeypatch, "आज की ताज़ा खबरें दिखाओ", {"limit": 5}) == "hi"


def test_an_asked_for_language_is_kept(monkeypatch):
    # "Show me Hindi news" in English: the model passes hi, and that wins.
    assert _language_used(monkeypatch, "Show me Hindi news", {"language": "hi"}) == "hi"
