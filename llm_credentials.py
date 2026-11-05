"""Which credential the model calls use — ONE definition, shared by
analysis._get_client() (the agents) and preflight_gate._check_api_health() (the
canary), so the canary can never test a different credential than the agents use.

Standard library only: preflight_gate imports this before `pip install`.

Why two key names (2026-11-05). Since 2026-11-04 the cloud session token is refused
for direct model calls (403 permission_error), so the routine needs an API key.
The cloud environment's settings reserve the name ANTHROPIC_API_KEY ("won't be used
to authenticate requests") and do not pass it into sessions. The routine's key is
therefore stored as AI_INVESTOR_ANTHROPIC_API_KEY. ANTHROPIC_API_KEY still wins when
present (local .env, GitHub Actions).
"""

from __future__ import annotations

import os

API_KEY_ENV_NAMES = ("ANTHROPIC_API_KEY", "AI_INVESTOR_ANTHROPIC_API_KEY")
SESSION_TOKEN_ENV = "CLAUDE_SESSION_INGRESS_TOKEN_FILE"
DEFAULT_BASE_URL = "https://api.anthropic.com"


def model_credential() -> tuple[str | None, str | None, str | None]:
    """(kind, secret, source) with kind ∈ {"api_key", "session_token", None}.

    Any non-empty API key wins, passed through unmodified. Otherwise the session
    token file (refused for model calls since 2026-11-04, kept so the failure is a
    clear 403 rather than a missing-credential error). Otherwise (None, None, None).
    A token file that exists but cannot be read raises OSError — the caller decides.
    """
    for name in API_KEY_ENV_NAMES:
        val = os.getenv(name)
        if val:
            return "api_key", val, name
    token_file = os.getenv(SESSION_TOKEN_ENV, "")
    if token_file and os.path.isfile(token_file):
        with open(token_file) as f:
            return "session_token", f.read().strip(), "session token"
    return None, None, None


def base_url() -> str:
    """The API root the agents' SDK client will use (the SDK honours
    ANTHROPIC_BASE_URL; the canary must match it)."""
    return (os.getenv("ANTHROPIC_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
