"""Code evaluators for the VoxPath agent (requirements R1-R10 in REQUIREMENTS.md).

Each evaluator answers one requirement and nothing else, so a failure always says
which rule broke (book Ch 13: keep separate requirements as separate results). All
are deterministic: the same run always gets the same verdict.

Signature: evaluator(output, expected) -> {"label", "score", "explanation"}
  output    what the runner recorded for one test case (see runner.run_case):
              {"turns": [{"user", "answer", "tool_calls": [{"tool", "input", "output", "status"}]}],
               "memories": [text, ...]}          # memory store after the conversation
  expected  the case's expectations from cases.jsonl:
              {"turns": [{"required_tools", "prohibited_tools", "tool_args", "ask", "answer_language"}],
               "prohibited_tools", "station_resolution", "memory_contains", "memory_absent"}
  label     "pass", "fail", or "n/a" when the case doesn't exercise the requirement
            (n/a is not a pass: it is left out of pass rates).

These parameter names (output, expected) are the ones Phoenix's run_experiment binds,
so the same functions run locally and as Phoenix experiment evaluators.
"""

from __future__ import annotations

import json
import re

PASS, FAIL, NA = "pass", "fail", "n/a"


def _result(label: str, explanation: str) -> dict:
    return {"label": label, "score": {PASS: 1.0, FAIL: 0.0}.get(label), "explanation": explanation}


def _turn_specs(output: dict, expected: dict) -> list[tuple[int, dict, dict]]:
    """(index, recorded turn, expectations for that turn) for every turn."""
    specs = (expected or {}).get("turns") or []
    return [(i, turn, specs[i] if i < len(specs) else {}) for i, turn in enumerate(output.get("turns", []))]


def _calls(turn: dict) -> list[dict]:
    return [c for c in turn.get("tool_calls", []) if c.get("status", "ok") == "ok"]


def _text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _norm(value) -> str:
    return " ".join(str(value).split()).casefold()


# --------------------------------------------------------------------------- R1 / R4 actions
def required_tools(output: dict, expected: dict) -> dict:
    """R1: every tool the turn requires was called in that turn (live data is re-fetched)."""
    checked, missing = 0, []
    for i, turn, spec in _turn_specs(output, expected):
        need = spec.get("required_tools") or []
        if not need:
            continue
        checked += 1
        called = {c["tool"] for c in _calls(turn)}
        missing += [f"turn {i + 1}: {tool}" for tool in need if tool not in called]
    if not checked:
        return _result(NA, "no required tools in this case")
    return _result(FAIL, "not called: " + ", ".join(missing)) if missing else _result(PASS, "all required tools called")


def prohibited_tools(output: dict, expected: dict) -> dict:
    """R4: tools that must not run (e.g. get_pnr_status with no PNR given) did not run."""
    global_ban = set((expected or {}).get("prohibited_tools") or [])
    checked, found = False, []
    for i, turn, spec in _turn_specs(output, expected):
        banned = global_ban | set(spec.get("prohibited_tools") or [])
        if not banned:
            continue
        checked = True
        found += [f"turn {i + 1}: {c['tool']}" for c in turn.get("tool_calls", []) if c["tool"] in banned]
    if not checked:
        return _result(NA, "no prohibited tools in this case")
    return _result(FAIL, "called: " + ", ".join(found)) if found else _result(PASS, "no prohibited tool called")


def tool_arguments(output: dict, expected: dict) -> dict:
    """R5: the tool was called with the user's values (compared case- and space-insensitively)."""
    checked, problems = False, []
    for i, turn, spec in _turn_specs(output, expected):
        for tool, want in (spec.get("tool_args") or {}).items():
            checked = True
            calls = [c for c in _calls(turn) if c["tool"] == tool]
            if not calls:
                problems.append(f"turn {i + 1}: {tool} not called")
                continue
            if not any(all(_norm((c.get("input") or {}).get(k, "")) == _norm(v) for k, v in want.items()) for c in calls):
                got = [c.get("input") for c in calls]
                problems.append(f"turn {i + 1}: {tool} expected {want}, got {got}")
    if not checked:
        return _result(NA, "no argument expectations in this case")
    return _result(FAIL, "; ".join(problems)) if problems else _result(PASS, "arguments match the request")


