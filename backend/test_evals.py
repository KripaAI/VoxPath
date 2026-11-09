"""Offline checks for the eval suite in evals/ (no model, no network).

Book Ch 17: validate an evaluator on a known-good and a known-bad run before
trusting what it says about the agent. Every evaluator here gets both, and the
runner is exercised end to end with a scripted model.
"""

import asyncio
import json
import re
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

import agent
import services
from evals import evaluators as ev
from evals.run import dataset_name, load_cases, run_case, select_cases, summarize


def call(tool, inp=None, out=None, status="ok"):
    return {"tool": tool, "input": inp or {}, "output": out, "status": status}


def run(*turns, memories=()):
    """turns: (answer, [tool calls]) pairs -> a recorded output."""
    return {
        "turns": [{"user": f"q{i}", "answer": a, "tool_calls": list(c)} for i, (a, c) in enumerate(turns)],
        "memories": list(memories),
    }


def label(evaluator, output, expected):
    return evaluator(output, expected)["label"]


NEWS = {"headlines": [{"headline": "Rain in Patna", "url": "https://example.com/patna-rain"}], "count": 1}


# --------------------------------------------------------------------------- known good / known bad
def test_required_tools():
    exp = {"turns": [{"required_tools": ["get_stock_price"]}, {"required_tools": ["get_stock_price"]}]}
    good = run(("₹10", [call("get_stock_price")]), ("₹11", [call("get_stock_price")]))
    stale = run(("₹10", [call("get_stock_price")]), ("still ₹10", []))  # answered turn 2 from memory
    assert label(ev.required_tools, good, exp) == "pass"
    assert label(ev.required_tools, stale, exp) == "fail"
    assert "turn 2" in ev.required_tools(stale, exp)["explanation"]
    assert label(ev.required_tools, good, {"turns": [{}]}) == "n/a"


def test_prohibited_tools_and_asking():
    exp = {"turns": [{"ask": {"keywords": ["PNR"], "not_tools": ["get_pnr_status"]}, "prohibited_tools": ["get_pnr_status"]}]}
    good = run(("Sure, what is your 10-digit PNR number?", []))
    guessed = run(("Your ticket is confirmed.", [call("get_pnr_status", {"pnr": "1234567890"})]))
    no_question = run(("Please share the PNR.", []))
    assert label(ev.asks_before_acting, good, exp) == "pass"
    assert label(ev.prohibited_tools, good, exp) == "pass"
    assert label(ev.asks_before_acting, guessed, exp) == "fail"
    assert label(ev.prohibited_tools, guessed, exp) == "fail"
    assert label(ev.asks_before_acting, no_question, exp) == "fail"


def test_max_calls():
    exp = {"turns": [{"max_calls": {"get_stock_price": 1}}]}
    one = run(("₹1324", [call("get_stock_price", {"ticker": "रिलायंस"})]))
    retried = run(("₹1324", [call("get_stock_price", {"ticker": "रिलायंस"}), call("get_stock_price", {"ticker": "Reliance"})]))
    assert label(ev.max_calls, one, exp) == "pass"
    assert label(ev.max_calls, retried, exp) == "fail"
    assert label(ev.max_calls, one, {}) == "n/a"


def test_tool_arguments_normalises_but_catches_wrong_values():
    exp = {"turns": [{"tool_args": {"get_pnr_status": {"pnr": "4521876390"}, "get_latest_news": {"region": "Bihar"}}}]}
    good = run(("ok", [call("get_pnr_status", {"pnr": " 4521876390 "}), call("get_latest_news", {"region": "bihar"})]))
    wrong = run(("ok", [call("get_pnr_status", {"pnr": "4521876309"}), call("get_latest_news", {"region": "Bihar"})]))
    assert label(ev.tool_arguments, good, exp) == "pass"
    assert label(ev.tool_arguments, wrong, exp) == "fail"


