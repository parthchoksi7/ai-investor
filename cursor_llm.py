"""Text completions billed to Cursor, not Anthropic.

The Cursor SDK runs an agent, not a raw chat endpoint. These calls pass an empty
tool list and an empty working directory, so the model can only return text. A
news headline inside a prompt cannot edit the repo, run a shell, or place an order.

Requires CURSOR_API_KEY (Cursor Dashboard → API Keys). One process-wide cap keeps
the per-ticker fan-out from opening dozens of agents at once.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from types import SimpleNamespace

# Pinned in code, not env, so a model change is a reviewed commit and shows up
# in git history next to the scorecards it affects. Haiku-tier seats (research,
# earnings, position review, event digest, canary) use "fast"; Sonnet-tier seats
# (regime, devil's advocate, PM, CRO) use "smart". Ids must appear in
# `python cursor_llm.py --models` for the key in use.
# Same versions the Claude routines call (Haiku 4.5, Sonnet 4.6). Cursor's
# catalog ids are the short form; confirmed against `cursor_llm.py --models`
# on 2026-10-11. Newer ids (haiku-5-5, sonnet-5-5) are a different sleeve.
CURSOR_MODELS = {
    "fast": "claude-haiku-4-5",
    "smart": "claude-sonnet-4-6",
}


def tier_of(requested_model: str) -> str:
    name = (requested_model or "").lower()
    return "smart" if ("sonnet" in name or "opus" in name) else "fast"


def model_for(requested_model: str) -> str:
    return CURSOR_MODELS[tier_of(requested_model)]

_SEM = threading.Semaphore(4)


def complete(requested_model: str, system: str, user: str,
             max_tokens: int = 600) -> tuple[str, str, SimpleNamespace]:
    """Return (text, stop_reason, response). stop_reason is "end_turn" on success.

    `requested_model` is the pipeline's Anthropic id; only its tier is used. Raises
    RuntimeError with the word "overloaded" when the failure is safe to retry, so
    analysis._safe_call keeps its existing backoff.
    """
    del max_tokens  # the SDK has no output cap; the prompts already bound length
    model = model_for(requested_model)
    api_key = os.environ["CURSOR_API_KEY"].strip()
    prompt = (
        "Reply with only the JSON the system instructions ask for. "
        "No markdown fences and no preamble.\n\n"
        f"SYSTEM:\n{system}\n\nUSER:\n{user}\n"
    )
    # One empty directory per call. A shared directory would let concurrent
    # agents (the semaphore allows 4) write into the same local store, and a
    # file left by one call would be visible to the next. The directory is
    # not the repo, and tools=[] means the model has no shell or editor.
    work = tempfile.mkdtemp(prefix="cursor-llm-")
    try:
        with _SEM:
            from cursor_sdk import Agent, AgentOptions, CursorAgentError, LocalAgentOptions
            try:
                result = Agent.prompt(
                    prompt,
                    AgentOptions(
                        model=model,
                        api_key=api_key,
                        tools=[],
                        name="ai-investor-completion",
                        local=LocalAgentOptions(cwd=work),
                    ),
                )
            except CursorAgentError as e:
                # "overloaded" is the word _safe_call's backoff already looks for.
                # Non-retryable failures stay "failed" so they do not take the
                # 30s overload delay.
                kind = "overloaded" if getattr(e, "is_retryable", False) else "failed"
                raise RuntimeError(f"Cursor agent {kind}: {e}") from e

        status = getattr(result, "status", None)
        if status != "finished":
            # error/expired are worth a retry; cancelled is not an overload.
            kind = "overloaded" if status in ("error", "expired") else "failed"
            raise RuntimeError(f"Cursor agent {kind}: status={status}")

        text = (getattr(result, "result", None) or "").strip()
        usage = getattr(result, "usage", None)
        response = SimpleNamespace(
            model=model,
            usage=SimpleNamespace(
                input_tokens=getattr(usage, "input_tokens", 0) or 0,
                output_tokens=getattr(usage, "output_tokens", 0) or 0,
                cache_read_input_tokens=getattr(usage, "cache_read_tokens", 0) or 0,
                cache_creation_input_tokens=getattr(usage, "cache_write_tokens", 0) or 0,
            ) if usage is not None else None,
        )
        return text, "end_turn", response
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    import sys
    if "--models" not in sys.argv:
        sys.exit("usage: python cursor_llm.py --models")
    from cursor_sdk import Cursor
    print("pinned:", CURSOR_MODELS)
    for m in Cursor.models.list(api_key=os.environ["CURSOR_API_KEY"].strip()):
        print(getattr(m, "id", m), "|", getattr(m, "display_name", "") or getattr(m, "name", ""))
