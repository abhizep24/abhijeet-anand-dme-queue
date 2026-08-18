import os
from typing import Tuple

from anthropic import AsyncAnthropic


def get_llm_client() -> Tuple[AsyncAnthropic, str]:
    """Return an async Anthropic client and the model name to use.

    Reads CLAUDE_API_KEY and CLAUDE_MODEL_NAME from the environment (.env).
    """
    api_key = os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("No API key found. Set CLAUDE_API_KEY in your .env.")

    client = AsyncAnthropic(api_key=api_key)
    model_name = os.getenv("CLAUDE_MODEL_NAME", "claude-sonnet-5")
    return client, model_name
