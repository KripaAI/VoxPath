# VoxPath

An AI assistant for India: live **stock prices**, **news** with source links, and **Indian Railways** (PNR status, live train status, train search, seat availability, fares). Users sign in, chat with an agent that calls live tools, and can reopen past conversations from a sidebar.

- **Backend:** FastAPI + LangGraph agent (OpenAI), tools served over MCP, Postgres (Neon) for chat memory and analytics
- **Frontend:** Next.js 16 + React 19 + Tailwind
- **Tracing (optional):** Arize Phoenix, showing every agent step

```
 Browser (Next.js :3000)
        │  REST + Bearer token
        ▼
 FastAPI (:8000) ──► LangGraph agent ──► OpenAI
        │                 │
        │                 └──► MCP tools: stocks (yfinance) · news (crawler) · railways (IRCTC via RapidAPI) · memory
        ▼
 Postgres (Neon): users, threads, LangGraph checkpoints, long-term memory, analytics tables
        │
 Phoenix (:6006, optional): step-by-step traces, linked from chat_messages.trace_id
```

---

## Quick start (Windows)

1. Install **Python 3.11** and **Node.js 20+**.
2. Create `backend/.env` from the example and fill in the four required values:
   ```
   copy backend\.env.example backend\.env
   ```
   | Variable | What it is |
   |---|---|
   | `OPENAI_API_KEY` | OpenAI key for the agent and memory embeddings |
   | `DATABASE_URL` | Postgres connection string (Neon works; keep `?sslmode=require`) |
   | `JWT_SECRET` | Random secret for sign-in tokens: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
   | `RAPIDAPI_KEY` | RapidAPI key subscribed to the **IRCTC API** (`irctc-api2.p.rapidapi.com`) |
3. Double-click **`start_voxpath.bat`**. On first run it creates the Python environment and installs the frontend libraries (a few minutes). Then it starts the backend and frontend, each in its own window, and opens the app.
4. Open **http://localhost:3000**, create an account, and start chatting.

The database tables are created automatically when the backend starts.

## Manual setup (any OS)

**Backend**
```bash
cd backend
python -m venv .venv
.venv/Scripts/activate            # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
cp .env.example .env              # then fill it in
python main.py                    # http://127.0.0.1:8000
```
> Start it with `python main.py`, not `uvicorn main:app`. On Windows, `main.py` picks the event loop the async Postgres driver needs.

**Frontend**
```bash
cd frontend
npm install
npm run dev                       # http://localhost:3000
```
The frontend calls `http://127.0.0.1:8000` by default. To point it elsewhere, set `NEXT_PUBLIC_API_BASE` in `frontend/.env.local`. If the frontend runs on another address, add that address to `ALLOWED_ORIGINS` in `backend/.env`.

**Tracing (optional)**

Set `PHOENIX_TRACING=true` in `backend/.env`. `start_voxpath.bat` then installs and starts Phoenix at **http://localhost:6006**. To do it by hand:
```bash
pip install -r backend/requirements-tracing.txt          # into the backend's environment
python -m venv tracing/.venv && tracing/.venv/Scripts/pip install -r tracing/requirements.txt
tracing/.venv/Scripts/phoenix serve
```

---

## API

Interactive documentation is generated from the code. With the backend running, open:

- **http://127.0.0.1:8000/docs**: every endpoint, its inputs, response shape and errors, with "Try it out". Click **Authorize** and paste a token first.
- **http://127.0.0.1:8000/openapi.json**: the same as a file. In Postman, use **Import → Link** with this URL to get a ready-made collection.