def test_news_links_grounded():
    good = run(("- [Rain in Patna](https://example.com/patna-rain) - heavy rain.", [call("get_latest_news", out=NEWS)]))
    invented = run(("- [Rain](https://example.com/made-up)", [call("get_latest_news", out=NEWS)]))
    later_turn = run(("headlines", [call("get_latest_news", out=NEWS)]), ("Again: https://example.com/patna-rain/", []))
    assert label(ev.news_links_grounded, good, {}) == "pass"
    assert label(ev.news_links_grounded, invented, {}) == "fail"
    assert label(ev.news_links_grounded, later_turn, {}) == "pass"  # a link from earlier in the session is fine
    assert label(ev.news_links_grounded, run(("no links", [])), {}) == "n/a"


def test_price_grounded():
    tool = call("get_stock_price", {"ticker": "TCS"}, {"result": "Tata Consultancy (TCS.NS): 4012.50 INR"})
    assert label(ev.price_grounded, run(("TCS is at ₹4,012.50.", [tool])), {}) == "pass"
    assert label(ev.price_grounded, run(("About Rs 4012 today.", [tool])), {}) == "pass"  # rounded
    assert label(ev.price_grounded, run(("TCS is at ₹4,120.50.", [tool])), {}) == "fail"
    assert label(ev.price_grounded, run(("TCS is at 4012.50 INR.", [])), {}) == "fail"  # no tool at all
    assert label(ev.price_grounded, run(("Hello!", [])), {}) == "n/a"
    # baseline news-007: a price quoted from a headline is grounded in the news result
    news = call("get_latest_news", out={"headlines": [{"headline": "3 stock picks under ₹50", "url": "https://x.com/a"}]})
    assert label(ev.price_grounded, run(("- [3 stock picks under ₹50](https://x.com/a)", [news])), {}) == "pass"


def test_station_resolved_first():
    exp = {"station_resolution": True}
    good = run(("fare", [call("resolve_station_code"), call("get_fare")]))
    wrong_order = run(("fare", [call("get_fare"), call("resolve_station_code")]))
    assert label(ev.station_resolved_first, good, exp) == "pass"
    assert label(ev.station_resolved_first, wrong_order, exp) == "fail"
    assert label(ev.station_resolved_first, good, {}) == "n/a"


def test_memory_checks():
    live = run(("ok", [call("save_memory", {"text": "TCS share price is ₹4012"})]))
    durable = run(("ok", [call("save_memory", {"text": "User likes IT stocks"})]))
    assert label(ev.no_live_data_in_memory, live, {}) == "fail"
    assert label(ev.no_live_data_in_memory, durable, {}) == "pass"

    found = {"memories": [{"id": "ab12cd34ef56", "text": "lives in Patna"}], "count": 1}
    good = run(("ok", [call("search_memories", out=found), call("update_memory", {"memory_id": "ab12cd34ef56", "text": "lives in Pune"})]))
    invented = run(("ok", [call("update_memory", {"memory_id": "999", "text": "lives in Pune"})]))
    from_save = run(  # baseline mem-002: reusing the id save_memory returned is correct
        ("noted", [call("save_memory", {"text": "lives in Patna"}, {"status": "saved", "id": "2123dffb9c75"})]),
        ("updated", [call("update_memory", {"memory_id": "2123dffb9c75", "text": "lives in Pune"})]),
    )
    assert label(ev.memory_id_from_tool, good, {}) == "pass"
    assert label(ev.memory_id_from_tool, from_save, {}) == "pass"
    assert label(ev.memory_id_from_tool, invented, {}) == "fail"

    exp = {"memory_contains": ["Pune"], "memory_absent": ["Patna"]}
    assert label(ev.memory_outcome, run(("ok", []), memories=["User lives in Pune"]), exp) == "pass"
    assert label(ev.memory_outcome, run(("ok", []), memories=["User lives in Patna", "User lives in Pune"]), exp) == "fail"
    assert label(ev.memory_outcome, run(("I've updated it!", []), memories=[]), exp) == "fail"  # a claim is not a record


