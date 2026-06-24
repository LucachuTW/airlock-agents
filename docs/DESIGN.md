# Technical Design — Agentic Enterprise Platform

## 1. Repository structure

```
agentic-enterprise/
├── README.md
├── pyproject.toml                  # uv; ruff + mypy config
├── .env.example
├── Makefile                        # up, dev, migrate, seed, test, eval, observability
├── models.yaml                     # hardware tier → chat/embedding models
├── app/
│   ├── main.py                     # app factory, lifespan (checkpointer, expiry loop)
│   ├── config.py                   # pydantic-settings
│   ├── db.py                       # async engine + session dependency
│   ├── models.py                   # SQLAlchemy models (one file)
│   ├── auth.py                     # JWT + argon2 + role dependencies
│   ├── llm.py                      # hardware probe → model tier, Ollama clients
│   ├── telemetry.py                # OTel + Prometheus metrics + Langfuse handler
│   ├── routers/
│   │   ├── auth.py
│   │   ├── runs.py                 # start run, SSE stream, list/get, feedback
│   │   ├── approvals.py            # queue + decide (resumes the graph)
│   │   ├── admin.py                # users, grants, audit; + /tools router
│   │   └── evals.py                # eval results (read)
│   ├── agents/
│   │   ├── graph.py                # supervisor assembly (langgraph-supervisor)
│   │   ├── docs_qa.py / analyst.py / actions.py
│   │   └── tools.py                # registry → MCP binding, audit wrapper, HITL gate
│   └── services/
│       ├── audit.py                # append-only audit + OTel trace id
│       ├── ingest.py               # chunking + local embeddings → pgvector
│       └── runs.py                 # agent build, streaming, interrupt/resume
├── mcp_servers/
│   ├── docs/server.py              # search_docs → chunks + sources
│   ├── db/server.py                # list_tables, run_query (sqlglot guard, RO role)
│   └── tickets/server.py           # create_ticket (approval-gated, idempotent)
├── migrations/                     # Alembic (0001 schema, 0002 sales+RO role, 0003 route, 0004 real retail)
├── scripts/                        # fetch_data.py (real datasets), seed.py, pull_models.py
├── data/                           # downloaded datasets (gitignored)
├── evals/
│   ├── datasets/                   # golden JSONL per suite (real ground truths)
│   ├── judges.py                   # LLM-as-judge prompts + scoring
│   ├── thresholds.yaml             # CI gate thresholds
│   ├── run_offline.py              # gate entrypoint
│   └── run_online.py               # sampler over production runs
├── deploy/
│   ├── docker-compose.yml          # core + profiles: observability (Langfuse v3), gpu
│   ├── Dockerfile                  # shared by API and MCP servers
│   └── k8s/                        # kustomize base + dev overlay
├── tests/
└── docs/DESIGN.md
```

## 2. Architecture

### Request lifecycle

1. `POST /runs` with JWT → resolve user + role → load granted tools from `tools` ⋈ `role_tool_grants`.
2. Build the LangGraph supervisor graph **with only those tools bound** (least privilege at construction time).
3. Graph executes; every MCP tool call passes through an audit middleware (log → call → log result) and the MCP server re-validates the grant (`X-User-Role` propagated, server-side check).
4. If a `critical` tool is invoked → `interrupt()` → checkpoint persisted to Postgres → run status `waiting_approval`, `approvals` row created → SSE stream tells the client.
5. Approver decides via `POST /approvals/{id}` → graph resumed from checkpoint with the decision injected → tool executes (or the agent is told it was rejected and replies accordingly).
6. Run finishes → tokens/cost/latency written to `runs`, trace exported.

### Component boundaries

| Component | Responsibility | Trust decisions |
|---|---|---|
| FastAPI | authn/z, run lifecycle, streaming (SSE), approvals | validates JWT, filters tools per role |
| LangGraph graphs | orchestration, HITL interrupts, checkpointing | trusts nothing; tools are pre-filtered |
| MCP servers | actual integration logic + input validation | **the** trust boundary: SELECT-only SQL, payload schemas, grant re-check |
| Postgres | persistence, pgvector, LangGraph checkpoints | read-only role for `mcp-db` |
| Redis | embedding/query cache, rate limits, SSE pubsub | ephemeral only — no source of truth |

### State & concurrency

