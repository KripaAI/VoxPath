"""Run the VoxPath agent on the benchmark and score it with the code evaluators.

    cd backend
    .venv/Scripts/python -m evals.run                         # every case
    .venv/Scripts/python -m evals.run --skip-tags railway     # no RapidAPI calls
    .venv/Scripts/python -m evals.run --ids stock-004,mem-002 --repeat 3
    .venv/Scripts/python -m evals.run --phoenix --name baseline   # also a Phoenix experiment

What it does (book Ch 5-8, 20-21):
  * Builds the same agent the app uses, with the same tools and system prompt, but on an
    in-memory checkpointer and memory store: test runs never touch the Neon database,
    and each case gets its own user, so memories cannot leak between cases.
  * Runs every turn of every case, recording each tool call (input and full output)
    and, at the end, what the memory store holds.
  * Scores each run with every evaluator, one result per requirement.
  * Prints and saves a report: pass rate per evaluator (n/a left out), per tag, and
    the failures with their explanations and Phoenix trace ids.
  * With --phoenix, uploads the cases as a versioned dataset and runs them as a Phoenix
    experiment, so versions can be compared side by side in the Phoenix UI.

This calls the real model (OpenAI) and the real tools (yfinance, the news store,
RapidAPI for railway cases), so it costs a little and live answers vary. It is not a
pytest test; test_evals.py checks the evaluators and this runner offline.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND / ".env")
# Evals read message text and tool results, and must not mix their traces into the
# app's Phoenix project.
os.environ["LOG_USER_CONTENT"] = "true"
os.environ["PHOENIX_PROJECT_NAME"] = os.getenv("EVAL_PHOENIX_PROJECT", "voxpath-evals")

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.store.memory import InMemoryStore  # noqa: E402

import agent  # noqa: E402
import memory  # noqa: E402
import services  # noqa: E402
import tracing  # noqa: E402
from evals.evaluators import EVALUATORS, LEVEL_OF, REQUIREMENT_OF, evaluate  # noqa: E402

# The evaluators this run applies: the code checks, plus the LLM judges with --judges.
ACTIVE = list(EVALUATORS)

try:
    from opentelemetry import context as _otel_context
except ImportError:  # tracing packages not installed
    _otel_context = None

CASES_FILE = Path(__file__).with_name("cases.jsonl")
RESULTS_DIR = Path(__file__).with_name("results")


# --------------------------------------------------------------------------- cases
def load_cases(path: Path = CASES_FILE) -> list[dict]:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = [c["id"] for c in cases]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"duplicate case ids: {sorted(duplicates)}")
    return cases


def select_cases(cases: list[dict], ids=None, tags=None, skip_tags=None) -> list[dict]:
    chosen = cases
    if ids:
        chosen = [c for c in chosen if c["id"] in ids]
    if tags:
        chosen = [c for c in chosen if set(c.get("tags", [])) & set(tags)]
    if skip_tags:
        chosen = [c for c in chosen if not set(c.get("tags", [])) & set(skip_tags)]
    return chosen


def dataset_name(cases: list[dict]) -> str:
    """The cases' content fingerprint is the dataset version: edit cases.jsonl and the
    next run uploads a new dataset instead of silently changing an old one."""
    digest = hashlib.sha256(json.dumps(cases, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:8]
    return f"voxpath-benchmark-{digest}"


# --------------------------------------------------------------------------- running
class Harness:
    """The app's agent on isolated, in-memory state, driven from one event loop."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.store = None

    def start(self):
        tracing.setup_tracing()  # no-op unless PHOENIX_TRACING=true
        self.store = InMemoryStore(
            index={"dims": services.get_embedding_dims(), "embed": services.build_embeddings(), "fields": ["text"]}
        )
        self.loop.run_until_complete(agent.init_agent(InMemorySaver(), self.store))
        return self

    def run_case(self, turns: list[str], case_id: str = "case") -> dict:
        return self.loop.run_until_complete(run_case(turns, case_id, self.store))

    def close(self):
        self.loop.close()


async def run_case(turns: list[str], case_id: str, store) -> dict:
    """Run one conversation; return what the evaluators read."""
    run_id = uuid.uuid4().hex[:6]
    user_id, thread_id = f"eval-{case_id}-{run_id}", f"eval-{case_id}-{run_id}"
    recorded = []
    for message in turns:
        started = time.perf_counter()
        error = None
        # Inside a Phoenix experiment the current tracing context is the experiment's own
        # span, so the agent's spans became its children: no root of their own, no trace
        # id captured, and orphans in the voxpath-evals project. Start each turn in an
        # empty context so it is a normal, findable trace (book Ch 3).
        token = _otel_context.attach(_otel_context.Context()) if _otel_context else None
        with agent.record_turn() as turn:
            try:
                answer = await agent.agent_chat(message, thread_id=thread_id, user_id=user_id, username="eval")
            except Exception as failure:  # a crash is a result to report, not a reason to stop
                answer, error = "", repr(failure)
            finally:
                if token is not None:
                    _otel_context.detach(token)
        recorded.append({
            "user": message,
            "answer": answer,
            "error": error,
            "tool_calls": [
                {"tool": c["tool"], "input": c["input"], "output": c.get("output"), "status": c["status"]}
                for c in turn.tool_calls
            ],
            "trace_id": turn.trace_id,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "prompt_tokens": turn.prompt_tokens,
            "completion_tokens": turn.completion_tokens,
        })
    memories = []
    if store is not None:
        items = await store.asearch((memory.MEMORY_NAMESPACE, user_id), limit=200)
        memories = [item.value.get("text", "") for item in items]
    output = {
        "turns": recorded,
        "memories": memories,
        "prompt_version": agent.PROMPT_VERSION,
        "model": services.get_model_name(),
    }
    return json.loads(json.dumps(output, ensure_ascii=False, default=str))  # JSON-safe for Phoenix


