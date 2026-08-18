"""Runtime constants - These are policy knobs the deterministic runtime enforces;
none are case specific and none encode an expected outcome."""

from __future__ import annotations

import os

# Where the mock world lives.
WORLD_BASE_URL = os.getenv("WORLD_BASE_URL", "http://localhost:8000")

# Hard upper bound on tool calls per case. This alone guarantees termination because the runtime will force an escalation if the agent reaches the step budget.
STEP_BUDGET = int(os.getenv("AGENT_STEP_BUDGET", "45"))


# Consecutive no-progress actions (blocked repeats, empty inbox polls, dead-number
# retries) before we give up and force an escalation. A softer companion to the
# step budget that catches stuck loops earlier.
STAGNATION_CAP = int(os.getenv("AGENT_STAGNATION_CAP", "8"))

# Bounds on a single runtime decided time advance.
MIN_ADVANCE = 1
MAX_ADVANCE = 20

# How many consecutive empty time-advances count as legitimate waiting (when the agent
# has an outbound async action still plausibly in flight) before the runtime concludes
# the reply may never come and nudges the agent to act differently or end the case.
EMPTY_ADVANCE_TOLERANCE = int(os.getenv("AGENT_EMPTY_ADVANCE_TOLERANCE", "3"))

# Max tokens the model may emit per turn (brief reasoning + one tool call, or a
# full escalation package). Required by the Anthropic Messages API.
MAX_TOKENS = int(os.getenv("AGENT_MAX_TOKENS", "2048"))
