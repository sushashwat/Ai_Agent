# AI Coding Agent

A small, general-purpose AI coding agent that explores an existing codebase, plans a
feature implementation for an open-ended product request, writes the code, and
summarizes what it did — with minimal human guidance.

Built for the assignment: point it at `callicoder/node-easy-notes-app` with the
request *"Improve the application so users can better organise and search their
notes"* and it decides on and implements a reasonable feature on its own. It is not
hardcoded to that repo or that request — it is a generic four-phase agent driven by
an LLM (Google Gemini, via the `google-genai` SDK) and a small, safety-scoped toolset.

## Quick start

```bash
git clone <this-repo-url> ai-coding-agent
cd ai-coding-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # then edit .env and set GEMINI_API_KEY

# Clone the target application the agent should modify
git clone https://github.com/callicoder/node-easy-notes-app.git ../target_repo

python main.py \
  --repo ../target_repo \
  --request "Improve the application so users can better organise and search their notes."
```

The agent prints its exploration, plan, tool calls, and final summary to the
console as it works, and writes two files into the target repo:

- `PLAN.md` — the exploration report and the JSON implementation plan.
- `CHANGES.md` — the final human-readable summary of what changed, generated from
  the actual `git diff`.

To verify the app still runs after the change:

```bash
cd ../target_repo
npm install
# make sure MongoDB is running locally (mongodb://localhost:27017/easy-notes)
node server.js
```

## Architecture

```
ai-coding-agent/
├── main.py                 # CLI entry point
├── agent/
│   ├── core.py              # CodingAgent: orchestrates the 4 phases
│   ├── llm_client.py         # Wrapper around the Gemini API (google-genai SDK)
│   │                          #   + a generic "tool loop" runner with rate-limit retry
│   ├── tools.py              # Sandboxed filesystem/shell tools + JSON schemas
│   └── prompts.py            # System prompts, one per phase
├── requirements.txt
└── .env.example
```

**Why this shape:** the interesting part of a coding agent is not any one clever
prompt, it's the *loop* — give the model tools, let it decide what to call and in
what order, and only intervene at phase boundaries. `llm_client.py`'s
`run_tool_loop()` is deliberately generic (system prompt + tool list + toolbox in,
final text out) so the same function drives both the read-only exploration phase and
the read/write implementation phase. `tools.py` and `prompts.py` are the only
genuinely repo/task-specific pieces, and neither hardcodes anything about notes
apps — they're reusable for any repo and any request.

## Agent workflow (4 phases)

The CLI request maps directly onto the assignment's expectations:

| # | Phase | Tools available | Output |
|---|-------|------------------|--------|
| 1 | **Explore** | `list_directory`, `read_file`, `search_code` (read-only) | Markdown report: stack, architecture, data model, relevant files, conventions |
| 2 | **Plan** | none (single completion over the exploration report) | Structured JSON plan: chosen approach + rationale + per-file steps + explicitly out-of-scope items |
| 3 | **Implement** | all read tools + `write_file`, `edit_file`, `run_command` (whitelisted) | Code changes on disk, self-verified with `node -c` / `git diff --stat` |
| 4 | **Summarize** | none (single completion over the real `git diff`) | PR-style Markdown summary grounded in the actual diff, not the plan |

Each phase is a **separate LLM call/loop with a narrower toolset**, rather than one
giant open-ended agent loop:

- The model can't start editing files before it has actually looked at them
  (`write_file`/`edit_file` are disabled until phase 3).
- The plan is a natural checkpoint — in a real product this is where a human could
  approve/edit the JSON plan before implementation runs; here it's logged to
  `PLAN.md` for transparency and reproducibility.
- The final summary is generated from the **real diff**, not from the agent's own
  memory of what it meant to do, so it can't hallucinate changes that didn't happen.

### The tool loop

`run_tool_loop()` implements a ReAct-style loop against the Gemini API:

1. Send the system prompt + running chat history + the tool declarations.
2. If the model's response contains function calls, execute each one against the
   sandboxed `ToolBox`, feed the results back as function responses, and go back to 1.
3. Stop when the model responds with text and no further function calls (or a hard
   `max_turns` cap is hit, to bound cost/runaway loops).

It also includes **rate-limit retry handling**: on the free tier, Gemini enforces
tight per-minute request quotas, so `_send_with_retry()` catches `429
RESOURCE_EXHAUSTED` errors, parses the server-suggested `retryDelay`, sleeps, and
retries (up to 5 attempts) instead of crashing mid-run.