# --------------------------------------------------------------------------- report
def summarize(records: list[dict]) -> dict:
    """records: [{"case", "repeat", "output", "results"}]. Pass rates leave n/a out."""
    by_evaluator = defaultdict(lambda: {"pass": 0, "fail": 0, "n/a": 0, "error": 0})
    by_tag = defaultdict(lambda: [0, 0])  # case-runs passing every applicable check, total
    case_runs = defaultdict(list)
    for r in records:
        failed = False
        for name, result in r["results"].items():
            by_evaluator[name][result["label"]] = by_evaluator[name].get(result["label"], 0) + 1
            failed |= result["label"] in ("fail", "error")
        for tag in r["case"].get("tags", []):
            by_tag[tag][0] += not failed
            by_tag[tag][1] += 1
        case_runs[r["case"]["id"]].append(not failed)
    return {
        "evaluators": {
            name: {**counts, "pass_rate": counts["pass"] / (counts["pass"] + counts["fail"]) if counts["pass"] + counts["fail"] else None}
            for name, counts in by_evaluator.items()
        },
        "tags": {tag: {"passed": p, "total": t} for tag, (p, t) in sorted(by_tag.items())},
        # pass^k (book Ch 1): a case counts only if it passed in every repeat
        "cases_all_runs_pass": sum(all(v) for v in case_runs.values()),
        "cases": len(case_runs),
    }


def render_report(records: list[dict], summary: dict, meta: dict) -> str:
    lines = [
        f"# VoxPath eval report: {meta['name']}",
        "",
        f"- When: {meta['when']}  ·  prompt_version `{meta['prompt_version']}`  ·  model `{meta['model']}`",
        f"- Cases: {summary['cases']}  ·  repeats: {meta['repeat']}  ·  dataset `{meta['dataset']}`",
        f"- Cases passing every check in every run (pass^k): **{summary['cases_all_runs_pass']} / {summary['cases']}**",
        "",
        "## By requirement (n/a left out of the rate)",
        "",
        "| Evaluator | Req | Level | Pass | Fail | n/a | Pass rate |",
        "|---|---|---|---|---|---|---|",
    ]
    for evaluator in ACTIVE:
        name = evaluator.__name__
        c = summary["evaluators"].get(name, {"pass": 0, "fail": 0, "n/a": 0, "pass_rate": None})
        rate = "—" if c["pass_rate"] is None else f"{c['pass_rate']:.0%}"
        lines.append(f"| `{name}` | {REQUIREMENT_OF[name]} | {LEVEL_OF[name]} | {c['pass']} | {c['fail']} | {c['n/a']} | {rate} |")
    lines += ["", "## By tag (a case run passes if no check failed)", "", "| Tag | Passed | Total |", "|---|---|---|"]
    lines += [f"| {tag} | {v['passed']} | {v['total']} |" for tag, v in summary["tags"].items()]
    lines += ["", "## Failures", ""]
    any_failure = False
    for r in records:
        bad = {n: x for n, x in r["results"].items() if x["label"] in ("fail", "error")}
        if not bad:
            continue
        any_failure = True
        traces = ", ".join(t["trace_id"] for t in r["output"]["turns"] if t.get("trace_id")) or "tracing off"
        lines.append(f"### {r['case']['id']} (run {r['repeat'] + 1}) · trace {traces}")
        lines.append(f"Expected: {r['case']['expected'].get('behaviour', '')}")
        for t in r["output"]["turns"]:
            tools = ", ".join(f"{c['tool']}({json.dumps(c['input'], ensure_ascii=False)[:80]})" for c in t["tool_calls"]) or "no tools"
            lines.append(f"- **User:** {t['user']}  \n  **Tools:** {tools}  \n  **Answer:** {t['answer'][:300]!r}" + (f"  \n  **Error:** {t['error']}" if t.get("error") else ""))
        lines += [f"- ❌ `{n}`: {x['explanation']}" for n, x in bad.items()]
        lines.append("")
    if not any_failure:
        lines.append("None.")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- main
