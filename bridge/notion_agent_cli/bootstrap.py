"""Workspace metadata bootstrap.

Library function used by the ``notion-agent init`` CLI subcommand.
Takes a ``token_v2`` cookie (the only thing the operator can copy from
a browser in <30s) plus optional fingerprint UUIDs, then calls
``/api/v3/loadUserContent`` to enumerate workspaces + extract user
identity. Returns a populated :class:`NotionAccount` ready to save.

``user_id`` is optional — Notion's /loadUserContent accepts the
request with ``token_v2`` alone and the response includes the user
record, so callers can pass just ``token_v2`` and let bootstrap
derive identity. Pass ``user_id`` explicitly only when you already
know it (e.g. parsed from a full ``document.cookie`` string).

If the user has multiple workspaces and the caller didn't pre-select
one, :func:`bootstrap_account` raises :class:`AmbiguousWorkspaceError`
carrying the available choices so the CLI can prompt.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from notion_agent_cli.account import NotionAccount
from notion_agent_cli.exceptions import ErrorCode, NotionAgentError

BASE_URL = "https://app.notion.com/api/v3"
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


@dataclass(slots=True, frozen=True)
class Workspace:
    space_id: str
    space_view_id: str
    space_name: str
    domain: str


@dataclass(slots=True, frozen=True)
class UserInfo:
    user_id: str
    user_name: str
    user_email: str


class AmbiguousWorkspaceError(NotionAgentError):
    """Raised when bootstrap finds >1 workspace and caller didn't pick one."""

    def __init__(self, workspaces: list[Workspace]):
        super().__init__(
            f"{len(workspaces)} workspaces available — caller must specify which: "
            + ", ".join(f"{w.space_name!r}" for w in workspaces),
            code=ErrorCode.WORKSPACE_AMBIGUOUS,
        )
        self.workspaces = workspaces


def parse_browser_cookie(cookie: str) -> dict[str, str]:
    """Parse a ``document.cookie`` string into ``{name: value}`` pairs.

    Values are kept verbatim — ``token_v2`` in particular is left in its
    URL-encoded form (``v03%3A...``), which is what Notion's edge accepts
    in subsequent requests. Whitespace around names and values is trimmed.
    Entries without ``=`` are dropped silently.
    """
    out: dict[str, str] = {}
    for part in cookie.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        out[name.strip()] = value.strip()
    return out


def _build_cookie(token_v2: str, browser_id: str, user_id: str | None) -> str:
    """Cookie header for /loadUserContent. The identity-bearing entries
    (``notion_user_id`` / ``notion_users``) are omitted when the caller
    hasn't supplied a user_id — Notion's edge authenticates on token_v2
    (JWT) alone and reports the user_id back in the response, so we send
    a minimal cookie on bootstrap and fill it in afterwards.
    """
    parts = [f"notion_browser_id={browser_id}"]
    if user_id:
        parts.append(f"notion_user_id={user_id}")
        parts.append(f'notion_users=[%22{user_id}%22]')
    parts.extend([
        "notion_check_cookie_consent=false",
        "notion_locale=en-US/legacy",
        f"token_v2={token_v2}",
    ])
    return "; ".join(parts)


def _bootstrap_headers(
    token_v2: str, browser_id: str, user_id: str | None,
) -> dict[str, str]:
    headers = {
        "accept":                    "application/json",
        "accept-language":           "en-US,en;q=0.9",
        "content-type":              "application/json",
        "notion-audit-log-platform": "web",
        "notion-client-version":     "23.13.20260528.1850",
        "origin":                    "https://app.notion.com",
        "referer":                   "https://app.notion.com/",
        "user-agent":                DEFAULT_UA,
        "sec-ch-ua":                 '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
        "sec-ch-ua-mobile":          "?0",
        "sec-ch-ua-platform":        '"macOS"',
        "sec-fetch-dest":            "empty",
        "sec-fetch-mode":            "cors",
        "sec-fetch-site":            "same-origin",
        "cookie":                    _build_cookie(token_v2, browser_id, user_id),
    }
    if user_id:
        headers["x-notion-active-user-header"] = user_id
    return headers


def _walk_record(record: dict[str, Any]) -> dict[str, Any]:
    """Unwrap Notion's ``{'role': '...', 'value': {...}}`` records."""
    if not isinstance(record, dict):
        return {}
    val = record.get("value")
    if isinstance(val, dict):
        inner = val.get("value")
        if isinstance(inner, dict):
            return inner
        return val
    return record


def _extract_workspaces(load_data: dict[str, Any]) -> list[Workspace]:
    rm = load_data.get("recordMap") or {}
    spaces_raw = rm.get("space") or {}
    space_views_raw = rm.get("space_view") or {}

    sv_by_space: dict[str, str] = {}
    for sv_id, sv_rec in space_views_raw.items():
        sv = _walk_record(sv_rec)
        sp = sv.get("space_id")
        if isinstance(sp, str) and sp not in sv_by_space:
            sv_by_space[sp] = sv_id

    out: list[Workspace] = []
    for space_id, space_rec in spaces_raw.items():
        sp = _walk_record(space_rec)
        out.append(Workspace(
            space_id=space_id,
            space_view_id=sv_by_space.get(space_id, ""),
            space_name=sp.get("name") or "",
            domain=sp.get("domain") or "",
        ))
    return out


