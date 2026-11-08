"""Compare two eval runs case by case (book Ch 20-21).

    cd backend
    .venv/Scripts/python -m evals.compare results/<baseline>.json results/<candidate>.json
    .venv/Scripts/python -m evals.compare --latest baseline-2575430bbe my-change   # by run name

A version comparison is only fair on the same cases with the same evaluators, and
only informative case by case:
  * per requirement: pass rates before and after (capability and regression read apart);
  * per case: fixed (failed before, passes now) and broken (passed before, fails now).
    Only these discordant cases say which version is better; McNemar's exact test asks
    whether "fixed vs broken" could be a coin flip;
  * every newly broken case is listed: those are the regressions to open first.
It warns when the runs differ in more than the agent (cases, evaluators) or when the
prompt and the model both changed (then the cause of a difference can't be known).
"""

from __future__ import annotations

import argparse
import json
import sys
from math import comb
from pathlib import Path

RESULTS_DIR = Path(__file__).with_name("results")


def load_run(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def latest_named(name: str) -> Path:
    matches = sorted(RESULTS_DIR.glob(f"*-{name}.json"))
    if not matches:
        raise SystemExit(f"no run named {name!r} in {RESULTS_DIR}")
    return matches[-1]


def case_outcomes(run: dict) -> dict[str, dict]:
    """case id -> {"passed": every repeat passed every applicable check, "failing": {evaluator}}."""
    out: dict[str, dict] = {}
    for r in run["records"]:
        entry = out.setdefault(r["case"]["id"], {"passed": True, "failing": set()})
        bad = {n for n, x in r["results"].items() if x["label"] in ("fail", "error")}
        entry["failing"] |= bad
        entry["passed"] &= not bad
    return out


def mcnemar_exact(fixed: int, broken: int) -> float:
    """Two-sided exact McNemar p-value: P(a split at least this uneven | fair coin)."""
    n = fixed + broken
    if n == 0:
        return 1.0
    k = min(fixed, broken)
    tail = sum(comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def pass_rates(run: dict, ids: set[str]) -> dict[str, tuple[int, int]]:
    """evaluator -> (pass, pass + fail) over the shared cases; n/a left out."""
    rates: dict[str, list[int]] = {}
    for r in run["records"]:
        if r["case"]["id"] not in ids:
            continue
        for name, x in r["results"].items():
            p, t = rates.setdefault(name, [0, 0])
            if x["label"] in ("pass", "fail", "error"):
                rates[name] = [p + (x["label"] == "pass"), t + 1]
    return {k: (v[0], v[1]) for k, v in rates.items()}


def compare(base: dict, cand: dict) -> dict:
    a, b = case_outcomes(base), case_outcomes(cand)
    shared = set(a) & set(b)
    fixed = sorted(i for i in shared if not a[i]["passed"] and b[i]["passed"])
    broken = sorted(i for i in shared if a[i]["passed"] and not b[i]["passed"])
    warnings = []
    if set(a) != set(b):
        warnings.append(f"different cases: compared only the {len(shared)} shared ones "
                        f"(only in baseline: {len(set(a) - shared)}, only in candidate: {len(set(b) - shared)})")
    ra, rb = pass_rates(base, shared), pass_rates(cand, shared)
    if set(ra) != set(rb):
        warnings.append(f"different evaluators: {sorted(set(ra) ^ set(rb))} ran in only one of the runs")
    ma, mb = base["meta"], cand["meta"]
    if ma["prompt_version"] != mb["prompt_version"] and ma["model"] != mb["model"]:
        warnings.append("both the prompt and the model changed: a difference can't be attributed to either")
    return {
        "baseline": ma, "candidate": mb, "shared": len(shared),
        "passed": (sum(a[i]["passed"] for i in shared), sum(b[i]["passed"] for i in shared)),
        "fixed": fixed, "broken": broken, "p_value": mcnemar_exact(len(fixed), len(broken)),
        "rates": {name: (ra.get(name), rb.get(name)) for name in sorted(set(ra) | set(rb))},
        "broken_detail": {i: sorted(b[i]["failing"]) for i in broken},
        "warnings": warnings,
    }


def _rate(x):
    return "—" if not x or not x[1] else f"{x[0]}/{x[1]} ({x[0] / x[1]:.0%})"


def render(c: dict) -> str:
    ma, mb = c["baseline"], c["candidate"]
    lines = [f"# {ma['name']} → {mb['name']}", "",
             f"- prompt `{ma['prompt_version']}` → `{mb['prompt_version']}` · model `{ma['model']}` → `{mb['model']}`",
             f"- Cases passing every check: {c['passed'][0]}/{c['shared']} → {c['passed'][1]}/{c['shared']}",
             f"- Fixed: {len(c['fixed'])} · Broken: {len(c['broken'])} · McNemar exact p = {c['p_value']:.3f}"
             + ("  (no discordant cases)" if not c["fixed"] and not c["broken"] else
                "  (could easily be chance)" if c["p_value"] > 0.1 else "")]
    lines += [f"- ⚠️ {w}" for w in c["warnings"]]
    lines += ["", "| Evaluator | Baseline | Candidate |", "|---|---|---|"]
    lines += [f"| `{n}` | {_rate(x)} | {_rate(y)} |" for n, (x, y) in c["rates"].items()]
    lines += ["", "## Newly broken (open these first)", ""]
    lines += [f"- `{i}`: now failing {', '.join(f'`{e}`' for e in fails)}" for i, fails in c["broken_detail"].items()] or ["None."]
    lines += ["", "## Newly fixed", ""] + ([f"- `{i}`" for i in c["fixed"]] or ["None."])
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Compare two eval runs case by case.")
    parser.add_argument("runs", nargs="*", help="baseline.json candidate.json")
    parser.add_argument("--latest", nargs=2, metavar=("BASELINE_NAME", "CANDIDATE_NAME"),
                        help="use the latest results file of each run name")
    args = parser.parse_args(argv)
    paths = [latest_named(n) for n in args.latest] if args.latest else args.runs
    if len(paths) != 2:
        parser.error("give two result files, or --latest NAME NAME")
    print(render(compare(load_run(paths[0]), load_run(paths[1]))))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
