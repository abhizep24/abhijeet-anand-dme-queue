# DME Care Advocate Agent — how to run

A single-agent ReAct loop that works the DME queue against the mock world. One
agent implementation handles all eight cases with no branching on `case_id`.

Full design rationale is in the **writeup** (`Write Up.md`). This file is just how to run it.

## 1. Start the world

```bash
docker compose up -d --build      # world at http://localhost:8000
curl -s localhost:8000/health     # {"ok":true,"cases":8}
```

## 2. Configure a model key

The agent runs on the Anthropic Messages API. Put your key in a local `.env`
(git-ignored) — see `.env.example`:

```
CLAUDE_API_KEY=sk-ant-...
CLAUDE_MODEL_NAME=claude-sonnet-5
```

`llm.py` reads `CLAUDE_API_KEY` (falling back to `ANTHROPIC_API_KEY`) and
`CLAUDE_MODEL_NAME` (default `claude-sonnet-5`). Nothing else in the code changes
between environments.

## 3. Install deps and run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # agent deps (world deps are in env/)

# One case on an isolated run (fast, ~cents) — best for iterating:
python -m agent.run --case case-01 --run-id try-01 --reset

# All eight cases sharing ONE clock (the deliverable run):
python -m agent.run --run-id final --reset
```

`--reset` starts the run's clock fresh. Every run is isolated by `X-Run-Id`, so
you can work cases in parallel or start clean anytime.

## 4. Get the results

```bash
curl -s -H 'X-Run-Id: final' localhost:8000/run/report | python -m json.tool
```

## 5. Offline scoring (optional, never used by the agent)

`eval.py` compares the final report against the expected terminal state per case
(derived by reading `data/world.yaml`, which the agent never sees at runtime):

```bash
python eval.py --run-id final
```

## Layout

```
agent/
  run.py           CLI entrypoint (all cases or one; no case_id branching)
  agent.py         the ReAct loop for a single case
  runtime.py       deterministic runtime: validation, evidence tracking,
                   directory distrust, clock control, loop safety, terminal gate
  tools.py         Pydantic-validated tool surface + Anthropic tool schemas
  world_client.py  the only code that talks HTTP to the world
  prompts.py       system prompt (all domain judgment lives here)
  config.py        step/stagnation/clock knobs (no case-specific values)
eval.py            offline evaluator (read-only)
llm.py             Anthropic Messages API client (key + model from .env)
```

## Where judgment lives

The **LLM** owns domain reasoning: reading artifacts, interpreting remittances and
transcripts, comparing clinical docs to policy, picking the next action, and
judging resolve-vs-escalate. **Deterministic Python** owns everything
safety-critical: schema validation, HTTP, evidence tracking, the resolve/escalate
evidence gate (you can only cite artifacts you actually fetched), directory
distrust (a line that doesn't reach anyone is blocked from blind retry),
runtime-controlled time advancement, and guaranteed termination (step budget +
stagnation cap + forced-escalation fallback).
