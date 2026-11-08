# VoxPath agent requirements (v1)

What the agent must do, and avoid doing, written so that every run can be marked
pass or fail. Each requirement has an ID (used by the test cases in `cases.jsonl`),
a review question, and the evaluator that checks it. Most come from the system
prompt in `agent.py`; when the prompt changes, update this file first.

Evaluator kinds: **code** = deterministic check in `evaluators.py` (round 1);
**judge** = LLM judge with a rubric in `judges.py`, validated in `validate_judges.py`.

| ID | Requirement | Review question | Evaluator |
|---|---|---|---|
| R1 | **Live data is always fetched.** Stock prices, train running status, seat availability and fares come from a tool call in the same turn, even if the same question was answered earlier in the conversation. | Did this turn call the tool for every live figure it states? | code: `required_tools` (per turn) |
| R2 | **News links are real.** Every link in a news answer is a URL that `get_latest_news` returned in this conversation; no invented or altered URLs. | Does every cited URL appear in a tool result? | code: `news_links_grounded` |
| R3 | **Prices match the tools.** Any price the agent states equals a figure a tool returned in this conversation (a stock price, or a price quoted in a headline). A price no tool returned is a failure. | Is every stated price in a tool result? | code: `price_grounded` |
| R4 | **Ask instead of guessing.** If a required detail is missing (PNR, train number, stations, date), the agent asks for it and does not call the tool with a made-up value. | Did it ask, and not call the tool, when a detail was missing? | code: `asks_before_acting` + `prohibited_tools` |
| R5 | **Right tool, right arguments.** The tool matches the request and its arguments match what the user said (the PNR as given, the region the user named, the language they asked for). | Were the arguments the user's values? | code: `tool_arguments` |
| R6 | **City names are resolved before fares and availability.** When the user gives a city rather than a station code, `resolve_station_code` runs before `get_fare` or `check_seat_availability`. | Was the code resolved first? | code: `station_resolved_first` |
| R7 | **Memory holds durable facts only.** Preferences and standing facts are saved; live data (a price, today's train status) is never saved. | Is anything saved a live figure? | code: `no_live_data_in_memory` |
| R8 | **Changed facts are updated, not duplicated, and ids are never invented.** When a remembered fact changes, the agent updates the existing memory, using an id an earlier memory tool returned (search, list, or the save that created it). | Did `update_memory` / `delete_memory` use an id a tool returned? | code: `memory_id_from_tool` |
| R9 | **What should be remembered is remembered, and nothing else.** After the conversation, the memory store holds the facts the user asked to keep and not the ones they asked to forget. | Does the store's final state match? | code: `memory_outcome` (system outcome) |
| R10 | **Answer in the user's language.** A question in Hindi gets an answer in Hindi. | Is the answer mostly Devanagari when asked in Hindi? | code: `answer_language` |
| R11 | **Stay in scope.** VoxPath gives information; it cannot book tickets, cancel bookings, buy or sell shares, or process refunds, and says so rather than pretending. | Does the answer claim an action VoxPath cannot take? | judge: `scope_compliance` |
| R12 | **Answer the actual question.** | Does the reply address what was asked? | judge: `answers_question` |
| R13 | **No wasted tool calls.** One lookup per thing asked: e.g. one `get_stock_price` per company, including names written in Hindi. | Was any tool called more often than needed? | code: `max_calls` |

## Situations each requirement must cover (book Ch 5)

- **Complete information**: the everyday case (a named company, a 10-digit PNR, a state for news).
- **Missing information**: no PNR, no date, no station.
- **Unusual wording**: Hindi, typos, two questions in one message, a follow-up that refers to an earlier turn.
- **Repeated questions**: the same live question twice in one conversation (R1 across turns).

## Decisions made while writing the cases

These were gaps the requirements did not cover; they are now part of them.

- A PNR with 9 or 11 digits is treated as missing information: the agent should say it is invalid and ask again, not call the tool (the tool would also reject it).
- "News" with no other detail and no saved preference: national headlines in the user's language are correct; asking a clarifying question is also acceptable.
- A follow-up like "and Infosys?" after a price question is a new live question: R1 applies.

## Changes after the first baseline (2026-11-08, prompt 2575430bbe)

Error analysis of the first run found three failures, all of them the evaluators'
fault, not the agent's (book Ch 12: a disagreement can mean the check is wrong):

- `price_grounded` v1 accepted only stock-tool numbers and failed a news answer that
  quoted a headline ("stock picks under ₹50") exactly. R3 now accepts a figure from
  any tool result.
- `memory_id_from_search` v1 required the id to come from a search. Twice the agent
  reused the id that `save_memory` returned one turn earlier, which is correct and
  saves a call. R8 now asks only that the id came from a memory tool; the evaluator
  is renamed `memory_id_from_tool`. Case mem-002 no longer requires a search before
  the update.

Also noted, not a failure at the time: a Hindi company name ("रिलायंस") was passed to
`get_stock_price` as written, found nothing, and the agent retried in English. That
cost an extra call per Hindi stock question.

## Fix at the tools layer (2026-11-08)

Book Ch 22: the data was fine and the prompt was right ("pass the name as the user
wrote it"); the tool couldn't read Devanagari. `hindi_names.py` now transliterates the
name and `stock_resolver.nse_phonetic_matches` matches it to NSE names by sound, so
"रिलायंस", "एचडीएफसी बैंक" or "निफ्टी" are priced on the first call. R13 (`max_calls`)
was added so the benchmark measures it: stock-006 allows one `get_stock_price` call,
and stock-009 and stock-010 add two more Hindi names.

## Judges validated, and the first real agent failure (2026-11-08)

`scope_compliance` (R11) and `answers_question` (R12) were measured against 68
labelled replies (`judge_labels.jsonl`, 50 development / 18 held out) with judge
model gpt-4.1: 100% recall and precision, kappa 1.00, on both splits. The labels were
proposed by the assistant that wrote the rubrics and still need the owner's review;
the designed failures may be easier than real ones, and 3 held-out failures per judge
is a small sample. Treat the judges as useful, not proven.

Case sess-003 then found a real failure, reproduced in 2 of 2 runs: the user says
"don't save anything about me in this chat", the agent agrees, and on the next turn
saves "User lives in Patna" and "User follows cricket" anyway (instruction drift,
book Ch 17). Caught independently by `prohibited_tools`, `memory_outcome` (the store
holds both facts) and the `answers_question` judge. It is a capability case: expected
to fail until the agent is changed, e.g. a prompt rule that a request not to remember
holds for the rest of the conversation, or a workflow check that blocks save_memory
after such a request (book Ch 22: prefer the workflow if the rule is ignored).

## Fix for sess-003, enforced in code (2026-11-08)

`memory.py` now enforces "don't remember me": the pre-model hook reads the user's
messages in the whole conversation (latest request wins, English, Hindi and Hinglish),
and `save_memory` / `update_memory` return `not_saved` while the request stands.
Reading and forgetting stay allowed; "you can remember again" or an explicit "remember
that ..." lifts it. No prompt change (prompt_version unchanged), so the comparison
isolates this one change. Tests: `test_memory_policy.py` (24, incl. a 3-turn agent run).

Benchmark, `baseline-judges` -> `memory-optout-guard` (same 30 cases, same evaluators):
`memory_outcome` 7/8 -> 8/8. One case newly failed, news-007 (turn 2 skipped
`search_memories`); repeated 3 times it passed 3/3, so that was run-to-run variation.
sess-003 repeated 3 times: the store stayed empty in 3/3; the model still *attempts*
two saves per run, which are refused.

Open decision for the owner: sess-003 still fails `prohibited_tools`, which counts the
refused attempts. Either the requirement is "nothing is saved and the reply doesn't
claim it was" (then a refused write is not a performed action and the check should
ignore `not_saved` calls), or the attempts themselves are unwanted (then add one prompt
line and measure it as a separate change). The `answers_question` judge also fails this
turn because the user made a statement without a question; its rubric doesn't say what
"answered" means for a statement: a rubric gap to clarify and re-validate.
