# Working the DME Queue: A Single-Agent Design Writeup

## 1. Architecture and Agent Design

**Stack.** Python, Anthropic SDK's native Messages API (`tools` plus
`tool_use`/`tool_result`). No LangChain, AutoGen, etc.

**Shape.** A single-agent ReAct loop: reason, one tool call, runtime validates, HTTP,
observation, repeat. The LLM holds a working hypothesis about the blocker. The runtime
owns everything safety-critical: schema validation, HTTP execution, evidence tracking,
the simulated clock, loop guards, and terminal-state enforcement. Probabilistic
reasoning on one side of a hard line, deterministic execution on the other.

Three deliberate choices:

- **Context isolation via `case_id` injection.** `case_id` is not a tool-schema field.
  The model calls `place_call(to=...)` and the runtime injects the active case id into
  the HTTP request. "One agent, one case" is enforced structurally. The model cannot
  address the wrong patient or leak state across cases even if it tried.
- **Tool descriptions as a second system prompt.** Schemas teach consequences, not just
  endpoints. `resubmit_claim`'s description states outright that a wrong-payer claim
  can't be fixed by resubmitting (the supplier must re-bill). This heads off a
  predictable hallucination at the tool-selection layer, before it becomes an action.
- **Error-as-observation.** The world client never raises. Every HTTP or transport error
  returns `{ok: False, error: ...}` as an observation the model reads and self-corrects
  from. One malformed call degrades gracefully instead of crashing the run.

Async time is a deterministic runtime operation, not something the model steers. The
model signals "nothing actionable yet," the runtime advances to the next scheduled event
and reports what landed. This removes any path for the model to hallucinate a reply or
manipulate the clock to get an answer it wants.

## 2. Where Judgment Lives

**The model owns interpretation:** reading unstructured case files and transcripts,
comparing clinical docs against coverage policy, decoding remittance denials, forming a
blocker hypothesis, and the resolve vs escalate call itself. These are fuzzy problems,
which is what an LLM is for.

**Code owns anything that must be guaranteed:**

- **Circuit breakers as config, not prose.** `STEP_BUDGET` and `STAGNATION_CAP` are
  hardcoded constants, not prompt instructions. An LLM should never be trusted to count
  its own iterations. Stagnation triggers a forced escalation in Python, so the run
  cannot hang and API cost is strictly bounded.
- **Stateful deduplication.** The runtime tracks seen transcripts and exact fax
  payloads. A repeated action that yields no new state gets a hard observation 
  instead of a prompt warning the model can talk itself past.
- **Evidence gate plus schema validation on terminals.** `resolve_case`/`escalate_case`
  require `evidence_ids` the runtime actually returned earlier. Fabricated ids are
  rejected outright, turning "don't fake a resolution" from a request into an invariant.
  A required `rationale` argument forces the resolve vs escalate reflection inline, at
  the decision point. Both fired live on Case 07: the model first tried to escalate
  without a rationale - rejected by schema, then cited a document id it had never
  actually read - rejected by the evidence gate, and only its third attempt went through
  honestly.
- **Guaranteed termination.** On budget exhaustion the model gets one constrained turn
  (`tool_choice` forced to `escalate_case`) to package a clean handoff. If that fails,
  `force_escalate_from_history` builds the escalation deterministically from the action
  log. The queue always terminates safely.

**One deliberately soft gate.** On a claim case, if the agent did outbound work but tries
to close without ever calling `resubmit_claim` (the only action that re-adjudicates a
claim), the runtime blocks once and nudges toward it. It fires at most once per case by
design, so it can never trap a case that genuinely can't be resubmitted. This is an
honest tradeoff: a nudge, not an invariant, because the underlying judgment
is domain reasoning I chose to leave with the model.

**A general "definition of done" fix.** LLMs assume a claim isn't resolved until money
posts, so one agent routed a claim to `ACCEPTED - IN PROCESS` and then waited forever for
a `PAID` remittance the mock world's short horizon never produces. From the advocate's
side, a claim accepted by the correct payer is done. One prompt line encodes that and 
applies to any case reaching this async terminal state. It's a domain judgment, 
so it lives in the prompt, and it doesn't branch on case id.

## 3. The Cut List

- **Exploring alternatives (the safe escalation).** When the assigned supplier is
  disconnected, or a specialist's timeline is impossible, the agent escalates instead of
  hunting the directory for an alternate vendor or clinic. The fix is a constrained
  `search_providers` tool (section 4), so routing stays a deterministic query rather than
  LLM guesswork; until that exists, brute-forcing provider routing across tens of
  thousands of NPIs is a safety risk, not a convenience, so I let it escalate. This is
  also why the one escalation the evaluator counts against me is a deliberate cut.
