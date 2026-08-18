# Take-Home: Working the Queue

## The problem, in our words

Mira Mace puts a care advocate in a Medicare patient's corner. The advocate's day is a
queue: a claim came back denied, a prior auth has been sitting for three weeks, a supplier
promised delivery and went quiet, a patient stopped replying. Each one is a small
investigation followed by a few phone calls, and none of them look alike. An advocate opens
a case, works out what's actually wrong, and does something about it.

Almost none of that is hard. It's just slow, and it doesn't scale — every case is a person
holding a phone.

We want a system that works the queue itself, coming back to a human only when it genuinely
has to. **That's what you're building a prototype of.**

## The setup

Concretely: you're building an **agent for DME coordination** — the equipment half of
that queue. This repo is the world to build it against: a mock server that simulates
everything an advocate touches — the phone lines, the fax machines, the patients, the
payers, the clock — as plain HTTP. Eight open cases are loaded into it.

Your agent gets the same inputs and outputs a human advocate has, nothing more: it can
read the case file, call, fax, text, check a claim, and watch the inbox; the server
decides what the world does back, and when. Your job is the loop that turns those
endpoints into worked cases.

## The domain, in five minutes

You don't need healthcare experience for this exercise — everything case-specific is
discoverable inside the world. But the vocabulary helps, so here it is.

The cases are mostly about **DME** — durable medical equipment: wheelchairs, walkers,
hospital beds. Getting a Medicare patient a piece of equipment involves four kinds of
party, and the case files use all of them:

| Party | Who they are | What they do |
| --- | --- | --- |
| **Patient** | The person we advocate for | Answers texts, sometimes. Has the insurance card. |
| **Practice** | A doctor's office | Writes the **written order** (the prescription for the equipment) and holds the chart: visit notes, evaluations. |
| **Supplier** | A DME company | Dispenses and delivers the equipment, and **bills the payer** for it. Can only bill exactly what the order says. |
| **Payer** | Medicare, or a private Medicare Advantage plan (Humana, Aetna, …) | Pays claims, decides **prior authorizations**. Which payer covers a patient can change — a claim has to go to whoever covered them **on the date of service**. |

And the paper that moves between them:

- **Written order** — signed by the treating practitioner. The supplier dispenses and
  bills against it, exactly as written.
- **Clinical documentation** — face-to-face (F2F) encounter notes, PT evaluations. The
  medical record that justifies the order. Coverage policy says what the record has to
  establish for each equipment code; when it doesn't, claims deny and authorizations sit.
- **HCPCS code** — the five-character code for a piece of equipment (K0001, E0135, …).
  The policy documents in this repo list the ones that matter here.
- **Claim / remittance** — the supplier bills the payer; the payer answers with a
  remittance: terse, coded lines (CO-16, N706, …) that say what happened and never quite
  say why. Denied claims can be **resubmitted** — but a resubmission that doesn't fix
  the underlying problem earns the same denial.
- **Prior authorization (PA)** — for some equipment the payer must approve *before*
  dispatch, and it decides on the strength of the clinical documentation.
- **Referral** — a primary-care doctor's authorization for a specialist visit. It names
  a specific specialist practice, and it expires.
- **Eligibility** — which plan covers a patient, effective when. It can change.

That's all the domain you need. The rest — what's actually wrong on each case — is in
the artifacts.

## Getting started

```bash
docker compose up
```

The world is then at `http://localhost:8000`, with interactive API docs at
`http://localhost:8000/docs` and the OpenAPI schema at `/openapi.json`. It's plain HTTP —
build your agent in whatever language you like.

Without Docker (Python 3.12+):

```bash
pip install -r env/requirements.txt
uvicorn env.app.main:app --reload
```

Start with `GET /cases`.

## The world

There is no integration layer. This is what our advocates actually have, and it's what your
agent has:

| Channel | What it is |
| --- | --- |
| **Phone** — `POST /calls` | The only way to reach a supplier, a doctor's office, or a payer's provider line. Someone picks up, or doesn't. You give it a number or a name and get back an outcome and a transcript. You are not building voice — assume the call happens. |
| **Fax** — `POST /faxes` | How a doctor's office receives a document request. Sometimes something comes back. Sometimes nobody works that queue. |
| **Text** — `POST /texts` | The patient. Replies come when they come. |
| **Claims** — `GET /claims/{id}`, `POST /claims/{id}/resubmit` | Remittance text, coded. Terse, and it does not explain itself. |
| **Policy** — `GET /policy` | Published coverage documentation. Long, and you have to actually read it to know what a claim needed. |
| **Directory** — `GET /directory` | A spreadsheet of suppliers and provider offices that ops keeps by hand. Numbers were right when someone typed them in. |
| **The case** — `GET /cases/{id}`, `GET /cases/{id}/documents/{ref}` | The case file and everything already attached to it: transcripts, remittance text, fax logs, message threads, chart notes. |