def run_local(harness: Harness, cases: list[dict], repeat: int) -> list[dict]:
    records = []
    for case in cases:
        for k in range(repeat):
            output = harness.run_case(case["turns"], case["id"])
            records.append({"case": case, "repeat": k, "output": output, "results": evaluate(output, case["expected"], ACTIVE)})
            failed = [n for n, x in records[-1]["results"].items() if x["label"] in ("fail", "error")]
            print(f"  {case['id']:<10} run {k + 1}: {'FAIL ' + ', '.join(failed) if failed else 'pass'}", flush=True)
    return records


def run_phoenix(harness: Harness, cases: list[dict], repeat: int, name: str, dataset: str) -> list[dict]:
    from phoenix.client import Client

    client = Client(base_url=os.getenv("PHOENIX_BASE_URL", "http://localhost:6006"))
    try:
        ds = client.datasets.get_dataset(dataset=dataset)
        print(f"Using Phoenix dataset {dataset} ({ds.example_count} examples)")
    except Exception:
        ds = client.datasets.create_dataset(
            name=dataset,
            inputs=[{"turns": c["turns"]} for c in cases],
            outputs=[c["expected"] for c in cases],
            metadata=[{k: c.get(k) for k in ("id", "requirement", "tags", "source")} for c in cases],
            dataset_description="VoxPath agent benchmark (backend/evals/cases.jsonl). Name suffix = content hash.",
        )
        print(f"Uploaded Phoenix dataset {dataset} ({len(cases)} examples)")

    by_id = {c["id"]: c for c in cases}
    records, counter = [], defaultdict(int)
    verdicts: dict[str, dict] = {}  # run_key -> results, computed once in the task

    def reuse(evaluator):
        """Phoenix evaluator that reports the verdict already computed for this run, so
        LLM judges are not called a second time."""
        def evaluate_run(output):
            return verdicts[output["run_key"]][evaluator.__name__]
        evaluate_run.__name__ = evaluator.__name__
        return evaluate_run

    def task(input, metadata):  # noqa: A002 - the name Phoenix binds
        case = by_id[metadata["id"]]
        output = harness.run_case(input["turns"], case["id"])
        k = counter[case["id"]]
        counter[case["id"]] += 1
        output["run_key"] = f"{case['id']}#{k}"
        verdicts[output["run_key"]] = evaluate(output, case["expected"], ACTIVE)
        records.append({"case": case, "repeat": k, "output": output, "results": verdicts[output["run_key"]]})
        return output

    client.experiments.run_experiment(
        dataset=ds,
        task=task,
        evaluators=[reuse(e) for e in ACTIVE],
        experiment_name=name,
        experiment_metadata={"prompt_version": agent.PROMPT_VERSION, "model": services.get_model_name()},
        repetitions=repeat,
    )
    return records


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ids", help="comma-separated case ids")
    parser.add_argument("--tags", help="only cases with any of these tags")
    parser.add_argument("--skip-tags", help="leave out cases with any of these tags (e.g. railway)")
    parser.add_argument("--repeat", type=int, default=1, help="runs per case (pass^k needs > 1)")
    parser.add_argument("--name", default=f"run-{agent.PROMPT_VERSION}", help="experiment / report name")
    parser.add_argument("--phoenix", action="store_true", help="also upload the dataset and run a Phoenix experiment")
    parser.add_argument("--judges", action="store_true", help="also run the LLM judges (R11, R12): one judge call per reply")
    args = parser.parse_args(argv)
    split = lambda s: [x.strip() for x in s.split(",") if x.strip()] if s else None  # noqa: E731

    if args.judges:
        from evals.judges import JUDGE_EVALUATORS

        ACTIVE.extend(JUDGE_EVALUATORS)
    all_cases = load_cases()
    cases = select_cases(all_cases, split(args.ids), split(args.tags), split(args.skip_tags))
    if not cases:
        print("No cases selected.")
        return 2
    dataset = dataset_name(cases)
    print(f"{len(cases)} cases · prompt_version {agent.PROMPT_VERSION} · model {services.get_model_name()} · {dataset}")

    harness = Harness().start()
    try:
        records = (run_phoenix if args.phoenix else lambda h, c, r, *_: run_local(h, c, r))(
            harness, cases, args.repeat, args.name, dataset
        )
    finally:
        harness.close()

    summary = summarize(records)
    meta = {
        "name": args.name, "when": datetime.now().isoformat(timespec="seconds"), "repeat": args.repeat,
        "prompt_version": agent.PROMPT_VERSION, "model": services.get_model_name(), "dataset": dataset,
    }
    RESULTS_DIR.mkdir(exist_ok=True)
    stem = RESULTS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{args.name}"
    stem.with_suffix(".json").write_text(
        json.dumps({"meta": meta, "summary": summary, "records": records}, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8",
    )
    report = render_report(records, summary, meta)
    stem.with_suffix(".md").write_text(report, encoding="utf-8")
    print("\n" + report.split("## Failures")[0])
    print(f"Full report: {stem.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