- **Mechanical directory distrust.** The prompt has the model separate transport success
  ("the call connected") from semantic success ("I reached the right party"); the only
  mechanical backstop today is identical-transcript dedup feeding the stagnation cap. The
  fix is runtime contact-staleness tracking plus a `wrong_party` outcome enum, so a
  wrong-but-connected number is caught in code rather than one call late by the model.
- **Heavyweight infrastructure (RAG, multi-agent, a database).** All skipped by design at
  this scale: policy is fetched on demand instead of pre-indexed, a single advocate works
  the queue sequentially, and in-memory state plus the server's `X-Run-Id` gives run
  isolation and the shared clock. Each earns its place only when the corpus, concurrency,
  or durability demands it (section 4), not in a prototype.
- **Robust observation sizing.** A naive character cap truncates oversized artifacts,
  which could silently slice a directory list or split a JSON body mid-field in
  production. The fix is pagination on list tools plus cheap-LLM summarization of large
  charts.
- **Pinning determinism.** A hosted endpoint isn't fully deterministic and current Claude
  models don't expose a temperature knob, so at one branch point the agent may fax the
  corrected order and resolve, or skip it and escalate. The fix is a structured
  `missing_prerequisite` reasoning field plus a scored harness (section 4) that reports
  accuracy as a pass rate rather than trusting a single run.

**Result: 7/8 by the provided evaluator.** Six cases resolved through non-trivial work
 and one resolved after the definition of done fix above. The
eighth is the supplier disconnected case (case 04). My agent escalates, and `eval.py` scores that
as a miss because the world is resolvable by switching suppliers. I'm keeping it a miss
on purpose: the only tool for finding an alternate is an unconstrained directory, and I
stand by not letting the model brute-force provider routing without the constrained
`search_providers` tool (see section 4). 


## 4. What's Next

Ordering logic: the one-day items are reliability, observability, and handoff-quality
wins that require no changes to the world or tools. They harden what already works and
make correctness measurable. The two-week items need new tools, infra, or a larger eval
corpus, and are what let this scale from 8 cases to a production queue.

**With 1 more day:**
1. Require the agent to confirm it checked alternate directory contacts for a blocked
   role before escalating. This turns thin handoffs into complete ones, at low cost.
2. More "definition of done" prompt coverage for other async terminal states, and a
   clearer bar for when unreachability counts as exhausted.
3. Deterministic contact resolution: given a blocked party and role (a practice's fax
   line, a supplier's billing office), match the correct directory entry in code from the
   case's own fields, instead of the model dialing entries one by one or guessing which
   number to try.
4. Rolling context summary (blocker, evidence, actions tried, open questions) so long
   trajectories don't degrade reasoning quality.
5. Replace the character cap with real pagination and cheap-LLM summarization for large
   clinical charts.
6. Prompt caching, plus a harness that reruns the offline `world.yaml` comparison N times
   per case and reports terminal accuracy as a pass rate, turning the run-to-run variance
   above from an anecdote into a measured number.

**With 2 weeks:**
1. A deterministic `search_providers(specialty, zip, radius)` tool to replace the flat
   directory. This moves routing from LLM guesswork to a constrained backend query,
   closing the supplier-interchangeability gap the right way instead of with a prompt
   hack.
2. A structured `{hypothesis, missing_prerequisite, tool_call}` reasoning schema before
   each action, for better determinism and easier failure debugging. This also directly
   targets the missed-prerequisite variance noted above.
3. One or two worked few-shot trajectories (hypothesis, act, wait, pivot) to cut empty
   time-advances and tool hallucination on complex multi-party flows.
4. Retrieval that injects only the policy relevant to a case's HCPCS code and payer. A
   monolithic prompt won't scale across equipment types and payer rules.
5. Durable checkpointing and explicit human-in-the-loop interruption (e.g. LangGraph),
   not for the framework itself but for cases that genuinely need a pause-and-review step
   mid-run.
6. Eval-driven development on two fronts:
   - **Offline:** Promptfoo or Braintrust over a large historical corpus so every prompt,
     tool, or policy change is regression-tested for terminal accuracy and evidence
     validity rather than spot-checked.
   - **Online:** LLM-as-judge scoring of live escalation packages for handoff quality
     (completeness, correct recommendation), which `world.yaml` can't grade, plus drift
     alerts on terminal-state distribution in production.
