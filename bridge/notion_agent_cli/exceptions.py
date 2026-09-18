"""Single error class for the whole library + a small code taxonomy.

All recoverable failures raise :class:`NotionAgentError`. The
``.code`` attribute lets automated callers branch on the failure mode
without parsing the human message — useful for cron / pipeline wrappers
that need to distinguish "user's token expired, alert them" from
"Notion is having a 503 day, retry later".

Codes are kept as a closed set (:class:`ErrorCode`) so adding new ones
requires touching this module — that's the point of the taxonomy.
"""
from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    UNKNOWN             = "unknown"
    AUTH_INVALID        = "auth_invalid"        # 401/403 — token_v2 expired / rejected
    PREMIUM_REQUIRED    = "premium_required"    # premium-feature-unavailable event
    NOTION_ERROR        = "notion_error"        # error event in NDJSON stream
    TRUST_RULE_DENIED   = "trust_rule_denied"   # server trust-rule blocked the inference (not retryable)
    HTTP_ERROR          = "http_error"          # other non-200 from Notion
    TRANSPORT           = "transport"           # httpx network failure
    EMPTY_PROMPT        = "empty_prompt"        # local validation — caller bug
    INVALID_CALLBACK    = "invalid_callback"    # local validation — both sync+async deltas set
    EMPTY_TEXT          = "empty_text"          # Notion responded 200 but no text
    ACCOUNT_MISSING     = "account_missing"     # notion_account.json file absent
    ACCOUNT_MALFORMED   = "account_malformed"   # notion_account.json unparseable
    ACCOUNT_INVALID     = "account_invalid"     # notion_account.json missing fields
    WORKSPACE_AMBIGUOUS = "workspace_ambiguous" # caller must disambiguate
    WORKSPACE_EMPTY     = "workspace_empty"     # /loadUserContent returned no spaces
    THREAD_STATE_MISSING   = "thread_state_missing"   # --thread-id but no saved file
    THREAD_STATE_MALFORMED = "thread_state_malformed" # saved state JSON unparseable


class NotionAgentError(Exception):
    """All recoverable failures from this package raise this type.

    ``code`` is a string drawn from :class:`ErrorCode`; defaults to
    ``ErrorCode.UNKNOWN`` for legacy raise sites that haven't been
    annotated. Automated callers should branch on ``.code``, not on the
    human-readable message.

    ``subtype`` / ``retryable`` carry the raw signal from an *inline*
    Notion error section (e.g. ``subType: "trust-rule-denied"`` /
    ``isRetryable: false``). They are ``None`` for raise sites that
    don't originate from a Notion error event — callers should fall back
    to :func:`retry_policy_for` for those.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = ErrorCode.UNKNOWN,
        subtype: str | None = None,
        retryable: bool | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.subtype = subtype
        self.retryable = retryable


# --------------------------------------------------------------------------- #
# Machine-readable failure policy
#
# The taxonomy above lets a caller branch on the *kind* of failure. The two
# maps below turn that into the two things an automated caller (a wrapper
# agent shelling out to the CLI, a cron job) actually needs to decide:
#
#   1. a process EXIT CODE it can switch on without parsing stderr, and
#   2. a RETRY POLICY (is this worth retrying, and after how long).
#
# Both are intentionally small. Exit codes only diverge from the generic
# ``1`` for the two failures a caller must handle *differently* — a
# rate-limit/anti-automation denial it should back off from, and an
# auth failure it must re-credential for. Everything else stays ``1`` so
# existing ``if rc != 0`` checks keep working.
# --------------------------------------------------------------------------- #

# Borrow the conventional sysexits.h numbers so the codes read sanely in a
# shell: 75 = EX_TEMPFAIL (transient, back off), 77 = EX_NOPERM (not allowed).
EXIT_GENERIC      = 1
EXIT_TRUST_RULE   = 75
EXIT_AUTH         = 77

_EXIT_CODE_BY_CODE: dict[str, int] = {
    ErrorCode.TRUST_RULE_DENIED: EXIT_TRUST_RULE,
    ErrorCode.AUTH_INVALID:      EXIT_AUTH,
}


def exit_code_for(code: str) -> int:
    """Map an :class:`ErrorCode` to a process exit code.

    Returns ``1`` for any code without a dedicated exit code, so a plain
    ``if rc != 0`` test still catches every failure.
    """
    return _EXIT_CODE_BY_CODE.get(code, EXIT_GENERIC)


# (retryable, retry_after_seconds). ``retry_after`` is a *suggested* backoff,
# not a hard contract — Notion gives us no Retry-After header. ``None`` for
# retry_after means "don't auto-retry" (re-credential / give up instead).
_RETRY_POLICY_BY_CODE: dict[str, tuple[bool, int | None]] = {
    # Anti-automation strict mode: an immediate retry is useless (Notion
    # itself reports isRetryable=false), but the window clears — back off
    # ~5 min then retry. Observed: a 6-minute backoff recovered cleanly.
    ErrorCode.TRUST_RULE_DENIED: (False, 300),
    # Credential problems: retrying the same token is pointless.
    ErrorCode.AUTH_INVALID:      (False, None),
    ErrorCode.PREMIUM_REQUIRED:  (False, None),
    # A 200 with no text is usually a Cloudflare clearance warm-up blip —
    # a single immediate retry typically smooths it.
    ErrorCode.EMPTY_TEXT:        (True, 0),
    # Transient server / network errors: short backoff, retry once.
    ErrorCode.TRANSPORT:         (True, 30),
    ErrorCode.HTTP_ERROR:        (True, 30),
    ErrorCode.NOTION_ERROR:      (True, 30),
}


def retry_policy_for(code: str) -> tuple[bool | None, int | None]:
    """Return ``(retryable, retry_after_seconds)`` for an error code.

    ``(None, None)`` for codes with no defined policy (local caller bugs
    like ``EMPTY_PROMPT`` / ``INVALID_CALLBACK``, or ``UNKNOWN``) — there
    is no useful generic retry advice for those.
    """
    return _RETRY_POLICY_BY_CODE.get(code, (None, None))