def _extract_user(load_data: dict[str, Any], user_id: str) -> UserInfo:
    rm = load_data.get("recordMap") or {}
    users_raw = rm.get("notion_user") or {}
    rec = users_raw.get(user_id)
    if not rec:
        return UserInfo(user_id=user_id, user_name="", user_email="")
    u = _walk_record(rec)
    name_parts = [u.get("given_name") or "", u.get("family_name") or ""]
    name = " ".join(p for p in name_parts if p).strip() or u.get("name") or ""
    return UserInfo(user_id=user_id, user_name=name, user_email=u.get("email") or "")


def _derive_user_id_from_recordmap(load_data: dict[str, Any]) -> str | None:
    """Pick the authenticated user_id from /loadUserContent's response.

    Notion always returns the calling user's record under
    ``recordMap.notion_user`` — there's exactly one entry for the token
    holder. Returns ``None`` only when the response has no user record at
    all, which is the signature of an invalid / expired token_v2.
    """
    rm = load_data.get("recordMap") or {}
    users_raw = rm.get("notion_user") or {}
    for uid in users_raw:
        return uid
    return None


async def fetch_user_content(
    *, token_v2: str, browser_id: str, user_id: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Call /api/v3/loadUserContent and return the parsed response.

    ``user_id`` is optional. When omitted, the request omits
    ``x-notion-active-user-header`` and the identity-bearing cookie
    entries — Notion authenticates on token_v2 alone and the response
    includes the user record so callers can derive user_id afterwards.
    """
    headers = _bootstrap_headers(token_v2, browser_id, user_id)
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=30.0)
    try:
        resp = await client.post(f"{BASE_URL}/loadUserContent", json={}, headers=headers)
        if resp.status_code != 200:
            code = (
                ErrorCode.AUTH_INVALID
                if resp.status_code in (401, 403)
                else ErrorCode.HTTP_ERROR
            )
            raise NotionAgentError(
                f"loadUserContent failed: HTTP {resp.status_code} body={resp.text[:500]!r}",
                code=code,
            )
        return resp.json()
    finally:
        if owns_client:
            await client.aclose()


def _ensure_uuid(value: str | None) -> str:
    return value or str(uuid.uuid4())


async def bootstrap_account(
    *,
    token_v2: str,
    user_id: str | None = None,
    browser_id: str | None = None,
    space_name: str | None = None,
    space_domain: str | None = None,
    agent_name: str = "",
    agent_accessory: str = "",
    agent_context_page_id: str = "",
    default_model: str = "opus-4.8",
    timezone: str = "America/Los_Angeles",
    http_client: httpx.AsyncClient | None = None,
) -> NotionAccount:
    """Probe ``/api/v3/loadUserContent`` and return a populated NotionAccount.

    Minimum input is ``token_v2``. ``user_id`` is auto-derived from the
    response when not supplied. Raises :class:`AmbiguousWorkspaceError`
    when multiple workspaces are available and ``space_name`` /
    ``space_domain`` didn't disambiguate, and ``NotionAgentError`` with
    code ``AUTH_INVALID`` when /loadUserContent returns no user record
    (signature of an expired / rejected token_v2).
    """
    browser_id = _ensure_uuid(browser_id)
    data = await fetch_user_content(
        token_v2=token_v2, user_id=user_id, browser_id=browser_id,
        http_client=http_client,
    )
    if not user_id:
        user_id = _derive_user_id_from_recordmap(data)
        if not user_id:
            raise NotionAgentError(
                "loadUserContent returned no user record — token_v2 may be "
                "invalid or expired.",
                code=ErrorCode.AUTH_INVALID,
            )
    workspaces = _extract_workspaces(data)
    if not workspaces:
        raise NotionAgentError(
            "loadUserContent returned no workspaces — token invalid?",
            code=ErrorCode.WORKSPACE_EMPTY,
        )

    if len(workspaces) == 1:
        chosen = workspaces[0]
    elif space_domain:
        match = next((w for w in workspaces if w.domain == space_domain), None)
        if match is None:
            raise AmbiguousWorkspaceError(workspaces)
        chosen = match
    elif space_name:
        match = next(
            (w for w in workspaces if w.space_name.lower() == space_name.lower()),
            None,
        )
        if match is None:
            raise AmbiguousWorkspaceError(workspaces)
        chosen = match
    else:
        raise AmbiguousWorkspaceError(workspaces)

    user = _extract_user(data, user_id)

    return NotionAccount(
        token_v2=token_v2,
        user_id=user_id,
        user_name=user.user_name,
        user_email=user.user_email,
        space_id=chosen.space_id,
        space_view_id=chosen.space_view_id,
        space_name=chosen.space_name,
        browser_id=browser_id,
        device_id=_ensure_uuid(None),
        timezone=timezone,
        agent_name=agent_name,
        agent_accessory=agent_accessory,
        agent_context_page_id=agent_context_page_id,
        default_model=default_model,
    )
