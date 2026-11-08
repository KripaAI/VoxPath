"""'Don't remember me' is enforced in code: once a user asks, memory writes are refused
for the rest of the conversation, whatever the model does (eval case sess-003)."""

import asyncio

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

import agent
import memory
import services


@pytest.mark.parametrize("text", [
    "Please don't save anything about me to memory in this chat.",
    "Don't remember any of this.",
    "Do not store my details.",
    "Stop remembering things about me.",
    "I live in Patna, and don't save that.",
    "Keep this off the record: I live in Patna.",
    "No memory for this chat please.",
    "मेरी बातें याद मत रखना",
    "इसे सेव मत करो",
    "ye sab save mat karo",
    "meri details yaad mat rakhna",
    "Don't save this: I live in Patna.",  # a refusal wins over the fact in the same message
])
def test_requests_not_to_be_remembered(text):
    assert memory.memory_directive(text) == "block"


@pytest.mark.parametrize("text", [
    "I don't remember my PNR number.",          # the user forgetting, not an instruction
    "Don't you remember my name?",
    "I can't remember which train it was.",
    "मुझे याद नहीं है",                          # "I don't remember" (नहीं, not the imperative मत)
    "What's the price of TCS?",
    "I live in Patna and I follow cricket.",
])
def test_ordinary_messages_are_not_directives(text):
    assert memory.memory_directive(text) is None


@pytest.mark.parametrize("text", [
    "You can remember things again.",
    "Start saving again please.",
    "Please remember that I live in Pune.",
    "Remember my name is Kripa.",
])
def test_requests_to_be_remembered(text):
    assert memory.memory_directive(text) == "allow"


def test_latest_request_wins_and_no_policy_means_no_block():
    assert not memory.writes_blocked()  # outside an agent run nothing is blocked
    with memory.write_policy():
        memory.apply_directives(["Don't save anything.", "What's the price of TCS?"])
        assert memory.writes_blocked()
        memory.apply_directives(["Don't save anything.", "You can remember things again."])
        assert not memory.writes_blocked()
    assert not memory.writes_blocked()


class ChatOpenAI(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self

    def get_num_tokens_from_messages(self, messages, tools=None):
        return 10


def _save(text, call_id):
    return AIMessage(content="", tool_calls=[{"name": "save_memory", "args": {"text": text}, "id": call_id}])


def test_the_agent_cannot_save_after_the_user_opts_out(monkeypatch):
    """sess-003 end to end: the model tries to save on turn 2 and is refused; the store
    stays empty; after the user allows it again, saving works."""
    monkeypatch.setattr(services, "build_llm", lambda: ChatOpenAI(responses=[
        AIMessage(content="Understood, I won't save anything."),           # turn 1
        _save("User lives in Patna.", "c1"), AIMessage(content="ok"),      # turn 2: tries anyway
        _save("User lives in Pune.", "c2"), AIMessage(content="Saved."),   # turn 3: allowed again
    ]))
    store = InMemoryStore()

    async def chat():
        await agent.init_agent(InMemorySaver(), store)
        replies, tool_results = [], []
        for message in ["Please don't save anything about me to memory in this chat.",
                        "I live in Patna and I follow cricket.",
                        "You can remember things again. Remember that I've moved to Pune."]:
            with agent.record_turn() as turn:
                replies.append(await agent.agent_chat(message, thread_id="t-optout", user_id="u-optout"))
            tool_results.append([c.get("output") for c in turn.tool_calls])
        return tool_results, [i.value["text"] for i in await store.asearch(("memories", "u-optout"), limit=10)]

    tool_results, stored = asyncio.run(chat())
    assert tool_results[1][0]["status"] == "not_saved"   # refused in code
    assert tool_results[2][0]["status"] == "saved"       # consent given again
    assert stored == ["User lives in Pune."]             # Patna never reached the store
