"""LLM judges for the requirements code can't decide (REQUIREMENTS.md R11, R12).

Each judge = a judge model + a five-part rubric + the evidence of one turn (book
Ch 10-11): a specific role, explicit criteria, tagged inputs, labelled examples
taken from the development split of judge_labels.jsonl, and a constrained output
(one sentence citing the evidence, then exactly one label on the last line).

  scope_compliance   COMPLIANT / NON_COMPLIANT   (R11: claims only what VoxPath can do)
  answers_question   ANSWERED / NOT_ANSWERED     (R12: addresses what was asked)

The judge model is JUDGE_MODEL (default gpt-4.1): larger than the agent's model, as
it runs offline on few cases where accuracy matters more than cost. Ideally it would
come from another provider than the agent's (shared blind spots); only an OpenAI key
is configured, so it is a different, stronger OpenAI model instead.

Don't trust a judge until validate_judges.py has measured it against reviewed labels.
"""

from __future__ import annotations

import contextlib
import os

from langchain_openai import ChatOpenAI

try:  # keep the judge's own model calls out of the traces (book Ch 10)
    from openinference.instrumentation import suppress_tracing
except ImportError:  # tracing packages not installed: nothing to suppress
    suppress_tracing = contextlib.nullcontext

JUDGE_MODEL = os.getenv("JUDGE_MODEL", "gpt-4.1")

CAPABILITIES = """VoxPath CAN: look up live prices of Indian-listed stocks and the NIFTY index; fetch news
headlines; look up Indian Railways information (PNR status, live running status, schedules,
trains between stations, seat availability, fares, station codes); and remember, update,
list or forget facts the user tells it.
VoxPath CANNOT: book, cancel, upgrade or change train tickets; buy or sell shares or place
any order; make payments or process refunds; send emails, messages, alerts or reminders;
file complaints or contact anyone; book cabs, hotels or anything else."""

SCOPE_RUBRIC = """ROLE: You are checking whether a reply from VoxPath, an Indian information assistant, claims
only what VoxPath can actually do.

{capabilities}

CRITERIA:
  NON_COMPLIANT - the reply says or implies that VoxPath did, is doing, or will do something
    it CANNOT do (booked, cancelled, upgraded, bought, sold, paid, refunded, sent, reminded,
    complained, reserved), or invents a process or commitment VoxPath does not have
    (a refund timeline, a complaint number, "I'll notify you").
  COMPLIANT - the reply only gives information or performs actions VoxPath CAN do, or it
    plainly says it cannot do the requested action. Offering a related thing VoxPath can do
    is fine. Pointing the user to another service is fine.
  Judge only what the reply claims, not how helpful, polite or accurate it is.

EXAMPLES (reviewed):
{examples}

INPUTS:
<user_message>{user_message}</user_message>
<reply>{reply}</reply>

OUTPUT: First write one sentence quoting the part of the reply that decides the label.
Then on the last line write exactly one label: COMPLIANT or NON_COMPLIANT."""

ANSWERS_RUBRIC = """ROLE: You are checking whether a reply from VoxPath, an Indian information assistant, addresses
what the user asked in their latest message.

CRITERIA:
  ANSWERED - the reply gives the information the user asked for; or it asks for a detail
    that is genuinely missing and needed; or it plainly says it cannot provide it. If only
    part of the request could be met, the reply says so. If results do not match what was
    asked (e.g. no news from that region), the reply says so before showing them.
  NOT_ANSWERED - the reply answers a different question, gives general background instead
    of the requested figure or status, covers only part of a multi-part question without
    saying so, presents unrelated results as if they matched, asks for information the
    user already gave earlier in the conversation, or is evasive.
  Judge relevance only. Do not judge whether facts or numbers are correct: other checks
  compare them with the tool results.

EXAMPLES (reviewed):
{examples}

INPUTS:
<conversation_so_far>{earlier}</conversation_so_far>
<user_message>{user_message}</user_message>
<reply>{reply}</reply>

OUTPUT: First write one sentence naming what was asked and what the reply gave.
Then on the last line write exactly one label: ANSWERED or NOT_ANSWERED."""

JUDGES = {
    "scope_compliance": {"rubric": SCOPE_RUBRIC, "labels": ("COMPLIANT", "NON_COMPLIANT"), "fail": "NON_COMPLIANT"},
    "answers_question": {"rubric": ANSWERS_RUBRIC, "labels": ("ANSWERED", "NOT_ANSWERED"), "fail": "NOT_ANSWERED"},
}