def asks_before_acting(output: dict, expected: dict) -> dict:
    """R4: with a detail missing, the agent asks for it (a question naming the detail)
    and does not call the tool that needs it."""
    checked, problems = False, []
    for i, turn, spec in _turn_specs(output, expected):
        ask = spec.get("ask")
        if not ask:
            continue
        checked = True
        answer = turn.get("answer", "")
        called = [c["tool"] for c in turn.get("tool_calls", []) if c["tool"] in set(ask.get("not_tools") or [])]
        if called:
            problems.append(f"turn {i + 1}: called {called} instead of asking")
        if "?" not in answer:
            problems.append(f"turn {i + 1}: no question asked")
        keywords = ask.get("keywords") or []
        if keywords and not any(k.casefold() in answer.casefold() for k in keywords):
            problems.append(f"turn {i + 1}: question does not mention {keywords}")
    if not checked:
        return _result(NA, "no missing-information turn in this case")
    return _result(FAIL, "; ".join(problems)) if problems else _result(PASS, "asked for the missing detail")


def max_calls(output: dict, expected: dict) -> dict:
    """R13 (efficiency): no tool is called more often than the turn needs, e.g. one
    get_stock_price for one company. Extra calls cost latency and money on every
    request (book Ch 21, 24)."""
    checked, over = False, []
    for i, turn, spec in _turn_specs(output, expected):
        for tool, limit in (spec.get("max_calls") or {}).items():
            checked = True
            n = sum(1 for c in turn.get("tool_calls", []) if c["tool"] == tool)
            if n > limit:
                over.append(f"turn {i + 1}: {tool} called {n}x (max {limit})")
    if not checked:
        return _result(NA, "no call limits in this case")
    return _result(FAIL, "; ".join(over)) if over else _result(PASS, "no redundant tool calls")


# --------------------------------------------------------------------------- R2 / R3 grounding
_MD_LINK = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)")
_BARE_URL = re.compile(r"(?<!\()https?://[^\s)\]>]+")


def _urls_in(text: str) -> list[str]:
    return [u.rstrip(".,;") for u in _MD_LINK.findall(text) + _BARE_URL.findall(text)]


def _canon(url: str) -> str:
    return url.strip().rstrip("/").casefold()


def news_links_grounded(output: dict, expected: dict) -> dict:
    """R2: every URL in an answer was returned by get_latest_news earlier in the conversation."""
    known: set[str] = set()
    cited, invented = 0, []
    for i, turn, _ in _turn_specs(output, expected):
        for call in _calls(turn):
            if call["tool"] == "get_latest_news" and isinstance(call.get("output"), dict):
                known |= {_canon(h.get("url", "")) for h in call["output"].get("headlines", []) if h.get("url")}
        for url in _urls_in(turn.get("answer", "")):
            cited += 1
            if _canon(url) not in known:
                invented.append(f"turn {i + 1}: {url}")
    if not cited:
        return _result(NA, "no links in the answers")
    if invented:
        return _result(FAIL, f"{len(invented)} of {cited} links not in any tool result: " + ", ".join(invented[:3]))
    return _result(PASS, f"all {cited} links came from get_latest_news")


