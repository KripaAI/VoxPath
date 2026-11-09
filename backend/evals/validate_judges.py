"""Measure the LLM judges against reviewed labels before trusting them (book Ch 12).

    cd backend
    .venv/Scripts/python -m evals.validate_judges                 # held-out split (the measurement)
    .venv/Scripts/python -m evals.validate_judges --split dev     # development split (for tuning)
    .venv/Scripts/python -m evals.validate_judges --judge scope_compliance

Each judge is treated as a classifier. The positive class is the FAILURE label
(NON_COMPLIANT, NOT_ANSWERED), because that is what the judge exists to catch:
  recall     = caught / all real failures       (missed failures are the costly error)
  precision  = caught / all failures flagged    (false alarms cost review time)
  agreement  = raw share of matching labels     (can look high while catching nothing)
  kappa      = agreement corrected for chance   (Cohen's kappa)
Every disagreement is listed with the judge's explanation: it means the judge, the
label, or the rubric is wrong (book Ch 12, Table 31), and only reading it tells which.

Rules this script keeps: rubric examples are development rows only (judges.py asserts
it); held-out rows are never used to tune. Once a held-out row has inspired a rubric
change, move it to "dev" and add a fresh held-out row.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

LABELS_FILE = Path(__file__).with_name("judge_labels.jsonl")
RESULTS_DIR = Path(__file__).with_name("results")


def load_labels(path: Path = LABELS_FILE) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def metrics(pairs: list[tuple[str, str]], fail_label: str) -> dict:
    """pairs: (reviewed label, judge label). Rows the judge couldn't label count as misses."""
    tp = sum(1 for r, j in pairs if r == fail_label and j == fail_label)
    fn = sum(1 for r, j in pairs if r == fail_label and j != fail_label)
    fp = sum(1 for r, j in pairs if r != fail_label and j == fail_label)
    tn = sum(1 for r, j in pairs if r != fail_label and j != fail_label)
    n = len(pairs)
    p_o = (tp + tn) / n if n else 0.0
    # chance agreement from each rater's own label frequencies
    reviewed_fail, judge_fail = (tp + fn) / n if n else 0, (tp + fp) / n if n else 0
    p_e = reviewed_fail * judge_fail + (1 - reviewed_fail) * (1 - judge_fail)
    kappa = (p_o - p_e) / (1 - p_e) if p_e < 1 else (1.0 if p_o == 1 else 0.0)
    return {
        "n": n, "tp": tp, "fn": fn, "fp": fp, "tn": tn,
        "recall": tp / (tp + fn) if tp + fn else None,
        "precision": tp / (tp + fp) if tp + fp else None,
        "agreement": p_o,
        "kappa": kappa,
    }


def _pct(x):
    return "—" if x is None else f"{x:.0%}"


def validate(judge_names: list[str], split: str, run_judge=None) -> dict:
    from evals.judges import JUDGE_MODEL, JUDGES
    from evals.judges import run_judge as default_run_judge

    run_judge = run_judge or default_run_judge
    labelled = load_labels()
    report = {"split": split, "judge_model": JUDGE_MODEL, "judges": {}}
    for name in judge_names:
        rows = [r for r in labelled if r["judge"] == name and (split == "all" or r["split"] == split)]
        results = []
        for row in rows:
            verdict = run_judge(name, row["user_message"], row["reply"], row.get("earlier"), labelled)
            results.append({"id": row["id"], "reviewed": row["label"], "judge": verdict["label"],
                            "explanation": verdict["explanation"], "reason": row.get("reason", ""),
                            "user_message": row["user_message"], "reply": row["reply"]})
            mark = "  " if verdict["label"] == row["label"] else "≠ "
            print(f"  {mark}{row['id']:<12} reviewed {row['label']:<14} judge {verdict['label']}", flush=True)
        m = metrics([(r["reviewed"], r["judge"]) for r in results], JUDGES[name]["fail"])
        report["judges"][name] = {"metrics": m, "results": results}
    return report


def render(report: dict) -> str:
    lines = [f"# Judge validation ({report['split']} split, judge model `{report['judge_model']}`)", ""]
    lines += ["| Judge | Cases | Failures (reviewed) | Recall | Precision | Agreement | Kappa |", "|---|---|---|---|---|---|---|"]
    for name, j in report["judges"].items():
        m = j["metrics"]
        lines.append(f"| `{name}` | {m['n']} | {m['tp'] + m['fn']} | {_pct(m['recall'])} | {_pct(m['precision'])} | "
                     f"{_pct(m['agreement'])} | {m['kappa']:.2f} |")
    lines += ["", "Confusion (positive = failure label): " + "; ".join(
        f"`{n}` TP {j['metrics']['tp']} FN {j['metrics']['fn']} FP {j['metrics']['fp']} TN {j['metrics']['tn']}"
        for n, j in report["judges"].items()), ""]
    lines += ["## Disagreements", ""]
    any_ = False
    for name, j in report["judges"].items():
        for r in j["results"]:
            if r["reviewed"] != r["judge"]:
                any_ = True
                lines += [f"### {r['id']} ({name}): reviewed {r['reviewed']}, judge {r['judge']}",
                          f"- User: {r['user_message']}", f"- Reply: {r['reply'][:300]!r}",
                          f"- Label reason: {r['reason']}", f"- Judge: {r['explanation']}",
                          "- Decide: wrong label, wrong judge (revise the rubric), or unclear requirement?", ""]
    if not any_:
        lines.append("None.")
    lines += ["", "Small sets move a lot: with 3 reviewed failures, recall can only be 0%, 33%, 67% or 100%. "
              "Labels marked 'proposed' still need the owner's review."]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    from evals.judges import JUDGES

    parser = argparse.ArgumentParser(description="Validate the LLM judges against reviewed labels.")
    parser.add_argument("--split", choices=["heldout", "dev", "all"], default="heldout")
    parser.add_argument("--judge", choices=list(JUDGES), help="only this judge")
    args = parser.parse_args(argv)
    report = validate([args.judge] if args.judge else list(JUDGES), args.split)
    text = render(report)
    RESULTS_DIR.mkdir(exist_ok=True)
    stem = RESULTS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-judges-{args.split}"
    stem.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    stem.with_suffix(".md").write_text(text, encoding="utf-8")
    print("\n" + text.split("## Disagreements")[0])
    print(f"Full report: {stem.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    BACKEND = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(BACKEND))
    from dotenv import load_dotenv

    load_dotenv(BACKEND / ".env")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
