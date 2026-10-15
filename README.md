# Code Review Agent

A conversational, context-aware code review agent built on [LangChain Deep Agents](https://github.com/langchain-ai/deepagents).

It reviews a pull request the way a senior teammate would. It reads the diff, retrieves related code and the team's conventions, checks the ticket, and reads the repo itself through read-only tools. It hands focused passes to a security specialist and a pattern-compliance specialist. Then it **talks with you** about what it found: why something matters, what the fix looks like, and whether your new commit resolves it.

![Overview](docs/overview.png)

## What's inside

| Path | What it does |
|---|---|
| `review_agent/session.py` | `ReviewSession`, the production agent: retrieval, read-only repo tools, specialist subagents, checkpointed memory, typed findings through a `submit_review` tool |
| `review_agent/context_engine.py` | AST chunking, embeddings (Gemini, or an offline fallback) and a ChromaDB retriever |
| `review_agent/reviewer.py` | Single-pass reviewers and the security + pattern ensemble, used by the benchmark |
| `review_agent/tools.py` | Read-only git tools with validated refs, timeouts and size caps; the sandboxed filesystem denies writes and secret files |
| `review_agent/ui.py`, `ui.html` | Web UI: PR picker, diff, findings, chat with the reviewer, and an overview with benchmark results |
| `review_agent/service.py` | HTTP API: `POST /sessions`, `POST /sessions/{id}/messages`, with a repo allowlist, session TTL and size limits |
| `review_agent/sample_prs.py`, `toy_repository.py` | A small FastAPI service with team conventions, and 15 mock PRs with 2 planted issues each |
| `review_agent/evaluation.py` | Deterministic scoring (precision, recall, F1) and a parallel eval runner |
| `review_agent/recordings.py` | Records real review sessions for offline demo replay, and runs the benchmark |
| `recordings/` | 15 recorded Gemini reviews and the benchmark results |
| `code_review_agent.ipynb` | Walkthrough: diff-only vs context-aware, the context engine, specialist agents, evaluation, the conversational agent |
| `code_review_agent_standalone.ipynb` | The same walkthrough, self-contained: it writes the package itself |
| `tests/` | Guardrail and API tests that make no model calls |

## Quick start

```bash
git clone https://github.com/praveenjune17/code-review-agent.git
cd code-review-agent
cp .env.example .env        # then add GOOGLE_API_KEY (Gemini)
uv sync
uv run uvicorn review_agent.ui:app --port 8765
```

Open http://localhost:8765. The 15 recorded reviews open instantly, and any question you type in the chat is answered live.

Other entry points:

```bash
uv run pytest                                        # tests, no API calls
uv run jupyter lab code_review_agent.ipynb           # the walkthrough notebook
uv run python -m review_agent.recordings             # re-record demo reviews (real model calls)
uv run python -m review_agent.recordings --benchmark # diff-only vs context-aware benchmark
```

Using pip instead: `pip install -r requirements.txt`.

## Benchmark

30 real reviews with `gemini-3.1-flash-lite` across the 15 mock PRs, scored against the 2 planted issues in each:

| Approach | Recall | Precision | F1 |
|---|---|---|---|
| Diff only | 70% | 78% | 74% |
| Context-aware (retrieved code + conventions + ticket) | **87%** | **82%** | **85%** |

The issues caught only with context were convention violations, such as a missing `get_current_user` dependency, a secret not loaded from settings, and validation limits defined elsewhere in the codebase.

## Configuration

All settings come from environment variables or `.env`. See `.env.example`.

| Variable | Default | Purpose |
|---|---|---|
| `GOOGLE_API_KEY` | | Gemini for the LLM and the embeddings (preferred) |
| `GOOGLE_CLOUD_PROJECT` | | Gemini through Vertex AI with gcloud credentials, instead of an API key |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | | Fallback LLM providers |
| `REVIEW_MODEL` | `google_genai:gemini-3.1-flash-lite` | Any `init_chat_model` string |
| `REVIEW_MODE` | `auto` | UI: `auto` (recorded reviews + live chat), `live`, or `replay` (no model calls) |
| `REVIEW_EMBEDDINGS` | `gemini` with a Google key, else `local` | `gemini`, `local` (offline, no download) or `chroma` |
| `REVIEW_ALLOWED_ROOTS` | current folder | Repos the HTTP API may review |
| `LANGSMITH_*` | | Optional tracing; every run is tagged `code-review` |

## Safety

The agent can only read. Writes to the repo are denied, `.env` files, keys and `.git/` internals can't be read, git refs and paths are validated, and every git call has a timeout and an output cap. The HTTP API only reviews repos under `REVIEW_ALLOWED_ROOTS`.
