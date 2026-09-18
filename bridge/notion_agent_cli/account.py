"""Account credentials + workspace metadata.

The credential file (default ``~/.notionagents/notion_account.json``)
holds a long-lived ``token_v2`` cookie + workspace UUIDs + an optional
Custom Agent persona binding. Schema details + bootstrap walkthrough
live in ``docs/01-notion-chat-protocol.md §4``.

We prefer ``full_cookie`` (the raw ``document.cookie`` string copied
from the browser) when present — that's the operator's escape hatch
when individual-field extraction is too fiddly. Otherwise we stitch
together the minimum set Notion's edge expects for a logged-in session.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from notion_agent_cli.exceptions import ErrorCode, NotionAgentError

_REQUIRED_FIELDS: tuple[str, ...] = (
    "token_v2",
    "user_id",
    "space_id",
)


@dataclass(slots=True, frozen=True)
class NotionAccount:
    # --- Credentials ---
    token_v2: str
    full_cookie: str = ""

    # --- Identity ---
    user_id: str = ""
    user_name: str = ""
    user_email: str = ""

    # --- Workspace ---
    space_id: str = ""
    space_name: str = ""
    space_view_id: str = ""

    # --- Browser fingerprint ---
    browser_id: str = ""
    device_id: str = ""
    # ``notion-client-version`` header. Notion ships a new web build ~daily
    # and its edge rejects versions that drift too far behind, so this is a
    # best-effort fallback only: `init` and `doctor --refresh-client-version`
    # overwrite it with the live build read from the /ai app shell (see
    # ``provider.fetch_live_client_version``). Bumped to the 2026-05-28 build
    # so an offline / fetch-failed install still starts reasonably current.
    client_version: str = "23.13.20260528.1850"
    # Default UA tracks current Chrome major; ``provider._sec_ch_ua``
    # derives the sec-ch-ua client-hints from whatever major this carries,
    # so bump them together. ``init`` overwrites this with the operator's
    # real browser UA when available.
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    )
    timezone: str = "America/Los_Angeles"

    # --- Custom agent persona (Jarvis-style binding) ---
    # Setting these makes threads surface in Notion's ✦ AI chat panel
    # under the named persona instead of the default chat.
    agent_name: str = ""
    agent_accessory: str = ""
    agent_context_page_id: str = ""
    # How the bound page relates to a Custom Agent persona. Populated
    # by `init`'s reverse-resolve against agents list:
    # - "persona_overlay"     — agent_context_page_id matches a
    #                           workflow-registered Custom Agent's
    #                           instructions page; chat panel runs main
    #                           ✦ AI runtime wearing the agent persona.
    # - "free_form_steering"  — any other Notion page used as steering
    #                           context; not a registered persona.
    # - ""                    — legacy account file (binding_mode wasn't
    #                           recorded at init time); re-run init to
    #                           populate.
    agent_binding_mode: str = ""

    # --- Default model alias ---
    default_model: str = "opus-4.8"

    # --- Forward-compat ---
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def has_jarvis_binding(self) -> bool:
        return bool(self.agent_name and self.agent_context_page_id)


def load_notion_account(path: Path | str) -> NotionAccount:
    """Read ``notion_account.json`` and return a validated NotionAccount.

    Raises :class:`NotionAgentError` on missing file, malformed JSON, or
    missing required fields so misconfiguration fails at startup rather
    than mid-stream.
    """
    p = Path(path).expanduser()
    if not p.exists():
        raise NotionAgentError(
            f"notion_account.json not found at {p}; run "
            "`notion-agent init --token-v2 <value>` to bootstrap one "
            "(pass `--token-v2 -` to read it from stdin).",
            code=ErrorCode.ACCOUNT_MISSING,
        )
    try:
        data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise NotionAgentError(
            f"notion_account.json malformed: {e}",
            code=ErrorCode.ACCOUNT_MALFORMED,
        ) from e

    missing = [f for f in _REQUIRED_FIELDS if not data.get(f)]
    if missing:
        raise NotionAgentError(
            f"notion_account.json missing required fields: {missing}. "
            "Run `notion-agent init` to regenerate.",
            code=ErrorCode.ACCOUNT_INVALID,
        )

    known = {f.name for f in NotionAccount.__dataclass_fields__.values()} - {"extras"}
    kwargs = {k: data[k] for k in known if k in data}
    extras = {k: v for k, v in data.items() if k not in known}
    return NotionAccount(**kwargs, extras=extras)


def save_notion_account(acc: NotionAccount, path: Path | str) -> None:
    """Serialize a NotionAccount to JSON. Used by the `init` subcommand."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    for f in NotionAccount.__dataclass_fields__.values():
        if f.name == "extras":
            continue
        data[f.name] = getattr(acc, f.name)
    data.update(acc.extras)  # extras round-trip
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def build_cookie_header(acc: NotionAccount) -> str:
    """Cookie header value — prefers full_cookie when set, else stitches
    together the minimum logged-in subset."""
    if acc.full_cookie:
        return acc.full_cookie

    user_id = acc.user_id
    user_id_nodash = user_id.replace("-", "")
    parts = [
        f"notion_browser_id={acc.browser_id}",
        f"device_id={acc.device_id}",
        f"notion_user_id={user_id}",
        f'notion_users=[%22{user_id}%22]',
        "notion_check_cookie_consent=false",
        "notion_locale=en-US/legacy",
        "notion_cookie_sync_completed=%7B%22completed%22%3Atrue%2C%22version%22%3A4%7D",
        f"_cioid={user_id_nodash}",
        f"token_v2={acc.token_v2}",
    ]
    return "; ".join(parts)
