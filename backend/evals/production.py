"""Evaluate real traffic and feed it back into the benchmark (book Ch 24-25).

    cd backend
    .venv/Scripts/python -m evals.production                  # last 7 days, code checks
    .venv/Scripts/python -m evals.production --days 30 --judges
    .venv/Scripts/python -m evals.production --no-annotate --no-feedback   # read-only dry run

What it does:
  1. Reads the app's traces from Phoenix (project PHOENIX_PROJECT_NAME, default "voxpath")
     and rebuilds every conversation: each root span is one turn (question -> answer),
     its tool spans are the calls, and turns are grouped by session id in time order.
  2. Runs the checks that need no expected answer (live traffic has none): news links
     come from a tool result, prices match a tool result, nothing live saved to memory,
     memory ids come from a tool. With --judges, also the two LLM judges.
  3. Writes each verdict back to that turn's root span as a Phoenix annotation
     (annotator CODE or LLM), so traces can be filtered by failure in the UI.
  4. Copies users' thumbs up/down (message_feedback in Postgres, linked through
     chat_messages.trace_id) onto the same spans as HUMAN annotations "user_feedback".
  5. Saves every failing or thumbs-down turn, with the user's exact words, to
     production_candidates.jsonl. A person writes the expected behaviour and moves it
     into cases.jsonl (source "production"); it then guards against that failure for good.

Monitoring says where to look, reading the trace says what happened (book Ch 24):
open the annotated traces before changing anything.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
CANDIDATES_FILE = Path(__file__).with_name("production_candidates.jsonl")

# Checks that are meaningful without an expected outcome (reference-free).
REFERENCE_FREE = ("news_links_grounded", "price_grounded", "no_live_data_in_memory", "memory_id_from_tool")


# --------------------------------------------------------------------------- spans -> conversations
def _tool_params() -> dict[str, list[str]]:
    import agent
    import mcp_server

    params = {}
    for name in agent.AGENT_TOOLS:
        fn = getattr(getattr(mcp_server, name), "fn", getattr(mcp_server, name))
        params[name] = list(inspect.signature(fn).parameters)
    return params


def _parse_input(tool: str, value, params: dict[str, list[str]]) -> dict:
    """A tool span's input: JSON of the arguments, or the bare value of a one-argument call."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    names = params.get(tool) or ["input"]
    return {names[0]: value}


def _parse_output(value):
    """A tool span's output is the serialized ToolMessage; its content is the tool's JSON."""
    if not isinstance(value, str):
        return value
    try:
        message = json.loads(value)
    except ValueError:
        return value
    content = message.get("data", {}).get("content", message) if isinstance(message, dict) else message
    if isinstance(content, str):
        try:
            return json.loads(content)
        except ValueError:
            return content
    return content


def _is_missing(value) -> bool:
    return value is None or (isinstance(value, float) and value != value)  # NaN from pandas


def conversations_from_spans(df, params: dict[str, list[str]]) -> list[dict]:
    """[{"session_id", "turns": [{"user", "answer", "tool_calls", "trace_id", "span_id", "started"}]}]"""
    if df is None or len(df) == 0:
        return []
    roots = df[df["parent_id"].apply(_is_missing)]
    tools = df[df["span_kind"] == "TOOL"]
    calls_by_trace: dict[str, list] = defaultdict(list)
    for _, span in tools.sort_values("start_time").iterrows():
        name = span.get("attributes.tool.name") or str(span["name"]).replace("🔧", "").strip()
        calls_by_trace[span["context.trace_id"]].append({
            "tool": name,
            "input": _parse_input(name, span.get("attributes.input.value"), params),
            "output": _parse_output(span.get("attributes.output.value")),
            "status": "error" if span.get("status_code") == "ERROR" else "ok",
        })
    sessions: dict[str, list] = defaultdict(list)
    for _, root in roots.sort_values("start_time").iterrows():
        session = root.get("attributes.session.id")
        session = session if not _is_missing(session) and session else root["context.trace_id"]
        sessions[session].append({
            "user": "" if _is_missing(root.get("attributes.input.value")) else str(root["attributes.input.value"]),
            "answer": "" if _is_missing(root.get("attributes.output.value")) else str(root["attributes.output.value"]),
            "tool_calls": calls_by_trace.get(root["context.trace_id"], []),
            "trace_id": root["context.trace_id"],
            "span_id": root["context.span_id"],
            "started": str(root["start_time"]),
        })
    return [{"session_id": s, "turns": t} for s, t in sessions.items()]