_PRICE = re.compile(
    r"(?:₹|\brs\.?|\binr)\s*([\d,]+(?:\.\d+)?)|([\d,]+(?:\.\d+)?)\s*(?:inr\b|rupees\b|rs\b)", re.IGNORECASE
)
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _to_float(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _prices_in(text: str) -> list[float]:
    values = [_to_float(a or b) for a, b in _PRICE.findall(text)]
    return [v for v in values if v is not None and v > 0]


def price_grounded(output: dict, expected: dict) -> dict:
    """R3: every price stated (₹ / Rs / INR) equals a number some tool returned earlier
    in the conversation: a stock price, or a figure quoted in a news headline. A
    rounded whole-rupee figure also matches.

    v2: v1 only accepted get_stock_price results and so failed a correct answer that
    quoted a headline ("stock picks under ₹50") word for word (baseline, news-007)."""
    tool_numbers: list[float] = []
    stated, ungrounded = 0, []
    for i, turn, _ in _turn_specs(output, expected):
        for call in _calls(turn):
            tool_numbers += [v for v in map(_to_float, _NUMBER.findall(_text(call.get("output")))) if v is not None]
        for price in _prices_in(turn.get("answer", "")):
            stated += 1
            ok = any(abs(price - n) < 0.006 or (price == int(price) and abs(price - n) < 1) for n in tool_numbers)
            if not ok:
                ungrounded.append(f"turn {i + 1}: {price:g}")
    if not stated:
        return _result(NA, "no prices stated")
    if ungrounded:
        return _result(FAIL, "prices not in any tool result: " + ", ".join(ungrounded))
    return _result(PASS, f"all {stated} prices match a tool result")


# --------------------------------------------------------------------------- R6 trajectory
_NEEDS_CODES = {"get_fare", "check_seat_availability"}


def station_resolved_first(output: dict, expected: dict) -> dict:
    """R6: when the user gave city names, resolve_station_code ran before any fare or
    availability call (in this turn or an earlier one)."""
    if not (expected or {}).get("station_resolution"):
        return _result(NA, "user gave station codes or no fare/availability asked")
    resolved = False
    for i, turn, _ in _turn_specs(output, expected):
        for call in turn.get("tool_calls", []):
            if call["tool"] == "resolve_station_code" and call.get("status", "ok") == "ok":
                resolved = True
            elif call["tool"] in _NEEDS_CODES:
                if not resolved:
                    return _result(FAIL, f"turn {i + 1}: {call['tool']} ran before resolve_station_code")
                return _result(PASS, "station codes resolved before the fare/availability call")
    return _result(NA, "no fare or availability call was made")


# --------------------------------------------------------------------------- R7 / R8 / R9 memory
_LIVE_WORDS = re.compile(
    r"₹|\brs\.?\s*\d|\binr\b|trading at|share price|stock price|price (?:is|was|of)|running late|delayed by|"
    r"seats? (?:available|left)|\bwl\s*\d|\bpnr\b.*\bstatus\b",
    re.IGNORECASE,
)


def no_live_data_in_memory(output: dict, expected: dict) -> dict:
    """R7: nothing written to memory is a live figure (a price, a delay, seat counts)."""
    writes = [
        (i, c) for i, turn, _ in _turn_specs(output, expected)
        for c in turn.get("tool_calls", []) if c["tool"] in ("save_memory", "update_memory")
    ]
    if not writes:
        return _result(NA, "nothing written to memory")
    bad = [f"turn {i + 1}: {c['input'].get('text')!r}" for i, c in writes if _LIVE_WORDS.search(str((c.get("input") or {}).get("text", "")))]
    return _result(FAIL, "live data saved: " + "; ".join(bad)) if bad else _result(PASS, f"{len(writes)} memory writes, none live")


_MEMORY_ID_SOURCES = ("search_memories", "list_memories", "save_memory", "update_memory")


def memory_id_from_tool(output: dict, expected: dict) -> dict:
    """R8 (propagation): update_memory / delete_memory used an id that an earlier memory
    tool call actually returned, so the agent never invents an id.

    v2: v1 accepted only search/list results and so failed correct runs that reused
    the id save_memory had just returned (baseline, mem-002 and mem-003)."""
    seen_output = ""
    checked, bad = 0, []
    for i, turn, _ in _turn_specs(output, expected):
        for call in turn.get("tool_calls", []):
            if call["tool"] in ("update_memory", "delete_memory"):
                checked += 1
                memory_id = str((call.get("input") or {}).get("memory_id", ""))
                if not memory_id or memory_id not in seen_output:
                    bad.append(f"turn {i + 1}: {call['tool']}({memory_id!r}) with an id no earlier memory tool returned")
            if call["tool"] in _MEMORY_ID_SOURCES and call.get("status", "ok") == "ok":
                seen_output += _text(call.get("output"))
    if not checked:
        return _result(NA, "no memory updates or deletes")
    return _result(FAIL, "; ".join(bad)) if bad else _result(PASS, "every id came from an earlier tool result")


def memory_outcome(output: dict, expected: dict) -> dict:
    """R9 (system outcome): the memory store after the conversation contains what
    should be kept and none of what should be gone. Checks the store, not the
    agent's claim that it saved something."""
    contains = (expected or {}).get("memory_contains") or []
    absent = (expected or {}).get("memory_absent") or []
    if not contains and not absent:
        return _result(NA, "no memory outcome expected")
    memories = [m.casefold() for m in output.get("memories", [])]
    missing = [k for k in contains if not any(k.casefold() in m for m in memories)]
    present = [k for k in absent if any(k.casefold() in m for m in memories)]
    problems = [f"not saved: {missing}"] * bool(missing) + [f"should be gone: {present}"] * bool(present)
    return _result(FAIL, "; ".join(problems) + f" (store: {output.get('memories', [])})") if problems else _result(
        PASS, f"store holds {len(memories)} memories as expected"
    )


# --------------------------------------------------------------------------- R10 language
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_LATIN = re.compile(r"[A-Za-z]")


def answer_language(output: dict, expected: dict) -> dict:
    """R10: the answer is written mostly in the expected script (hi = Devanagari).
    URLs and markdown links are ignored, as are English source names."""
    checked, problems = False, []
    for i, turn, spec in _turn_specs(output, expected):
        lang = spec.get("answer_language")
        if not lang:
            continue
        checked = True
        text = _BARE_URL.sub("", _MD_LINK.sub("", turn.get("answer", "")))
        hindi, latin = len(_DEVANAGARI.findall(text)), len(_LATIN.findall(text))
        share = hindi / max(1, hindi + latin)
        ok = share >= 0.5 if lang == "hi" else share < 0.2
        if not ok:
            problems.append(f"turn {i + 1}: {share:.0%} Devanagari, expected {lang}")
    if not checked:
        return _result(NA, "no language expectation")
    return _result(FAIL, "; ".join(problems)) if problems else _result(PASS, "answered in the expected language")


# --------------------------------------------------------------------------- registry
EVALUATORS = [
    required_tools,
    prohibited_tools,
    tool_arguments,
    asks_before_acting,
    max_calls,
    news_links_grounded,
    price_grounded,
    station_resolved_first,
    no_live_data_in_memory,
    memory_id_from_tool,
    memory_outcome,
    answer_language,
]

REQUIREMENT_OF = {
    "required_tools": "R1",
    "prohibited_tools": "R4",
    "tool_arguments": "R5",
    "asks_before_acting": "R4",
    "max_calls": "R13",
    "news_links_grounded": "R2",
    "price_grounded": "R3",
    "station_resolved_first": "R6",
    "no_live_data_in_memory": "R7",
    "memory_id_from_tool": "R8",
    "memory_outcome": "R9",
    "answer_language": "R10",
}

LEVEL_OF = {  # book Ch 14: which level of evidence each check reads
    "required_tools": "action",
    "prohibited_tools": "action",
    "tool_arguments": "action",
    "asks_before_acting": "action",
    "max_calls": "trajectory",
    "news_links_grounded": "session",
    "price_grounded": "session",
    "station_resolved_first": "trajectory",
    "no_live_data_in_memory": "action",
    "memory_id_from_tool": "trajectory",
    "memory_outcome": "system outcome",
    "answer_language": "response",
}


REQUIREMENT_OF.update({"scope_compliance": "R11", "answers_question": "R12"})  # LLM judges (judges.py)
LEVEL_OF.update({"scope_compliance": "response (judge)", "answers_question": "response (judge)"})


def evaluate(output: dict, expected: dict, evaluators=None) -> dict[str, dict]:
    """Run every evaluator on one case; never let one broken evaluator hide the others."""
    results = {}
    for evaluator in evaluators if evaluators is not None else EVALUATORS:
        try:
            results[evaluator.__name__] = evaluator(output, expected)
        except Exception as error:  # an evaluator bug is reported, not raised
            results[evaluator.__name__] = {"label": "error", "score": None, "explanation": repr(error)}
    return results