def test_answer_language():
    exp = {"turns": [{"answer_language": "hi"}]}
    assert label(ev.answer_language, run(("रिलायंस का शेयर ₹2,950 पर है। [Moneycontrol](https://x.com)", [])), exp) == "pass"
    assert label(ev.answer_language, run(("Reliance is at ₹2,950.", [])), exp) == "fail"


def test_a_broken_evaluator_is_reported_not_raised(monkeypatch):
    def broken(output, expected):
        raise KeyError("boom")

    monkeypatch.setattr(ev, "EVALUATORS", [broken])
    assert ev.evaluate(run(("x", [])), {})["broken"]["label"] == "error"


# --------------------------------------------------------------------------- the dataset itself
def test_cases_are_well_formed():
    cases = load_cases()
    assert len(cases) >= 40
    known_tools = set(agent.AGENT_TOOLS)
    requirements = set(re.findall(r"^\| (R\d+) \|", (Path("evals") / "REQUIREMENTS.md").read_text(encoding="utf-8"), re.M))
    for case in cases:
        exp = case["expected"]
        assert case["turns"] and exp.get("behaviour"), case["id"]
        assert len(exp.get("turns", [])) <= len(case["turns"]), case["id"]
        assert set(case["requirement"].split()) <= requirements, case["id"]
        assert case["source"] in {"designed", "trace", "support", "production", "synthetic"}, case["id"]
        named = set(exp.get("prohibited_tools", []))
        for spec in exp.get("turns", []):
            named |= set(spec.get("required_tools", [])) | set(spec.get("prohibited_tools", []))
            named |= set(spec.get("tool_args", {})) | set((spec.get("ask") or {}).get("not_tools", []))
        assert named <= known_tools, (case["id"], named - known_tools)


def test_case_selection_and_dataset_versioning():
    cases = load_cases()
    no_rail = select_cases(cases, skip_tags=["railway"])
    assert no_rail and all("railway" not in c["tags"] for c in no_rail)
    assert [c["id"] for c in select_cases(cases, ids=["stock-004"])] == ["stock-004"]
    assert dataset_name(cases) == dataset_name(load_cases())
    assert dataset_name(cases) != dataset_name(cases[:-1])  # editing the cases makes a new version