def turn_view(conversation: dict, i: int) -> dict:
    """The conversation up to turn i, with earlier answers blanked: their tool results
    still ground this turn, but only this turn's answer is checked."""
    turns = [dict(t, answer="") for t in conversation["turns"][:i]] + [conversation["turns"][i]]
    return {"turns": turns, "memories": []}


# --------------------------------------------------------------------------- scoring
def score(conversations: list[dict], use_judges: bool) -> list[dict]:
    """One row per turn: its trace ids and every applicable verdict."""
    from evals import evaluators as ev

    code_checks = [getattr(ev, name) for name in REFERENCE_FREE]
    judge_names = ["scope_compliance", "answers_question"] if use_judges else []
    rows = []
    for conversation in conversations:
        for i, turn in enumerate(conversation["turns"]):
            if not turn["answer"]:
                continue
            results = {}
            view = turn_view(conversation, i)
            for check in code_checks:
                result = check(view, {})
                if result["label"] != "n/a":
                    results[check.__name__] = {**result, "kind": "CODE"}
            # Judges read the earlier turns (with their answers) as context.
            context = {"turns": conversation["turns"][: i + 1]}
            for name in judge_names:
                results[name] = {**_judge_last_turn(name, context), "kind": "LLM"}
            rows.append({"session_id": conversation["session_id"], "turn": i + 1, **{k: turn[k] for k in ("trace_id", "span_id", "user", "answer", "started")},
                         "results": results})
    return rows


def _judge_last_turn(judge_name: str, view: dict) -> dict:
    from evals.judges import JUDGES, run_judge

    *earlier_turns, last = view["turns"]
    earlier = []
    for t in earlier_turns:
        earlier += [{"role": "user", "content": t["user"]}, {"role": "assistant", "content": t["answer"]}]
    verdict = run_judge(judge_name, last["user"], last["answer"], earlier)
    if verdict["label"] == "error":
        return verdict
    ok = verdict["label"] == JUDGES[judge_name]["labels"][0]
    return {"label": "pass" if ok else "fail", "score": 1.0 if ok else 0.0, "explanation": verdict["explanation"]}


# --------------------------------------------------------------------------- feedback
def load_feedback(trace_ids: set[str]) -> dict[str, dict]:
    """trace id -> latest thumbs up/down for that turn, from Postgres (read-only)."""
    url = os.getenv("DATABASE_URL")
    if not url or not trace_ids:
        return {}
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT m.trace_id, f.rating, f.comment, f.updated_at FROM message_feedback f "
            "JOIN chat_messages m ON m.turn_id = f.turn_id WHERE m.trace_id = ANY(%s) ORDER BY f.updated_at",
            (list(trace_ids),),
        ).fetchall()
    return {trace_id: {"rating": rating, "comment": comment} for trace_id, rating, comment, _ in rows}


# --------------------------------------------------------------------------- output
def annotate(client, rows: list[dict], feedback: dict[str, dict]) -> int:
    """Write verdicts and feedback onto each turn's root span. Re-running replaces them
    (same name, span and identifier), so this is safe to schedule."""
    written = 0
    for row in rows:
        for name, result in row["results"].items():
            client.spans.add_span_annotation(
                span_id=row["span_id"], annotation_name=name, annotator_kind=result["kind"],
                label=result["label"], score=result["score"], explanation=result["explanation"][:1000],
                identifier="voxpath-evals", sync=True,
            )
            written += 1
        fb = feedback.get(row["trace_id"])
        if fb:
            client.spans.add_span_annotation(
                span_id=row["span_id"], annotation_name="user_feedback", annotator_kind="HUMAN",
                label="thumbs_up" if fb["rating"] > 0 else "thumbs_down", score=1.0 if fb["rating"] > 0 else 0.0,
                explanation=fb.get("comment") or None, identifier="voxpath-feedback", sync=True,
            )
            written += 1
    return written


