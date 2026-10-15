"""Text completions billed to Cursor, not Anthropic.

The Cursor SDK runs an agent, not a raw chat endpoint. These calls pass an empty
tool list and a private working directory, so the model can only return text. A
news headline inside a prompt cannot edit the repo, run a shell, or place an order.

Requires CURSOR_API_KEY (Cursor Dashboard → API Keys). One process-wide cap keeps
the per-ticker fan-out from opening dozens of agents at once.

System text is a project rule, not a block inside the user message. Python
cursor-sdk 1.0.37 has no system-prompt field: the bridge proto does not carry
the TypeScript client's `systemPrompt` (that option is also account-gated and
fails closed with `unknown option '--system-prompt'`). Writing the role as a
`SYSTEM:` section in the user message makes Haiku 4.5 refuse the turn. The
local agent does load `.cursor/rules/*.mdc` when `setting_sources` includes
`project`, and `alwaysApply: true` injects that file as instructions.
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

# Appended to the caller's system text inside the project rule. The caller's
# user payload is sent unchanged, so this stays out of the user message.
_JSON_ONLY = (
    "Reply with one JSON object and no other text. "
    "Do not add a preamble or markdown code fences."
)


def tier_of(requested_model: str) -> str:
    name = (requested_model or "").lower()
    return "smart" if ("sonnet" in name or "opus" in name) else "fast"


def model_for(requested_model: str) -> str:
    return CURSOR_MODELS[tier_of(requested_model)]

_SEM = threading.Semaphore(4)


def _unwrap_json_fence(text: str) -> str:
    """Drop one outer markdown fence when the body is JSON.

    Haiku 4.5 follows the project rule and then still wraps the object in
    ```json, matching the Cursor harness's habit of fencing code. The live
    parser in analysis._parse_json strips that fence; do the same here so
    the returned text is the JSON object.
    """
    raw = text.strip()
    if not raw.startswith("```"):
        return raw
    lines = raw.splitlines()
    if len(lines) < 2 or lines[-1].strip() != "```":
        return raw
    inner = "\n".join(lines[1:-1]).strip()
    if inner.startswith("{") or inner.startswith("["):
        return inner
    return raw


def _write_project_rule(work: str, system: str) -> None:
    """Write the system text as an always-applied project rule."""
    rules = os.path.join(work, ".cursor", "rules")
    os.makedirs(rules, exist_ok=True)
    body = (system or "").strip()
    if body:
        body = f"{body}\n\n{_JSON_ONLY}\n"
    else:
        body = f"{_JSON_ONLY}\n"
    path = os.path.join(rules, "role.mdc")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("---\nalwaysApply: true\n---\n\n")
        fh.write(body)


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
    # One empty directory per call. A shared directory would let concurrent
    # agents (the semaphore allows 4) write into the same local store, and a
    # file left by one call would be visible to the next. The directory is
    # not the repo, and tools=[] means the model has no shell or editor.
    # The rule file is the only instruction source. The user message is the
    # caller's payload, with no SYSTEM: wrapper.
    work = tempfile.mkdtemp(prefix="cursor-llm-")
    try:
        _write_project_rule(work, system)
        with _SEM:
            from cursor_sdk import Agent, AgentOptions, CursorAgentError, LocalAgentOptions
            try:
                result = Agent.prompt(
                    user,
                    AgentOptions(
                        model=model,
                        api_key=api_key,
                        tools=[],
                        name="ai-investor-completion",
                        local=LocalAgentOptions(
                            cwd=work,
                            setting_sources=["project"],
                        ),
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

        text = _unwrap_json_fence(getattr(result, "result", None) or "")
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