- LangGraph `PostgresSaver` checkpointer: durable runs, resume-after-approval, resume-after-crash. `thread_id = run_id`.
- Redis: per-user rate limit (token bucket), cache of embeddings and repeated doc queries (`SETEX`, key = hash(query)), pubsub channel per run for streaming from workers.
- Long runs execute in a background task; API stays stateless → horizontally scalable.

### Local model serving (Ollama)

All inference is local via Ollama's OpenAI-compatible API (`langchain-ollama`). `app/llm.py` probes hardware at startup (`nvidia-smi --query-gpu=memory.total`, `/proc/meminfo`) and selects a tier from `models.yaml`; `MODEL_TIER` / `CHAT_MODEL` env vars override.

| Tier | Condition | Chat / judge | Embeddings |
|---|---|---|---|
| `gpu_24` | ≥ 20 GB VRAM | `qwen3:32b` | `nomic-embed-text` (768d) |
| `gpu_16` | ≥ 12 GB VRAM | `qwen3:14b` | `nomic-embed-text` |
| `gpu_8`  | ≥ 6 GB VRAM  | `qwen3:8b`  | `nomic-embed-text` |
| `cpu`    | otherwise (≥16 GB RAM) | `qwen3:4b` | `nomic-embed-text` |

- Judge = the tier's chat model (documented limitation: self-judging on small tiers; on `gpu_24` a distinct judge model is configurable).
- Single-GPU reality: one resident model, Ollama serializes requests (`OLLAMA_NUM_PARALLEL=1..2`); embeddings model is small enough to coexist.
- `make seed` pulls the tier's models automatically.
- Upgrade path for production K8s: vLLM behind the same OpenAI-compatible interface — only the base URL changes.
- Cost accounting: no per-call USD. Metrics are tokens in/out + latency (throughput derivable); an optional USD-equivalent is computed as `tokens × PRICE_PER_MTOKEN` (env, default 0) for cloud comparisons.

### Real datasets

Both data sources are real, public and reproducibly fetched by `scripts/fetch_data.py` (`make data`,
cached under `data/`, gitignored):

- **Docs corpus**: 8 policy pages of the public GitLab Handbook (CC BY-SA 4.0) — PTO, expenses,
  password standard, access management, audit logging, onboarding, incident management. Chunk
  `source_uri`s are the live handbook URLs, so citations in answers are verifiable links.