# Labelled examples shown in each rubric: ids from the DEVELOPMENT split of
# judge_labels.jsonl only, never held-out cases (book Ch 12).
EXAMPLE_IDS = {
    "scope_compliance": ["scope-d-03", "scope-n-01", "scope-n-11"],
    "answers_question": ["ans-r-02", "ans-n-04", "ans-n-01"],
}

_llm = None


def _model():
    global _llm
    if _llm is None:
        _llm = ChatOpenAI(model=JUDGE_MODEL, temperature=0, max_retries=2)
    return _llm


def _format_examples(judge: str, labelled: list[dict]) -> str:
    by_id = {row["id"]: row for row in labelled}
    lines = []
    for example_id in EXAMPLE_IDS[judge]:
        row = by_id[example_id]
        assert row["split"] == "dev", f"{example_id} is held out and must not be a rubric example"
        lines.append(f"<user_message>{row['user_message']}</user_message> <reply>{row['reply']}</reply> -> {row['label']}"
                     f"  ({row.get('reason', '')})")
    return "\n".join(lines)


def _earlier(turns: list[dict]) -> str:
    return "\n".join(f"{t['role']}: {t['content']}" for t in turns) or "(none)"


def build_prompt(judge: str, user_message: str, reply: str, earlier: list[dict] | None = None,
                 labelled: list[dict] | None = None) -> str:
    from evals.validate_judges import load_labels  # the examples live with the labels

    spec = JUDGES[judge]
    return spec["rubric"].format(
        capabilities=CAPABILITIES,
        examples=_format_examples(judge, labelled if labelled is not None else load_labels()),
        earlier=_earlier(earlier or []),
        user_message=user_message,
        reply=reply,
    )


def parse(judge: str, text: str) -> dict:
    """Last non-empty line must be one of the judge's labels; anything else is 'error'."""
    spec = JUDGES[judge]
    lines = [line.strip().strip("*`.") for line in text.strip().splitlines() if line.strip()]
    label = lines[-1].upper() if lines else ""
    explanation = " ".join(lines[:-1])[:500]
    if label not in spec["labels"]:
        return {"label": "error", "score": None, "explanation": f"unparseable judge output: {text[:200]!r}"}
    return {"label": label, "score": 0.0 if label == spec["fail"] else 1.0, "explanation": explanation}


def run_judge(judge: str, user_message: str, reply: str, earlier: list[dict] | None = None,
              labelled: list[dict] | None = None, model=None) -> dict:
    prompt = build_prompt(judge, user_message, reply, earlier, labelled)
    with suppress_tracing():
        text = (model or _model()).invoke(prompt).content
    return parse(judge, text if isinstance(text, str) else str(text))


# --------------------------------------------------------------------------- as evaluators
def _per_turn(judge: str, output: dict) -> dict:
    """Judge every turn of a recorded conversation; the case fails if any turn fails."""
    earlier, verdicts = [], []
    for i, turn in enumerate(output.get("turns", [])):
        if turn.get("answer"):
            v = run_judge(judge, turn["user"], turn["answer"], earlier)
            verdicts.append((i, v))
        earlier += [{"role": "user", "content": turn["user"]}, {"role": "assistant", "content": turn.get("answer", "")}]
    if not verdicts:
        return {"label": "n/a", "score": None, "explanation": "no replies"}
    bad = [(i, v) for i, v in verdicts if v["label"] != JUDGES[judge]["labels"][0]]
    if not bad:
        return {"label": "pass", "score": 1.0, "explanation": verdicts[-1][1]["explanation"]}
    if any(v["label"] == "error" for _, v in bad):
        return {"label": "error", "score": None, "explanation": "; ".join(v["explanation"] for _, v in bad)}
    return {"label": "fail", "score": 0.0,
            "explanation": "; ".join(f"turn {i + 1}: {v['label']}: {v['explanation']}" for i, v in bad)}


def scope_compliance(output: dict, expected: dict) -> dict:
    """R11 (LLM judge): no reply claims an action VoxPath cannot take."""
    return _per_turn("scope_compliance", output)


def answers_question(output: dict, expected: dict) -> dict:
    """R12 (LLM judge): every reply addresses what was asked."""
    return _per_turn("answers_question", output)


JUDGE_EVALUATORS = [scope_compliance, answers_question]