### Authentication
Sign in to get a token, then send it on every other request:
```
Authorization: Bearer <access_token>
```
Tokens last 7 days (`TOKEN_TTL_HOURS`). The user is always taken from the token; requests never carry a user id.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/auth/register` | Create an account: `{username, password, email?}` → `{access_token, user}` |
| `POST` | `/auth/login` | Sign in with username or email: `{username, password}` → `{access_token, user}` |
| `GET` | `/auth/me` | The signed-in user (used to check a session is still valid) |
| `POST` | `/chat` | Send a message: `{message, thread_id}` → `{text, audio, turn_id}` |
| `GET` | `/conversations?limit=20&offset=0` | The user's chats, most recently used first: `[{id, title, created_at, last_used}]` |
| `GET` | `/conversations/{id}` | One chat with its messages: `{id, title, ..., messages: [{role, content}]}` |
| `DELETE` | `/conversations/{id}` | Hide a chat (`204`). It disappears from the app but stays in the database. |
| `POST` | `/feedback` | Rate an answer: `{turn_id, rating: 1 \| -1 \| 0, comment?}` (`0` clears the vote) → `204` |
| `GET` | `/health` | Liveness check |
| `GET` | `/news/status` | When the background news refresh last ran |

### Behaviour the frontend relies on
- **The frontend creates conversations.** Generate a UUID as `thread_id` for a new chat and send it with every message. The first message creates the conversation and names it after the first real question; greetings like "hi" are skipped. Without a `thread_id`, all of a user's messages go into one default chat.
- **Hidden chats can't take new messages.** After hiding the open chat, start a new one: sending to it returns `404`.
- **`404` means "not yours, doesn't exist, or hidden"**, deliberately the same answer for all three.
- **Dates** are ISO 8601 with a time zone.

### Errors

| Status | Meaning | Suggested handling |
|---|---|---|
| `401` | Missing, invalid or expired token | Send the user to sign-in |
| `404` | Conversation not found (see above) | Drop it from the list; start a new chat |
| `422` | Request failed validation | Show the field message from `detail` |
| `429` | Rate limit (login, sign-up, chat) | Ask the user to wait |
| `503` + `"retryable": true` | Database briefly unreachable, nothing ran | Safe to resend once after `Retry-After` seconds |
| `503` + `"retryable": false` | Database dropped mid-answer | Let the user decide whether to resend |

---

## Data and analytics

Everything lives in Postgres; tables are created or upgraded on startup.

| Table | One row per | Use |
|---|---|---|
| `users` | account | username (case-insensitive), optional email, bcrypt password hash |
| `threads` | conversation | owner, title, last used, `deleted_at` when hidden |
| `checkpoints*` | agent state | LangGraph's saved conversations (binary; read through the API) |
| `store*` | long-term memory | facts the agent remembers about a user, with vector search |
| `chat_messages` | chat turn | question, answer, tools used, status, latency, model, tokens, `cost_usd`, Phoenix `trace_id` |
| `tool_calls` | tool call | tool, input, result preview, status, error, latency (joins `chat_messages` on `turn_id`) |
| `auth_events` | sign-up / sign-in attempt | event, username tried, failure reason, IP |
| `news_crawl_runs` | news source per crawl | status (`Success`, `No items`, `HTTP 403`…), items found and saved, latency (kept 30 days) |
| `message_feedback` | thumbs up/down | rating and optional comment per answer |

Analytics writes happen after the response is sent and never fail a request. `chat_messages.trace_id` opens the matching trace in Phoenix.

```sql
-- Cost and speed per day
SELECT created_at::date, count(*), sum(cost_usd), avg(duration_ms) FROM chat_messages GROUP BY 1 ORDER BY 1;
-- Slow or failing tools
SELECT tool, count(*), avg(duration_ms), count(*) FILTER (WHERE status = 'error') AS errors FROM tool_calls GROUP BY 1;
-- News sources that failed in the last day
SELECT source, count(*) FROM news_crawl_runs
WHERE status <> 'Success' AND created_at > now() - interval '1 day' GROUP BY 1;
-- Disliked answers and their traces
SELECT m.user_input, m.agent_output, m.trace_id
FROM message_feedback f JOIN chat_messages m USING (turn_id) WHERE f.rating = -1;
```

`backend/backfill_chat_messages.py` copies conversations from before `chat_messages` existed into it. It's safe to re-run: rows already there are skipped.

---

## News

A background job crawls about 34 sources every 5 minutes (`NEWS_REFRESH_INTERVAL`): national and regional pages (Bihar, Uttar Pradesh) plus topic feeds for business, sports, technology, science, world and entertainment, in English and Hindi. The agent's news tool filters by **region**, **category**, **topic** and **language**, shows each story once across papers, and returns each headline with its link, publish time (IST) and summary, so answers cite their sources. Sources are configured in `backend/news_config.py`.

## Tests

```bash
cd backend
.venv/Scripts/python -m pytest -q
```
The tests use an in-memory fake database and never call OpenAI, RapidAPI or Neon.

## Project layout

```
backend/
  main.py             FastAPI app: startup, auth and chat endpoints, error handling
  conversations.py    /conversations endpoints (chat history)
  auth.py             accounts, passwords, tokens, conversation ownership
  agent.py            LangGraph agent, system prompt, per-turn recording
  mcp_server.py       the tools the agent can call (MCP)
  services.py         stocks, news search, LLM setup
  railway.py          IRCTC lookups via RapidAPI
  news_*.py           news sources, crawler, storage, background refresher
  memory.py           long-term memory tools
  analytics.py        analytics tables and best-effort recording
  tracing.py, trace_view.py   optional Phoenix tracing, cleaned-up trace view
  test_*.py           tests
frontend/src/
  app/chat/page.tsx                 chat page (behind sign-in)
  components/AuthGate.tsx           sign-in / sign-up
  components/ChatInterface.tsx      chat, sidebar wiring, feedback buttons
  components/ConversationSidebar.tsx
  lib/auth.ts, lib/conversations.ts API calls
tracing/              Phoenix server (own environment)
docs/                 technical documentation, visual overview, deployment roadmap
start_voxpath.bat     one-click start on Windows
```

More detail: [`docs/TECHNICAL_DOCUMENTATION.md`](docs/TECHNICAL_DOCUMENTATION.md) and [`docs/VISUAL_OVERVIEW.md`](docs/VISUAL_OVERVIEW.md).

## Security notes

- Never commit `backend/.env` or `frontend/.env.local`; both are git-ignored. Share secrets outside the repo.
- Set `LOG_USER_CONTENT=false` to keep what users type out of logs and traces.
- Set `REGISTRATION_OPEN=false` to stop new sign-ups.
- Phoenix listens only on `127.0.0.1`, because traces contain chat text.