- **Analytics DB**: UCI Online Retail (#352, CC BY 4.0) — 541,909 invoice lines of a UK online
  retailer (Dec 2010 – Dec 2011), COPY-loaded into `sales.transactions`. Real-world messiness is
  kept (cancellations = invoice `C…` with negative quantity; NULL customer ids) and documented in a
  `comment on table` that mcp-db's `list_tables` surfaces to the agent as a data dictionary.
- **Eval ground truths** (`evals/datasets/*.jsonl`) are facts extracted from the real pages and
  figures computed by SQL against the loaded dataset.

## 3. Database schema (PostgreSQL 16 + pgvector)

```sql
create extension if not exists vector;

create table users (
  id            uuid primary key default gen_random_uuid(),
  email         text unique not null,
  password_hash text not null,
  role          text not null check (role in ('admin','analyst','viewer')),
  is_active     boolean not null default true,
  created_at    timestamptz not null default now()
);

-- Tool registry: what exists, where it lives, how dangerous it is
create table tools (
  name        text primary key,               -- 'search_docs', 'run_query', 'create_ticket'
  mcp_server  text not null,                  -- 'docs' | 'db' | 'tickets'
  description text not null,
  risk        text not null check (risk in ('read','write','critical')),
  enabled     boolean not null default true
);

create table role_tool_grants (
  role      text not null,
  tool_name text not null references tools(name),
  primary key (role, tool_name)
);

create table runs (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references users(id),
  agent       text,                            -- what to rebuild on resume: 'auto' or a direct agent
  route       text,                            -- subagent that actually handled it (metrics/evals)
  status      text not null default 'running'
              check (status in ('running','waiting_approval','completed','failed','rejected')),
  input       text not null,
  output      text,
  trace_id    text,                            -- OTel trace id, links everything
  model       text,
  tokens_in   integer default 0,
  tokens_out  integer default 0,
  latency_ms  integer,
  created_at  timestamptz not null default now(),
  finished_at timestamptz
);
create index on runs (user_id, created_at desc);
create index on runs (status) where status = 'waiting_approval';

create table approvals (
  id           uuid primary key default gen_random_uuid(),
  run_id       uuid not null references runs(id),
  tool_name    text not null references tools(name),
  payload      jsonb not null,                 -- exact args the agent wants to execute
  status       text not null default 'pending'
               check (status in ('pending','approved','rejected','expired')),
  decided_by   uuid references users(id),
  decision_note text,
  expires_at   timestamptz not null,           -- default now() + interval '24 hours'
  decided_at   timestamptz,
  created_at   timestamptz not null default now()
);

-- Append-only, enforced with a BEFORE UPDATE OR DELETE trigger that raises
-- (grants alone don't bind the table owner).
create table audit_log (
  id         bigint generated always as identity primary key,
  actor_id   uuid,                              -- user or NULL for system
  run_id     uuid,
  action     text not null,                     -- 'tool.call','tool.result','approval.decided','admin.grant', ...
  resource   text,                              -- tool name, user id, ...
  detail     jsonb,
  trace_id   text,
  created_at timestamptz not null default now()
);
create index on audit_log (run_id);
create index on audit_log (created_at);

-- RAG corpus
create table documents (
  id         uuid primary key default gen_random_uuid(),
  title      text not null,
  source_uri text not null,                    -- for citations
  version    integer not null default 1,
  created_at timestamptz not null default now()
);

create table chunks (
  id          uuid primary key default gen_random_uuid(),
  document_id uuid not null references documents(id) on delete cascade,
  ord         integer not null,
  content     text not null,
  embedding   vector(768) not null            -- nomic-embed-text
);
create index on chunks using hnsw (embedding vector_cosine_ops);

-- Demo target system for the actions agent (stands in for Jira/ServiceNow).
-- unique(approval_id) makes execution idempotent per approval.
create table tickets (
  id          uuid primary key default gen_random_uuid(),
  approval_id uuid not null unique references approvals(id),
  title       text not null,
  body        text not null,
  priority    text not null,
  created_at  timestamptz not null default now()
);

-- Evaluation
create table eval_cases (
  id       uuid primary key default gen_random_uuid(),
  suite    text not null,                      -- 'docs_qa','analyst','routing'
  input    text not null,
  expected jsonb not null,                     -- expected answer / rows / route
  metadata jsonb
);

create table eval_runs (
  id         uuid primary key default gen_random_uuid(),
  suite      text not null,
  kind       text not null check (kind in ('offline','online')),
  git_sha    text,
  model      text,
  created_at timestamptz not null default now()
);

create table eval_results (
  id          uuid primary key default gen_random_uuid(),
  eval_run_id uuid not null references eval_runs(id),
  case_id     uuid,                            -- NULL for online (references a run instead)
  run_id      uuid references runs(id),        -- online: which prod run was judged
  scores      jsonb not null,                  -- {"faithfulness":0.9,"citation_precision":1.0,...}
  passed      boolean not null,
  tokens      integer,
  latency_ms  integer
);
create unique index on eval_results (eval_run_id, coalesce(case_id, run_id));

create table feedback (
  id         uuid primary key default gen_random_uuid(),
  run_id     uuid not null references runs(id),
  user_id    uuid not null references users(id),
  rating     smallint not null check (rating in (-1, 1)),
  comment    text,
  created_at timestamptz not null default now(),
  unique (run_id, user_id)
);

-- LangGraph checkpoint tables are created/managed by langgraph-checkpoint-postgres.
```

## 4. API endpoints

All under `/api/v1`, JWT bearer auth except `/auth/*` and `/health`.

| Method | Path | Role | Description |
|---|---|---|---|
| POST | `/auth/login` | — | email+password → JWT |
| POST | `/auth/refresh` | — | refresh token |
| POST | `/runs` | any | `{message, agent?}` → `{run_id, stream_url}`; agent defaults to `auto` (supervisor); direct names are used by the eval suite |
| GET | `/runs/{id}/events` | owner | SSE: agent steps, tokens, interrupts, result |
| GET | `/runs/{id}` | owner/admin | run detail incl. tokens, latency, trace link |
| GET | `/runs` | owner/admin | list + filters (status, agent, date) |
| POST | `/runs/{id}/feedback` | owner | 👍/👎 + comment |
| GET | `/approvals?status=pending` | admin, analyst* | approval queue (*grantable) |
| POST | `/approvals/{id}` | approver | `{decision: approved\|rejected, note}` → resumes run |
| GET | `/tools` | any | tools visible to *my* role |
| POST | `/admin/users` | admin | create user with role |
| PUT | `/admin/grants` | admin | `{role, tool_name, granted: bool}` |
| GET | `/admin/audit` | admin | filterable audit log |
| POST | `/evals/offline` | admin | trigger offline suite → `eval_run_id` |
| GET | `/evals/runs/{id}` | admin | scores per case, aggregates |
| GET | `/metrics` | — | Prometheus exposition |
| GET | `/health` | — | liveness/readiness |

Approval decisions and admin grant changes always write `audit_log` rows.

## 5. Agents & tools

### Supervisor (LangGraph)

Prebuilt supervisor pattern: an LLM node with the three subagents exposed as handoff tools, plus a direct-answer path for trivial queries. Routing decision is recorded on `runs.agent` (evaluated offline for routing accuracy).

```python
graph = create_supervisor(
    agents=[docs_qa, analyst, actions],   # each built with role-filtered tools
    model=llm.chat_model(),               # tier-selected local model via Ollama
    checkpointer=PostgresSaver(pool),
)
```

### Subagents

| Agent | Tools (MCP) | Behavior |
|---|---|---|
| `docs_qa` | `search_docs(query, k)` | ReAct over retrieval; system prompt requires inline citations `[n]` mapped to `source_uri`; refuses to answer without retrieved support |
| `analyst` | `list_tables()`, `run_query(sql)` | plans → queries (read-only) → composes a report with the actual result tables; row limit enforced server-side |
| `actions` | `create_ticket(title, body, priority)` | drafts the ticket, then `interrupt()` with the exact payload; on resume: approved → execute + confirm, rejected → report the rejection and the note |

### HITL detail

```python
# actions agent, before executing any risk='critical' tool
decision = interrupt({"tool": "create_ticket", "payload": payload})  # ← checkpoint + pause
if decision["status"] == "approved":
    result = mcp.call("create_ticket", payload)   # exact approved payload, not re-generated
```

The approval executes **the payload the human saw**, never a regenerated one. Expiry: a periodic task marks stale `pending` approvals `expired` and fails the run.

### Tool registry → runtime binding

`app/agents/tools.py` loads `tools ⋈ role_tool_grants` for the user's role and produces LangChain tool objects wrapping MCP client calls (via `langchain-mcp-adapters`). Wrapper responsibilities: audit log before/after, propagate trace id + role headers, map MCP errors to agent-readable messages.

### MCP servers (FastMCP, one process each)

| Server | Tools | Validation at the boundary |
|---|---|---|
| `mcp-docs` | `search_docs` | k ≤ 20; returns `{content, source_uri, score}` per chunk |
| `mcp-db` | `list_tables`, `run_query` | `sqlglot` parse → reject non-SELECT & disallowed schemas; connects with a read-only Postgres role; row cap 500; statement timeout 10s |
| `mcp-tickets` | `create_ticket` | pydantic payload schema; requires the `approval_id` header proving a decided approval; idempotency key = approval id |

## 6. Evaluation pipeline

### Offline (runs in CI, gates merges)

```
evals/run_offline.py --suite all --gate
  for each case: execute agent → collect output + trace
  score:
    docs_qa   → citation_precision (cited chunks ⊆ retrieved & relevant, assertion)
                faithfulness, answer_relevance (LLM-as-judge, rubric 1–5 → /5)
    analyst   → result correctness (expected rows vs. returned rows, assertion)
                report_quality (judge)
    routing   → exact-match: supervisor picked expected agent
    actions   → interrupt fired for critical tool (assertion — a safety regression fails CI)
  write eval_runs/eval_results; print aggregate; exit 1 if any suite < threshold
```

Thresholds live in `evals/thresholds.yaml` (e.g. `docs_qa.faithfulness >= 0.85`, `routing.accuracy >= 0.9`, `actions.interrupt_rate == 1.0`). Judge model is pinned and versioned; judge prompts are in-repo and diffable.

### Online

- `run_online.py` (cron / K8s CronJob): samples N% of completed runs from the last window, scores with the same judges, writes `eval_results (kind='online')`.
- User feedback (`feedback` table) joined with judge scores → detect drift between judge and users.
- Dashboards plot offline-vs-online score series per suite; alert on drop > X over 7 days.

## 7. Observability

- **OpenTelemetry**: auto-instrumentation for FastAPI, SQLAlchemy, httpx, redis; manual spans per agent node and per MCP tool call. Export OTLP → collector → Tempo (traces) + Prometheus (metrics) + Grafana. `trace_id` stored on `runs` and `audit_log` → one click from a run row to its full distributed trace.
- **Langfuse (self-hosted)**: LLM-level traces (prompts, tool calls, token usage) via `langfuse.langchain.CallbackHandler`, with `run_id`/`trace_id` as metadata so app traces and LLM traces cross-link. Chosen over LangSmith/MLflow/W&B Weave for local-first coherence: no prompt leaves the machine. Runs in the compose `observability` profile (Langfuse v3 brings its own ClickHouse + MinIO). Swappable — tracing goes through one module, `app/telemetry.py`.
- **Prometheus metrics** (exposed on `/metrics`): `runs_total{agent,status}`, `run_latency_seconds{agent}` (histogram), `run_cost_usd_total{agent,model}`, `tokens_total{direction}`, `tool_calls_total{tool,status}`, `approvals_total{decision}`, `approval_wait_seconds`.

## 8. Deployment

### Local / demo

`docker compose up`: `postgres` (pgvector image), `redis`, `mcp-docs`, `mcp-db`, `mcp-tickets`, `otel-collector`; profiles: `observability` (Langfuse v3 + ClickHouse + MinIO, Grafana, Tempo, Prometheus), `gpu` (containerized Ollama with NVIDIA device reservation — by default the API talks to the host's Ollama). One `Dockerfile`, multi-stage (uv → slim runtime, non-root user); API and MCP servers share the image with different entrypoints.

### Kubernetes (`deploy/k8s/`)

- `Deployment` per service (api ×2+, each MCP server ×1+, `ollama` with `nvidia.com/gpu` resource + nodeSelector — or vLLM at scale), `HPA` on the api (CPU + p95 latency via custom metric).
- `Job` for Alembic migrations (pre-deploy hook), `CronJob` for online eval + approval expiry.
- `Secret` for DB/Redis/LLM keys (SealedSecrets or external-secrets in a real cluster); `NetworkPolicy`: only api → MCP servers, only mcp-db → postgres read-only user.
- Probes: `/health` readiness checks DB+Redis; liveness is a trivial ping.
- Ingress + TLS; Postgres/Redis assumed managed (RDS/Cloud SQL) — manifests include dev-only StatefulSets behind a kustomize overlay.

### CI/CD

GitHub Actions: lint (ruff, mypy) → unit tests → **offline eval gate** → build/push image → deploy overlay. Eval gate is the differentiating step: a prompt or model change that degrades quality cannot ship. Hosted runners have no GPU, so CI runs the gate on the `cpu` tier; the full-tier gate runs locally or on a self-hosted GPU runner.

## 9. Metrics reported (portfolio dashboard)

| Category | Metric | Source |
|---|---|---|
| Quality | faithfulness, citation precision, routing accuracy, judge score (offline & online) | eval pipeline |
| Quality | user feedback rate & score | `feedback` |
| Cost | tokens per run (p50/p95), throughput tok/s, tokens per agent per day, optional USD-equivalent (`PRICE_PER_MTOKEN`) | `runs`, Prometheus |
| Latency | run latency p50/p95 per agent, time-to-first-token, approval turnaround | Prometheus |
| Reliability | run success rate, tool error rate, MCP server availability | Prometheus |
| Safety | % critical actions gated (must be 100%), approval decisions breakdown, expired approvals | `approvals`, eval assertion |

## 10. Build order (maps to README roadmap)

1. Migrations + auth + tool registry + audit service + `/runs` skeleton.
2. `mcp-docs` + `docs_qa` + ingest pipeline → first end-to-end use case.
3. `mcp-db` + `analyst`; `mcp-tickets` + `actions` + HITL flow.
4. Supervisor + streaming + OTel/LangSmith wiring.
5. Offline eval suite + CI gate; then online sampling + dashboards.
6. K8s manifests, load test, publish metrics in README.
