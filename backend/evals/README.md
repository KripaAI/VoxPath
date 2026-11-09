# VoxPath evals

Behaviour tests for the agent, following *Evaluating & Improving Agentic
Applications* (DeepLearning.AI × Arize): written requirements, a versioned set of
test cases, and evaluators that each check one requirement.

| File | What it is |
|---|---|
| `REQUIREMENTS.md` | What the agent must do and avoid (R1–R12), each with a review question and its evaluator, plus every change made to them and why |
| `cases.jsonl` | The benchmark: one test case per line (request turns, expected behaviour, per-turn expectations, source, tags) |
| `evaluators.py` | Code evaluators, one per requirement; deterministic, no model calls |
| `run.py` | Runs the agent on the cases and scores every run |
| `judges.py` | Two LLM judges (R11 scope, R12 answers the question) with five-part rubrics |
| `judge_labels.jsonl` | 68 labelled replies (dev / held-out) the judges are measured against |
| `validate_judges.py` | Recall, precision, agreement and Cohen's kappa of each judge, with every disagreement |
| `compare.py` | Two runs case by case: fixed vs broken, McNemar exact p, newly broken cases first |
| `production.py` | Scores real traces from Phoenix, writes verdicts and thumbs up/down back as annotations, saves failing turns as candidate cases |
| `../test_evals.py` | Offline pytest checks: each evaluator on known-good and known-bad runs, the dataset's validity, and the runner end to end with a scripted model |

## Running

```bash
cd backend
.venv/Scripts/python -m evals.run --skip-tags railway          # local report only
.venv/Scripts/python -m evals.run --phoenix --name my-change    # + Phoenix dataset and experiment
.venv/Scripts/python -m evals.run --ids stock-004 --repeat 5    # pass^k on one case
```

- The run uses the app's real agent, model and tools, on **in-memory** state: it never
  writes to the Neon database. It costs a little OpenAI usage; railway cases also use
  RapidAPI quota (skip them with `--skip-tags railway`).
- Reports land in `results/` (git-ignored): a `.md` summary and a `.json` with every
  turn, tool call and verdict.
- With `--phoenix` (needs `pip install -r requirements-tracing.txt` and Phoenix running):
  the cases are uploaded as dataset `voxpath-benchmark-<hash>` (the hash changes when
  `cases.jsonl` changes, so old results keep pointing at the cases they ran on), and the
  run becomes an experiment you can compare with others in the Phoenix UI. With
  `PHOENIX_TRACING=true`, every case's agent trace goes to the separate project
  `voxpath-evals`, and the report lists the trace ids of failures.

## Reading results

- Results are per requirement. Never collapse them into one score: a policy failure
  hides behind passes elsewhere (book Ch 13, 19).
- `n/a` means the case doesn't exercise that requirement; it is left out of pass rates.
- A small count is a weak measurement: 25/25 still has a 95% interval of roughly
  87–100%, and a check with one applicable case says almost nothing.

## Comparing a change (book Ch 20–21)

1. Run the current version: `--phoenix --name baseline-<prompt_version>`.
2. Make **one** change (prompt, tool description, workflow...).
3. Run again with the same cases and evaluators: `--phoenix --name <change>`.
4. Compare in Phoenix, case by case. Open every case that passed before and fails now.

Every trace and `chat_messages` row now carries `prompt_version` (a hash of the system
prompt) and `model`, so production traffic can be grouped by version the same way:
`metadata['prompt_version'] == '...'` in the Phoenix search bar.

## Adding cases

Add a line to `cases.jsonl`; `test_evals.py` checks it is well formed. Good sources, in
order: production traces and thumbs-down answers (`source: "production"`), your own
failures with one detail changed, and designed cases for situations you haven't seen.
Write the expected behaviour yourself, never with a model.

## LLM judges (book Ch 10-12)

```bash
.venv/Scripts/python -m evals.validate_judges                # measure on the held-out labels
.venv/Scripts/python -m evals.run --skip-tags railway --judges --phoenix --name my-change
```

- Judge model: `JUDGE_MODEL` (default `gpt-4.1`), stronger than the agent's model. Its own
  calls are not traced. One judge call per reply per judge, so `--judges` costs more.
- Before trusting a judge, and after every rubric change: run `validate_judges`. Read every
  disagreement and decide whether the label, the rubric or the requirement is wrong.
- **Review `judge_labels.jsonl`** (`labelled_by` says "proposed"): the labels are a
  starting point, not ground truth, until you have checked them. Add harder, real cases.
- Rubric examples must come from `dev` rows; a held-out row that inspired a rubric change
  moves to `dev` and is replaced by a fresh held-out row.

## Comparing two runs (book Ch 21)

```bash
.venv/Scripts/python -m evals.compare --latest baseline-judges my-change
```

Shows fixed and broken cases (and McNemar's exact p for "fixed vs broken could be
chance"), pass rates per requirement, and warns if the cases, the evaluators, or both
prompt and model changed.

## Production (book Ch 24-25)

```bash
.venv/Scripts/python -m evals.production --days 7            # code checks
.venv/Scripts/python -m evals.production --days 7 --judges   # + judges
```

Reads the app's traces from Phoenix, rebuilds each conversation, runs the checks that need
no expected answer, writes each verdict onto the turn's trace (filter by annotation in the
Phoenix UI), copies thumbs up/down from Postgres onto the same traces, and appends every
failing or thumbs-down turn, with the user's exact words, to `production_candidates.jsonl`
(git-ignored: it holds real messages). Write the expected behaviour for a candidate, move it
into `cases.jsonl` with `source: "production"`, and it guards against that failure for good.
Re-running is safe: annotations are replaced, candidates are not duplicated.

Only turns traced with `PHOENIX_TRACING=true` can be scored or linked to feedback.