### Repository exploration

The explore phase is intentionally *not* a fixed script (e.g. "always read
`package.json` then `server.js`") — the model decides what to look at, the same way
a human engineer would: usually `list_directory('.')` first for the lay of the land,
then `read_file` on whatever looks like the entry point and data models, then
`search_code` for cross-cutting patterns (e.g. route registrations, symbol usages).
This is what lets the same agent generalize to a different repo or a follow-up
request without any code changes — the exploration strategy lives in the prompt,
not in Python.

### Safety guardrails

- **Path sandboxing**: every tool resolves paths relative to `repo_root` and
  refuses to touch anything outside it.
- **Phased write access**: `write_file`/`edit_file` are disabled during exploration
  (`allow_write=False`); the model literally cannot mutate the repo while exploring.
- **Command allowlist**: `run_command` only accepts an explicit list of safe,
  useful commands (`npm install`, `node -c <file>`, `git diff`, etc.) — it is not a
  general shell.
- **Precise edits over blind rewrites**: `edit_file` is a `str_replace`-style tool
  that requires the *exact*, *unique* existing text, so the model can't silently
  clobber unrelated parts of a file the way a full `write_file` overwrite could.

## What the agent actually built for this request

Running the agent against `node-easy-notes-app` (a plain Express + Mongoose REST
API — no frontend, no auth, five endpoints: `POST/GET /notes`,
`GET/PUT/DELETE /notes/:noteId`), the exploration phase identifies it as a thin
MVC-ish `routes -> controller -> model` structure. Given no further spec, the agent
converged on:

- **Tags** on notes: added a `tags: [String]` field to `Note`
  (`app/models/note.model.js`), and updated `create`/`update` in the controller to
  accept and persist an optional `tags` array.
- **Search & filter** on the existing `GET /notes` endpoint via query parameters —
  `?search=<keyword>` (case-insensitive match against `title`/`content`) and
  `?tag=<tag>` (filter by tag) — so **no new endpoint is required** and every
  existing client call to `GET /notes` keeps working exactly as before.

This satisfies "organise" (tags) and "search" (query + tag filter) while keeping
every existing route, request shape, and response shape unchanged. The
implementation phase self-verified with `node -c` and `git diff --stat` before
declaring done. See `PLAN.md`/`CHANGES.md` in the target repo (generated per run)
for the exact plan and diff from any given execution.

## Assumptions & trade-offs

- **No human-in-the-loop gate by default.** The assignment asks for "minimal user
  guidance," so phase 2's plan is logged but not blocked on approval. A production
  version would add a `--dry-run` flag that stops after `PLAN.md` for human
  review before phase 3 runs.
- **No automated test suite in the target repo**, so "preserve existing
  functionality" is verified via (a) syntax-checking every touched file with
  `node -c`, (b) the plan constraint to only make additive changes, and (c) manual
  verification (`npm install && node server.js`, hitting the old endpoints via
  Postman).
- **Local environment note:** the target repo pins an old Mongoose version
  (`^5.2.8`) that uses the legacy MongoDB wire protocol. Modern local MongoDB
  installs (v6+) no longer support it, so a local Mongoose bump was needed purely
  to *run* the app for manual verification — this is an environment compatibility
  step, unrelated to the agent's implementation, and is not something the agent
  itself needed to change to satisfy the request.
- **Free-tier rate limits**: Gemini's free tier enforces a low requests-per-minute
  quota, so the tool loop includes retry-with-backoff on `429` errors rather than
  failing the run.
- **`run_command` allowlist is deliberately conservative** (no arbitrary shell) —
  this trades some agent flexibility for making the tool safe to hand to an LLM
  unsupervised.
- **Model choice**: defaults to a Gemini Flash-tier model (overridable via
  `GEMINI_MODEL`), chosen for its generous free tier and solid tool-use support.
- **JSON plan parsing** is best-effort (strips code fences, falls back to
  regex-extracting the first `{...}` block) rather than strict structured-output
  mode, to keep the dependency footprint small.

## Generalizing to a new request

Nothing in `agent/` references notes, tags, or Express specifically — `tools.py`
and `llm_client.py` are domain-agnostic, and `prompts.py` only encodes *how to
explore/plan/implement/summarize*, not *what* to build. Pointing the same CLI at
the same repo with a different `--request` (or at a different repo entirely) drives
the same four phases with a fresh exploration and a fresh plan tailored to that
request.