# --------------------------------------------------------------------------- the runner, end to end
class ChatOpenAI(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self

    def get_num_tokens_from_messages(self, messages, tools=None):
        return 10


@pytest.mark.parametrize("answer, price_label", [("TCS is at ₹4,012.50.", "pass"), ("TCS is at ₹4,120.00.", "fail")])
def test_runner_records_a_real_agent_run(monkeypatch, answer, price_label):
    monkeypatch.setattr(services, "get_stock_price_smart", lambda ticker: {"result": f"Tata Consultancy ({ticker}.NS): 4012.50 INR"})
    monkeypatch.setattr(services, "build_llm", lambda: ChatOpenAI(responses=[
        AIMessage(content="", tool_calls=[{"name": "get_stock_price", "args": {"ticker": "TCS"}, "id": "c1"}]),
        AIMessage(content=answer),
    ]))
    case = next(c for c in load_cases() if c["id"] == "stock-001")

    async def go():
        store = InMemoryStore()
        await agent.init_agent(InMemorySaver(), store)
        return await run_case(case["turns"], case["id"], store)

    output = asyncio.run(go())
    turn = output["turns"][0]
    assert turn["answer"] == answer and turn["error"] is None
    assert turn["tool_calls"][0]["tool"] == "get_stock_price"
    assert turn["tool_calls"][0]["input"] == {"ticker": "TCS"}
    assert "4012.50" in turn["tool_calls"][0]["output"]["result"]  # the full output, not just a preview
    assert output["prompt_version"] == agent.PROMPT_VERSION

    results = ev.evaluate(output, case["expected"])
    assert results["required_tools"]["label"] == "pass"
    assert results["tool_arguments"]["label"] == "pass"
    assert results["price_grounded"]["label"] == price_label
    assert results["memory_outcome"]["label"] == "n/a"

    summary = summarize([{"case": case, "repeat": 0, "output": output, "results": results}])
    assert summary["evaluators"]["price_grounded"]["pass_rate"] == (1.0 if price_label == "pass" else 0.0)


# --------------------------------------------------------------------------- LLM judges (offline)
from evals import judges  # noqa: E402
from evals.validate_judges import load_labels, metrics  # noqa: E402


def test_judge_metrics_match_the_book():
    # Book Ch 12 worked example: TP 8, FN 2, FP 3, TN 27
    pairs = [("U", "U")] * 8 + [("U", "S")] * 2 + [("S", "U")] * 3 + [("S", "S")] * 27
    m = metrics(pairs, "U")
    assert (m["recall"], round(m["precision"], 3), m["agreement"], round(m["kappa"], 2)) == (0.8, 0.727, 0.875, 0.68)
    # 90% agreement from always saying the common label is no better than chance
    m = metrics([("S", "S")] * 9 + [("U", "S")], "U")
    assert m["agreement"] == 0.9 and m["kappa"] == 0 and m["recall"] == 0


def test_labels_are_balanced_split_and_examples_come_from_dev():
    rows = load_labels()
    assert len({r["id"] for r in rows}) == len(rows)
    for name, spec in judges.JUDGES.items():
        mine = [r for r in rows if r["judge"] == name]
        assert {r["label"] for r in mine} == set(spec["labels"])
        held_failures = [r for r in mine if r["split"] == "heldout" and r["label"] == spec["fail"]]
        assert len(held_failures) >= 3, name  # enough failures held out to measure recall at all
        by_id = {r["id"]: r for r in mine}
        assert all(by_id[i]["split"] == "dev" for i in judges.EXAMPLE_IDS[name])


def test_judge_prompt_has_the_five_parts_and_tagged_inputs():
    prompt = judges.build_prompt("answers_question", "Can you check its status?", "Please share your PNR.",
                                 [{"role": "user", "content": "My PNR is 4521876390."}])
    for part in ("ROLE:", "CRITERIA:", "EXAMPLES (reviewed):", "INPUTS:", "OUTPUT:"):
        assert part in prompt
    assert "<user_message>Can you check its status?</user_message>" in prompt
    assert "user: My PNR is 4521876390." in prompt


def test_judge_output_parsing():
    assert judges.parse("scope_compliance", "It says 'Done! booked'.\nNON_COMPLIANT")["label"] == "NON_COMPLIANT"
    assert judges.parse("scope_compliance", "Declines.\n**COMPLIANT**")["score"] == 1.0
    assert judges.parse("scope_compliance", "I think it is fine.")["label"] == "error"


def test_judge_as_evaluator_with_a_scripted_model(monkeypatch):
    replies = iter(["Claims a booking.\nNON_COMPLIANT", "Declines.\nCOMPLIANT"])
    monkeypatch.setattr(judges, "_model", lambda: type("M", (), {"invoke": lambda self, p: AIMessage(content=next(replies))})())
    output = run(("Done, booked! PNR 6234567890", []), ("I can't book tickets.", []))
    output["turns"][0]["user"], output["turns"][1]["user"] = "Book a ticket", "Book another"
    result = judges.scope_compliance(output, {})
    assert result["label"] == "fail" and "turn 1" in result["explanation"]


# --------------------------------------------------------------------------- version comparison
from evals.compare import compare as compare_runs, mcnemar_exact  # noqa: E402


def test_mcnemar_matches_the_book():
    assert round(mcnemar_exact(9, 3), 2) == 0.15     # Ch 21: 9 fixed vs 3 broken, not conclusive
    assert round(mcnemar_exact(12, 2), 3) == 0.013
    assert round(mcnemar_exact(14, 6), 3) == 0.115   # Answer 16
    assert mcnemar_exact(0, 0) == 1.0


def _fake_run(name, outcomes, prompt="p1", model="m1"):
    records = [{"case": {"id": cid}, "repeat": 0,
                "results": {"required_tools": {"label": "pass" if ok else "fail"}}} for cid, ok in outcomes.items()]
    return {"meta": {"name": name, "prompt_version": prompt, "model": model}, "records": records}


def test_compare_finds_fixed_broken_and_warns():
    base = _fake_run("base", {"a": True, "b": False, "c": True})
    cand = _fake_run("cand", {"a": True, "b": True, "c": False, "d": True}, prompt="p2", model="m2")
    c = compare_runs(base, cand)
    assert (c["fixed"], c["broken"], c["shared"]) == (["b"], ["c"], 3)
    assert c["broken_detail"] == {"c": ["required_tools"]}
    assert any("both the prompt and the model" in w for w in c["warnings"])
    assert any("different cases" in w for w in c["warnings"])


# --------------------------------------------------------------------------- production scoring
import pandas as pd  # noqa: E402

from evals import production  # noqa: E402

PARAMS = {"get_stock_price": ["ticker"], "save_memory": ["text"], "get_latest_news": ["region", "topic"]}


def _span(trace, span, name, kind, parent=None, inp=None, out=None, session="s1", start="2026-11-08T10:00:00"):
    return {"context.trace_id": trace, "context.span_id": span, "parent_id": parent, "name": name, "span_kind": kind,
            "attributes.tool.name": name if kind == "TOOL" else None, "attributes.input.value": inp,
            "attributes.output.value": out, "attributes.session.id": session, "start_time": start, "status_code": "OK"}


def _tool_out(content):
    return json.dumps({"type": "tool", "data": {"content": json.dumps(content)}})


def test_production_rebuilds_conversations_and_checks_each_turn():
    df = pd.DataFrame([
        _span("t1", "r1", "chat", "AGENT", inp="price of tata tech?", out="Tata Tech is at ₹665.65.", start="2026-11-08T10:00:00"),
        _span("t1", "x1", "get_stock_price", "TOOL", parent="r1", inp="tata tech",
              out=_tool_out({"result": "TATA TECHNOLOGIES LIMITED (TATATECH.NS): 665.65 INR"}), start="2026-11-08T10:00:01"),
        _span("t2", "r2", "chat", "AGENT", inp="and now?", out="Still ₹700.00.", start="2026-11-08T10:05:00"),
    ])
    conversations = production.conversations_from_spans(df, PARAMS)
    assert len(conversations) == 1 and [t["trace_id"] for t in conversations[0]["turns"]] == ["t1", "t2"]
    call = conversations[0]["turns"][0]["tool_calls"][0]
    assert call["input"] == {"ticker": "tata tech"} and "665.65" in call["output"]["result"]

    rows = production.score(conversations, use_judges=False)
    assert rows[0]["results"]["price_grounded"]["label"] == "pass"
    assert rows[1]["results"]["price_grounded"]["label"] == "fail"  # 700 no tool returned: a live price from nowhere
    assert "news_links_grounded" not in rows[0]["results"]          # n/a checks are not annotated


def test_production_candidates_keep_the_users_words(monkeypatch):
    target = Path("evals/results/test-candidates.jsonl")  # git-ignored folder
    target.parent.mkdir(exist_ok=True)
    target.unlink(missing_ok=True)
    monkeypatch.setattr(production, "CANDIDATES_FILE", target)
    conversations = [{"session_id": "s1", "turns": [{"user": "मेरा PNR देखो", "answer": "x", "trace_id": "t1"}]}]
    rows = [{"session_id": "s1", "turn": 1, "trace_id": "t1", "answer": "x", "started": "now",
             "results": {"price_grounded": {"label": "fail", "explanation": "prices not in any tool result"}}}]
    assert production.save_candidates(rows, {}, conversations) == 1
    assert production.save_candidates(rows, {}, conversations) == 0  # not saved twice
    saved = json.loads(target.read_text(encoding="utf-8"))
    target.unlink()
    assert saved["turns"] == ["मेरा PNR देखो"] and saved["expected_behaviour"].startswith("TODO")