**Read the artifacts, not the summary.** Every case file opens with where things stand
according to whoever last touched it. That isn't always right, and on several of these the
thing actually blocking the case is sitting in a transcript or a remittance code that nobody
read carefully.

The cases are not variations on a theme. What's wrong differs, what would fix it differs,
and two cases that look like the same problem don't have the same answer.

**How you turn these endpoints into tools is entirely your call.** They're deliberately raw
— no convenience endpoint hands you a case pre-digested. What your agent can do, at what
granularity, and what it sees when something fails is part of what you're designing.

## Time

Nothing async happens instantly. A fax that gets answered, a text that gets a reply, a
resubmitted claim — each lands on a specific day, and some never land at all.

- `GET /clock` — the current day and what's in flight
- `POST /clock/advance` — move the clock; anything now due gets delivered
- `GET /inbox` — everything that has landed

The clock starts at day 0. Days in the case files are relative to it.

## Runs

Every endpoint takes an optional `X-Run-Id` header. Runs are fully isolated, so you can work
on several cases in parallel or start clean whenever you like — use a new id, or
`POST /runs/{id}/reset`. Omit the header and you share a run called `default`.

A tip that will save you real time: iterate on a single case with a fresh run id. A full
eight-case run with a capable model takes on the order of twenty minutes of wall clock,
and it tests nothing that a two-minute single-case run doesn't.

`GET /run/report` gives you the state of all eight cases plus the full action log, which is
what we'd like you to send us.

## How a case ends

Every case terminates in exactly one of two states, and getting this right matters more than
how many you resolve:

- **`POST /cases/{id}/resolve`** — the blocker is gone and you can say what unblocked it,
  with the evidence.
- **`POST /cases/{id}/escalate`** — this needs a human, here's why, and here's everything
  they need so they don't start over. A good package is a real deliverable: what you found,
  what you tried, what's left, what you'd recommend.

**Some of these cases cannot be resolved by any agent.** Escalating those is the correct
answer and we score it as a success. An agent that reports resolution on a case it did not
resolve is the worst outcome in this exercise — worse than one that gives up early.

## What we're asking for

A prototype that takes the queue and works it.

You will not get all eight in the time. Get as far as you can, and be straight with us about
where you landed.

### Requirements

Two, and they're not negotiable:

- **It works.** The agent runs against the world and does real work — reads the artifacts,
  decides something, acts, and reaches an ending. Not a pipeline over stubs.
- **The agent knows what it actually did.** Every case it touches ends `resolved` or
  `escalated`, and it's right about which. Where it's uncertain, it says so rather than
  guessing.

One ground rule: **one agent, all eight cases.** Don't branch on case id.

Everything else is your call.

## Constraints

- **Around 3 hours.** The scope is bigger than the time. Prioritize, and tell us what you
  cut.
- Skip auth, persistence, UI polish. In-memory or a JSON file is fine.
- Any stack, any model, any framework. Use what you'd actually reach for in production.
- Bring your own model key. A full run of this exercise costs on the order of a dollar
  or two on any mainstream provider. If that's a barrier, tell us and we'll sort it out.
- Use AI freely while building. We don't care who typed the code. We care a great deal about
  where the models sit inside the running system — and where you deliberately kept them out.
- Don't modify anything under `data/` or `env/`. If you think something in there is broken,
  tell us — you might be right.

## Deliverable

1. **The code.** A repo or zip we can read, with a short README: how to run it.
2. **Your results.** The output of `GET /run/report` from your final run — one run, all
   eight cases sharing one clock — or the same thing in your own words. We'd rather see
   the real run than a tidied table.
3. **A 1–2 page writeup:**
   - **Architecture and agent design.** What you reached for and why — stack, framework, how
     the agent is put together, what it can do and how you decided.
   - **Where judgment lives.** Which decisions you gave the model and which you kept in
     code, and why.
   - **The cut list.** What you deliberately didn't do, and why.
   - **What's next.** With 1 more day, what do you build? With 2 weeks? Why that order?

## What's in here

```
data/cases/       the eight case files
data/policy/      published coverage documentation
data/directory.csv
data/world.yaml   what every channel returns, and when
env/              the service
```

Yes, `data/world.yaml` is readable. Read it if you want — using it to check whether your
agent ended each case the right way is fair game, and it's how you'll know when to
iterate. But your agent has to find its own way through: don't hand-wire the paths you
found there. We read the action log, and the line is obvious in it.

## A note on the data

Every patient, provider, supplier, claim, and phone number here is invented. Nothing in this
repo is real patient data.