def save_candidates(rows: list[dict], feedback: dict[str, dict], conversations: list[dict]) -> int:
    """Append failing / thumbs-down turns not already saved. The expected behaviour is
    left for a person to write (book Ch 7: never let a model decide what is correct)."""
    seen = set()
    if CANDIDATES_FILE.exists():
        seen = {json.loads(line)["trace_id"] for line in CANDIDATES_FILE.read_text(encoding="utf-8").splitlines() if line.strip()}
    by_session = {c["session_id"]: c for c in conversations}
    added = 0
    with CANDIDATES_FILE.open("a", encoding="utf-8") as f:
        for row in rows:
            failing = {n: r["explanation"] for n, r in row["results"].items() if r["label"] in ("fail", "error")}
            thumbs_down = feedback.get(row["trace_id"], {}).get("rating", 0) < 0
            if not (failing or thumbs_down) or row["trace_id"] in seen:
                continue
            turns = by_session[row["session_id"]]["turns"][: row["turn"]]
            f.write(json.dumps({
                "trace_id": row["trace_id"], "session_id": row["session_id"], "seen_at": row["started"],
                "turns": [t["user"] for t in turns],          # the user's exact words, verbatim
                "agent_answer": row["answer"], "failing_checks": failing,
                "user_feedback": feedback.get(row["trace_id"]),
                "expected_behaviour": "TODO: written by a person before this becomes a case",
            }, ensure_ascii=False) + "\n")
            added += 1
    return added


def summary(rows: list[dict], feedback: dict[str, dict]) -> str:
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for row in rows:
        for name, r in row["results"].items():
            counts[name][0] += r["label"] == "pass"
            counts[name][1] += r["label"] in ("pass", "fail", "error")
    lines = [f"{len(rows)} production turns scored", "", "| Check | Kind | Pass | Applicable |", "|---|---|---|---|"]
    for name, (p, t) in sorted(counts.items()):
        kind = "LLM" if name in ("scope_compliance", "answers_question") else "CODE"
        lines.append(f"| `{name}` | {kind} | {p} | {t} |")
    ups = sum(1 for fb in feedback.values() if fb["rating"] > 0)
    lines += ["", f"User feedback linked to traces: {ups} up, {len(feedback) - ups} down"]
    failing = [r for r in rows if any(x["label"] in ("fail", "error") for x in r["results"].values())]
    lines += ["", "Failing turns (open these traces first):"]
    lines += [f"- {r['trace_id']} · {r['user'][:70]!r} · " + ", ".join(n for n, x in r["results"].items() if x["label"] in ("fail", "error"))
              for r in failing] or ["- none"]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Score production traces and feed them back into the evals.")
    parser.add_argument("--days", type=float, default=7, help="look back this many days")
    parser.add_argument("--limit", type=int, default=5000, help="max spans to read")
    parser.add_argument("--judges", action="store_true", help="also run the LLM judges (one call per turn per judge)")
    parser.add_argument("--no-annotate", action="store_true", help="don't write annotations to Phoenix")
    parser.add_argument("--no-feedback", action="store_true", help="don't read thumbs up/down from Postgres")
    parser.add_argument("--no-candidates", action="store_true", help="don't save failing turns as candidate cases")
    args = parser.parse_args(argv)

    from phoenix.client import Client

    project = os.getenv("PHOENIX_PROJECT_NAME", "voxpath")
    client = Client(base_url=os.getenv("PHOENIX_BASE_URL", "http://localhost:6006"))
    df = client.spans.get_spans_dataframe(
        project_identifier=project, start_time=datetime.now(timezone.utc) - timedelta(days=args.days),
        limit=args.limit, timeout=60,
    )
    conversations = conversations_from_spans(df, _tool_params())
    rows = score(conversations, args.judges)
    feedback = {} if args.no_feedback else load_feedback({r["trace_id"] for r in rows})
    written = 0 if args.no_annotate else annotate(client, rows, feedback)
    added = 0 if args.no_candidates else save_candidates(rows, feedback, conversations)
    print(f"Project {project!r}: {len(conversations)} conversations")
    print(summary(rows, feedback))
    print(f"\nAnnotations written: {written} · new candidate cases: {added} ({CANDIDATES_FILE.name})")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(BACKEND))
    from dotenv import load_dotenv

    load_dotenv(BACKEND / ".env")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
