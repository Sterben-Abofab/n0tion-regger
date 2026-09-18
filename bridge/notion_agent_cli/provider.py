"""Async client for one round-trip to ``/api/v3/runInferenceTranscript``.

Library entry point — :class:`NotionAgentClient`. Loads the credential
file, builds the chat-panel-equivalent payload, POSTs it, streams the
NDJSON response, returns a :class:`~notion_agent_cli.types.ChatResponse`.

Scope (MVP):
- new thread per call by default; continuation via ``thread_id=`` +
  per-thread state under ``~/.notionagents/threads/``
- ``asPatchResponse=true`` (precise chat-panel mirror)
- Jarvis-style custom-agent binding via account file (recommended)
- text + token usage + thinking (thinking captured but not surfaced)
- optional ``on_text_delta`` callback for streaming consumers

Out of scope (see docs/ROADMAP.md):
- per-day / per-agent rolling thread strategy (rotating threads
  automatically; current API forces the caller to track thread ids)
- file attachments / image generation
- token_v2 auto-refresh (it's a long-lived JWT; on 401 we raise and
  the operator re-bootstraps the account file)
"""
from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from curl_cffi.requests import AsyncSession as CurlAsyncSession

from notion_agent_cli.account import (
    NotionAccount,
    build_cookie_header,
    load_notion_account,
)
from notion_agent_cli.exceptions import ErrorCode, NotionAgentError
from notion_agent_cli.models import load_user_model_map, resolve_alias, resolve_model
from notion_agent_cli.ndjson import NDJSONStreamParser
from notion_agent_cli.thread_state import (
    ThreadState,
    load_thread_state,
    save_thread_state,
)
from notion_agent_cli.transcript import (
    _now_iso,
    build_full_transcript,
    build_inference_request,
    build_partial_transcript,
    new_uuid,
)
from notion_agent_cli.types import ChatResponse, TokenUsage

log = logging.getLogger(__name__)

# Notion migrated the chat panel from www.notion.so → app.notion.com
# (the /ai SPA 302s through /api/v3/sessionSync?...&__dm_a=1). The live
# browser now posts every /api/v3 call to app.notion.com with an
# ``origin: https://app.notion.com``; matching that host + origin keeps
# the request indistinguishable from the real client. www.notion.so still
# answers the API today, but app.notion.com is where the trusted browser
# session lives.
DEFAULT_BASE_URL = "https://app.notion.com/api/v3"
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)

# ``runInferenceTranscript`` is gated by a server-side trust rule that, as of
# 2026-06-15, fingerprints the TLS/JA3 handshake: a pure-httpx (Python/OpenSSL)
# client is denied with ``trust-rule-denied`` no matter how faithful its
# headers/cookies/body are, while a request carrying a real Chrome TLS
# fingerprint passes with the *same* credentials. curl_cffi replays Chrome's
# JA3 via curl-impersonate, so the inference path posts through it instead of
# httpx. Proven by ablation (see memory/project_notion_trust_rule.md). The
# ancillary JSON endpoints + the /ai warm-up are NOT TLS-gated and stay on
# httpx (see ``_get_client`` / ``_post_json``).
_IMPERSONATE = "chrome"
# (connect, read) seconds — read is generous so a long streamed inference turn
# isn't cut off mid-stream (mirrors httpx's read=300 above).
_INFERENCE_TIMEOUT = (10, 300)

# Host root (no /api/v3) for fetching the app-shell HTML that carries the
# live build version. Notion's chat panel lives at ``/ai`` on this host.
DEFAULT_WEB_BASE_URL = "https://app.notion.com"

# Pull the Chrome major out of the stored user-agent so the client-hints
# stay self-consistent with it. A real Chrome sends
# ``sec-ch-ua: "Chromium";v="<major>", "Google Chrome";v="<major>",
# "Not/A)Brand";v="99"``; a mismatched/forged sec-ch-ua is a classic bot
# tell, so we derive it instead of hardcoding a version that rots.
_CHROME_MAJOR_RE = re.compile(r"Chrome/(\d+)")

_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


def _sec_ch_ua(user_agent: str) -> str:
    m = _CHROME_MAJOR_RE.search(user_agent or "")
    major = m.group(1) if m else "148"
    return (
        f'"Chromium";v="{major}", "Google Chrome";v="{major}", '
        f'"Not/A)Brand";v="99"'
    )

# Notion stamps the current web build into the /ai app-shell HTML in two
# spots — the root element's ``data-notion-version`` attribute and a JS
# config object's ``version:"..."``. Either is the value the chat panel
# sends as ``notion-client-version``. Format: ``23.13.YYYYMMDD.HHMM``.
_CLIENT_VERSION_RE = re.compile(r'data-notion-version="(\d+\.\d+\.\d{8}\.\d+)"')
_CLIENT_VERSION_FALLBACK_RE = re.compile(r'version:"(\d+\.\d+\.\d{8}\.\d+)"')


@dataclass(slots=True)
class _PreparedCall:
    """Internal — output of :meth:`NotionAgentClient._prepare_call`.

    Carries the URL/body/headers ready to POST, plus a ``save_state``
    closure that persists thread continuation state on success.
    """
    url: str
    body: dict[str, Any]
    headers: dict[str, str]
    active_thread_id: str
    notion_model: str
    save_state: Callable[[], None]


def build_headers(
    acc: NotionAccount,
    *,
    accept: str = "application/x-ndjson",
) -> dict[str, str]:
    """Headers mirroring captured chat-panel traffic.

    Notion-specific headers (``x-notion-active-user-header`` /
    ``x-notion-space-id`` / ``notion-client-version``) are the ones the
    server actually validates; ``sec-ch-ua`` / ``sec-fetch-*`` are
    fingerprint hygiene to stay indistinguishable from a real browser.

    ``accept`` defaults to ``application/x-ndjson`` (the inference
    endpoint) — pass ``application/json`` for ancillary endpoints like
    ``/getAvailableModels`` that return a single JSON document.

    ``origin`` / ``referer`` / ``sec-ch-ua`` mirror the live app.notion.com
    client exactly (see the 2026-05-29 capture): a stale origin or a
    forged client-hints string is what flips Notion's
    ``checkRunInferenceTranscriptRuleSet`` into denying the call. Note we
    deliberately do NOT set ``accept-encoding`` — httpx advertises only
    the codecs it can actually decode, so letting it manage that header
    avoids asking for (e.g.) zstd we can't decompress.
    """
    return {
        "accept":                      accept,
        "accept-language":             "en-US,en;q=0.9",
        "content-type":                "application/json",
        "notion-audit-log-platform":   "web",
        "notion-client-version":       acc.client_version,
        "origin":                      "https://app.notion.com",
        "referer":                     f"https://app.notion.com/ai?assetsVersion={acc.client_version}",
        "user-agent":                  acc.user_agent,
        "x-notion-active-user-header": acc.user_id,
        "x-notion-space-id":           acc.space_id,
        "sec-ch-ua":          _sec_ch_ua(acc.user_agent),
        "sec-ch-ua-mobile":   "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest":     "empty",
        "sec-fetch-mode":     "cors",
        "sec-fetch-site":     "same-origin",
        "priority":           "u=1, i",
        "dnt":                "1",
        "cookie":             build_cookie_header(acc),
    }


async def fetch_live_client_version(
    *,
    web_base_url: str = DEFAULT_WEB_BASE_URL,
    user_agent: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> str:
    """Return Notion's current web build version from the ``/ai`` app shell.

    Notion ships a new web build roughly daily and stamps the build id
    (``23.13.YYYYMMDD.HHMM``) into the chat-panel HTML. That id is the
    value the browser sends as the ``notion-client-version`` header, and
    Notion's edge rejects requests whose version is too far behind the
    live build. Pinning it at ``init`` time therefore goes stale within
    days — the recurring "CLI suddenly 400s" failure mode.

    This fetch needs no auth (the shell HTML is public), so it's a cheap
    way for ``doctor --refresh-client-version`` to keep the account file
    current instead of the operator bumping it by hand after every Notion
    release.

    Raises :class:`NotionAgentError` if the page can't be fetched or the
    version string isn't present (Notion changed the shell markup).
    """
    url = f"{web_base_url.rstrip('/')}/ai"
    headers = {
        "accept":     "text/html,application/xhtml+xml",
        "user-agent": user_agent or _DEFAULT_UA,
    }
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(
        timeout=httpx.Timeout(20.0), follow_redirects=True,
    )
    try:
        resp = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        raise NotionAgentError(
            f"could not fetch {url} to read live client_version: {e}",
            code=ErrorCode.TRANSPORT,
        ) from e
    finally:
        if owns_client:
            await client.aclose()

    if resp.status_code != 200:
        raise NotionAgentError(
            f"{url} returned HTTP {resp.status_code} while reading live "
            f"client_version",
            code=ErrorCode.HTTP_ERROR,
        )
    html = resp.text
    m = _CLIENT_VERSION_RE.search(html) or _CLIENT_VERSION_FALLBACK_RE.search(html)
    if m is None:
        raise NotionAgentError(
            f"no notion build version found in {url} app shell — Notion may "
            f"have changed the page markup; update the regex in provider.py",
            code=ErrorCode.HTTP_ERROR,
        )
    return m.group(1)


async def fetch_session_warmup(
    *,
    web_base_url: str = DEFAULT_WEB_BASE_URL,
    user_agent: str | None = None,
    cookie: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[str | None, dict[str, str]]:
    """GET the ``/ai`` shell as a browser does and report what came back.

    Returns ``(live_client_version_or_None, cookies_set_by_response)``.

    The point is the cookies: Cloudflare mints a fresh ``__cf_bm`` /
    ``_cfuvid`` on this navigation when the request looks browser-faithful
    (HTTP/2 + a real Chrome ``sec-ch-ua`` + the session cookies) and the
    caller doesn't already carry a live ``__cf_bm`` — verified 2026-05-29:
    a GET with the session cookies but no ``__cf_bm`` came back 200 with
    ``set-cookie: __cf_bm=…`` / ``_cfuvid=…``. That lets the CLI refresh
    its own Cloudflare clearance the same way the browser does, instead of
    the operator re-pasting a cookie every ~30 min. ``__cf_bm`` is what
    Notion's trust rule checks once it tightens, so keeping it current is
    what makes the inference path stay unblocked long-term.

    Best-effort and non-raising on a non-200 (returns ``(None, {})``) —
    callers treat warmup as opportunistic, never load-bearing.
    """
    url = f"{web_base_url.rstrip('/')}/ai"
    ua = user_agent or _DEFAULT_UA
    headers = {
        "accept":             "text/html,application/xhtml+xml",
        "accept-language":    "en-US,en;q=0.9",
        "user-agent":         ua,
        "sec-ch-ua":          _sec_ch_ua(ua),
        "sec-ch-ua-mobile":   "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest":     "document",
        "sec-fetch-mode":     "navigate",
        "sec-fetch-site":     "none",
        "priority":           "u=0, i",
    }
    if cookie:
        headers["cookie"] = cookie
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(
        timeout=httpx.Timeout(20.0), follow_redirects=True, http2=True,
    )
    try:
        resp = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        raise NotionAgentError(
            f"could not warm up session against {url}: {e}",
            code=ErrorCode.TRANSPORT,
        ) from e
    finally:
        if owns_client:
            await client.aclose()

    version: str | None = None
    if resp.status_code == 200:
        m = _CLIENT_VERSION_RE.search(resp.text) or _CLIENT_VERSION_FALLBACK_RE.search(resp.text)
        version = m.group(1) if m else None
    return version, dict(resp.cookies)


class NotionAgentClient:
    """Direct caller for Notion's ✦ AI chat inference endpoint."""

    def __init__(
        self,
        account_path: Path | str | None,
        *,
        base_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        inference_session: CurlAsyncSession | None = None,
        as_patch_response: bool = True,
        generate_title: bool = True,
        model_map_path: Path | str | None = None,
        thread_state_dir: Path | str | None = None,
        account: NotionAccount | None = None,
    ):
        if account_path is None and account is None:
            raise ValueError("NotionAgentClient: pass account_path or account=")
        self.account_path = Path(account_path).expanduser() if account_path else None
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.as_patch_response = as_patch_response
        self.generate_title = generate_title
        # None → resolve_model loads the default `~/.notionagents/models.json`
        # lazily; explicit Path lets tests inject a fixture file.
        self.model_map_path = (
            Path(model_map_path).expanduser() if model_map_path else None
        )
        # None → thread_state defaults to `~/.notionagents/threads/`; tests
        # point at tmp_path to avoid touching the user's home directory.
        self.thread_state_dir = (
            Path(thread_state_dir).expanduser() if thread_state_dir else None
        )

        # Pre-populated account (`account=`) lets callers like `init`
        # query workspace endpoints with a freshly-bootstrapped credential
        # *before* it lands on disk — no temp file dance required.
        self._account: NotionAccount | None = account
        self._client: httpx.AsyncClient | None = http_client
        # Tests inject a client → we shouldn't close it on aclose().
        self._owns_client = http_client is None
        # Separate curl_cffi session for the TLS-gated inference POST (see
        # _IMPERSONATE). Injectable for tests; only closed if we created it.
        self._inference_session: CurlAsyncSession | None = inference_session
        self._owns_inference_session = inference_session is None
        self._user_map_cache: dict[str, str] | None = None

    # ----------------------------- account ---------------------------- #

    def load_account(self) -> NotionAccount:
        if self._account is None:
            if self.account_path is None:
                raise NotionAgentError(
                    "no account file path and no in-memory account supplied",
                    code=ErrorCode.ACCOUNT_MISSING,
                )
            self._account = load_notion_account(self.account_path)
        return self._account

    def _get_user_model_map(self) -> dict[str, str]:
        if self._user_map_cache is None:
            self._user_map_cache = load_user_model_map(self.model_map_path)
        return self._user_map_cache

    # ----------------------------- http ------------------------------- #

    def _get_client(self) -> httpx.AsyncClient:
        """httpx client for the NON-inference endpoints.

        The ancillary JSON endpoints (``_post_json``) are not TLS-gated, so
        they keep using httpx. ``http2=True`` still matches the browser's wire
        protocol. The TLS-gated inference POST goes through curl_cffi instead —
        see :meth:`_inference_stream`.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, http2=True)
        return self._client

    def _get_inference_session(self) -> CurlAsyncSession:
        """curl_cffi session for the TLS-gated ``runInferenceTranscript`` POST."""
        if self._inference_session is None:
            self._inference_session = CurlAsyncSession()
        return self._inference_session

    @contextlib.asynccontextmanager
    async def _inference_stream(
        self, url: str, body: dict[str, Any], headers: dict[str, str],
    ) -> AsyncIterator[Any]:
        """Open the inference POST as a streamed, Chrome-TLS-impersonated response.

        Single seam shared by :meth:`complete` / :meth:`stream_lines` /
        :meth:`start_run_detached`. Always uses curl_cffi — Chrome TLS
        impersonation is the only thing that passes Notion's trust rule (see
        ``_IMPERSONATE``). Yields a curl_cffi response exposing ``.status_code``
        and ``.aiter_lines()``; transport failures become
        :class:`NotionAgentError`. Tests monkeypatch this method to feed canned
        NDJSON without a network round trip.
        """
        session = self._get_inference_session()
        try:
            async with session.stream(
                "POST", url, json=body, headers=headers,
                impersonate=_IMPERSONATE, timeout=_INFERENCE_TIMEOUT,
            ) as resp:
                yield resp
        except NotionAgentError:
            raise
        except Exception as e:  # curl_cffi raises CurlError / RequestsError
            raise NotionAgentError(
                f"notion transport error: {e}", code=ErrorCode.TRANSPORT,
            ) from e

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._owns_inference_session and self._inference_session is not None:
            await self._inference_session.close()
            self._inference_session = None

    async def __aenter__(self) -> NotionAgentClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    # ----------------------------- prep ------------------------------- #

    def _prepare_call(
        self,
        *,
        prompt: str,
        system: str | None,
        model: str | None,
        web_search: bool,
        workspace_search: bool,
        ask_mode: bool,
        thread_id: str | None,
        workflow_id: str | None = None,
    ) -> _PreparedCall:
        """Build URL/body/headers + a state-save closure.

        Shared by :meth:`complete` and :meth:`stream_lines` so the
        request-building logic doesn't drift between them. The
        ``save_state`` closure persists the thread state for the next
        ``--thread-id`` continuation — call it only after the HTTP call
        completes successfully.

        ``workflow_id`` (R5-A) routes the run through Custom Agent
        workflow mode: ``threadParentPointer.table = "workflow"``,
        ``context.surface = "custom_agent"``, etc. Used by
        :meth:`start_run_detached`. On continuation the workflow_id
        is restored from the saved thread state, so passing it
        explicitly on a ``thread_id`` continuation isn't required.
        """
        acc = self.load_account()

        joined = f"{system}\n\n{prompt}" if system else prompt
        if not joined.strip():
            raise NotionAgentError(
                "empty prompt (system and user are both blank)",
                code=ErrorCode.EMPTY_PROMPT,
            )

        if thread_id is not None:
            prior_state = load_thread_state(thread_id, self.thread_state_dir)
            notion_model = prior_state.notion_model
            effective_workflow_id = workflow_id or prior_state.workflow_id or None
            new_updated_config_ids = [*prior_state.updated_config_ids, new_uuid()]
            transcript = build_partial_transcript(
                acc,
                new_user_text=joined,
                notion_model=notion_model,
                config_id=prior_state.config_id,
                context_id=prior_state.context_id,
                updated_config_ids=new_updated_config_ids,
                use_web_search=web_search,
                use_workspace_search=workspace_search,
                use_read_only_mode=ask_mode,
                original_datetime=prior_state.original_datetime,
                workflow_id=effective_workflow_id,
            )
            active_thread_id = thread_id
            create_thread = False
            is_partial = True
            req_generate_title = False

            def save_state() -> None:
                prior_state.updated_config_ids = new_updated_config_ids
                prior_state.last_activity_iso = _now_iso(acc.timezone)
                save_thread_state(prior_state, self.thread_state_dir)
        else:
            notion_model = resolve_model(
                model or acc.default_model,
                user_map=self._get_user_model_map(),
            )
            effective_workflow_id = workflow_id
            first_turn_config_id = new_uuid()
            first_turn_context_id = new_uuid()
            first_turn_datetime = _now_iso(acc.timezone)
            transcript = build_full_transcript(
                acc,
                user_text=joined,
                notion_model=notion_model,
                use_web_search=web_search,
                use_workspace_search=workspace_search,
                use_read_only_mode=ask_mode,
                config_id=first_turn_config_id,
                context_id=first_turn_context_id,
                now=first_turn_datetime,
                workflow_id=effective_workflow_id,
            )
            active_thread_id = new_uuid()
            create_thread = True
            is_partial = False
            req_generate_title = self.generate_title

            def save_state() -> None:
                save_thread_state(
                    ThreadState(
                        thread_id=active_thread_id,
                        config_id=first_turn_config_id,
                        context_id=first_turn_context_id,
                        original_datetime=first_turn_datetime,
                        notion_model=notion_model,
                        updated_config_ids=[],
                        last_activity_iso=_now_iso(acc.timezone),
                        workflow_id=effective_workflow_id or "",
                    ),
                    self.thread_state_dir,
                )

        body = build_inference_request(
            acc,
            transcript=transcript,
            thread_id=active_thread_id,
            create_thread=create_thread,
            is_partial_transcript=is_partial,
            as_patch_response=self.as_patch_response,
            generate_title=req_generate_title,
            workflow_id=effective_workflow_id,
        )
        headers = build_headers(acc)
        url = f"{self.base_url}/runInferenceTranscript"

        log.debug(
            "POST %s thread=%s model=%s (alias=%s) jarvis=%s partial=%s",
            url, active_thread_id, notion_model,
            resolve_alias(notion_model, user_map=self._get_user_model_map()) or "?",
            acc.has_jarvis_binding, is_partial,
        )

        return _PreparedCall(
            url=url,
            body=body,
            headers=headers,
            active_thread_id=active_thread_id,
            notion_model=notion_model,
            save_state=save_state,
        )

    async def _raise_for_http(self, resp: Any) -> None:
        """Translate non-200 inference responses to NotionAgentError.

        ``resp`` is a curl_cffi streamed response (the inference path's only
        client); its body is read with ``atext()``.
        """
        text = await resp.atext()
        snippet = text[:500] if text else ""
        if resp.status_code in (401, 403):
            raise NotionAgentError(
                f"notion auth failed ({resp.status_code}): token_v2 expired "
                f"or rejected. Re-run `notion-agent init` with a fresh "
                f"cookie. body={snippet!r}",
                code=ErrorCode.AUTH_INVALID,
            )
        raise NotionAgentError(
            f"notion API {resp.status_code}: {snippet!r}",
            code=ErrorCode.HTTP_ERROR,
        )

    # ----------------------------- complete --------------------------- #

    async def complete(
        self,
        *,
        prompt: str,
        system: str | None = None,
        model: str | None = None,
        web_search: bool = True,
        workspace_search: bool = True,
        ask_mode: bool = False,
        on_text_delta: Callable[[str], None] | None = None,
        on_text_delta_async: Callable[[str], Awaitable[None]] | None = None,
        thread_id: str | None = None,
        workflow_id: str | None = None,
    ) -> ChatResponse:
        """Send one user message, return the assistant reply.

        Parameters
        ----------
        prompt
            The user-side text. If ``system`` is also set, both are
            stitched together (Notion has no first-class system role —
            agent instructions live on the bound context_page_id).
        model
            Friendly alias (``opus-4.8``), Anthropic id
            (``claude-opus-4-8``), or Notion internal id
            (``ambrosia-tart-high``). Defaults to ``account.default_model``.
            Ignored when ``thread_id`` is set — continuation locks the
            model to whatever turn-1 chose.
        web_search / workspace_search
            Toggles the corresponding config flags.
        ask_mode
            Sets ``useReadOnlyMode=True`` — the model answers but skips
            page edits.
        on_text_delta
            Optional sync callback fired with each new text chunk as it
            streams in. Use this to print to stdout in real time.
        on_text_delta_async
            Optional async variant — coroutine awaited per chunk. Use
            this from async consumers (FastAPI SSE, etc.) where ``await``
            is needed to push the chunk through (e.g. ``await
            queue.put(chunk)``). Set at most one of
            ``on_text_delta`` / ``on_text_delta_async``.
        thread_id
            Continue a prior thread (the ``thread_id`` from a previous
            response). The CLI loads
            ``~/.notionagents/threads/<thread_id>.json`` for the
            config_id / context_id / original datetime needed to make
            Notion accept the partial transcript. Missing state →
            :class:`NotionAgentError` with code
            ``THREAD_STATE_MISSING``.
        """
        if on_text_delta is not None and on_text_delta_async is not None:
            raise NotionAgentError(
                "set on_text_delta OR on_text_delta_async, not both",
                code=ErrorCode.INVALID_CALLBACK,
            )

        prep = self._prepare_call(
            prompt=prompt,
            system=system,
            model=model,
            web_search=web_search,
            workspace_search=workspace_search,
            ask_mode=ask_mode,
            thread_id=thread_id,
            workflow_id=workflow_id,
        )

        parser = NDJSONStreamParser()
        last_text_len = 0
        async with self._inference_stream(prep.url, prep.body, prep.headers) as resp:
            if resp.status_code != 200:
                await self._raise_for_http(resp)

            async for line in resp.aiter_lines():
                parser.feed_line(line)
                if len(parser.text) > last_text_len:
                    chunk = parser.text[last_text_len:]
                    last_text_len = len(parser.text)
                    if on_text_delta_async is not None:
                        await on_text_delta_async(chunk)
                    elif on_text_delta is not None:
                        on_text_delta(chunk)

        result = parser.finalize()
        if not result.text:
            # Inline ``error`` sections (e.g. trust-rule-denied) are raised
            # by the NDJSON parser before we get here, so reaching this
            # point means a genuinely empty stream with no error marker —
            # the model produced no text (rare) or the thread is in a bad
            # state.
            raise NotionAgentError(
                f"notion returned empty text (events={result.event_type_counts}, "
                f"lines={result.line_count}); no error event was present — the "
                f"model produced no text or the thread is in a bad state",
                code=ErrorCode.EMPTY_TEXT,
            )

        # Persist state so the next `chat --thread-id <id>` finds what
        # it needs. Cheap (small JSON file) even when the operator never
        # continues — and the alternative ("save only when asked") would
        # need the caller to predict re-use before sending.
        prep.save_state()

        return ChatResponse(
            text=result.text,
            model=result.notion_model or prep.notion_model,
            thread_id=prep.active_thread_id,
            thinking=result.thinking,
            usage=TokenUsage(
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cache_read=result.cache_read_tokens,
                cache_creation=result.cache_creation_tokens,
            ),
            raw={
                "notion_model":      result.notion_model or prep.notion_model,
                "event_type_counts": result.event_type_counts,
                "line_count":        result.line_count,
            },
        )

    # ----------------------------- stream_lines ----------------------- #

    async def stream_lines(
        self,
        *,
        prompt: str,
        system: str | None = None,
        model: str | None = None,
        web_search: bool = True,
        workspace_search: bool = True,
        ask_mode: bool = False,
        thread_id: str | None = None,
        workflow_id: str | None = None,
    ) -> AsyncIterator[str]:
        """Stream raw NDJSON lines from the inference endpoint.

        Like :meth:`complete` but without parsing. Each yielded value
        is one non-empty NDJSON line (no trailing newline). Use this
        for programmatic consumers that want the full event stream
        (CLI ``chat --ndjson``, the FastAPI ``/chat`` SSE branch, etc.).

        Thread state is persisted as in :meth:`complete` after the
        stream is fully consumed without error, so continuation via
        ``--thread-id`` keeps working for raw consumers too.

        Raises :class:`NotionAgentError` on HTTP / transport failures.
        Terminal events embedded in the stream (``error`` /
        ``premium-feature-unavailable``) are NOT translated — the raw
        consumer is responsible for inspecting them.
        """
        prep = self._prepare_call(
            prompt=prompt,
            system=system,
            model=model,
            web_search=web_search,
            workspace_search=workspace_search,
            ask_mode=ask_mode,
            thread_id=thread_id,
            workflow_id=workflow_id,
        )
        async with self._inference_stream(prep.url, prep.body, prep.headers) as resp:
            if resp.status_code != 200:
                await self._raise_for_http(resp)
            async for line in resp.aiter_lines():
                if line.strip():
                    yield line
        prep.save_state()

    # ----------------------------- ancillary -------------------------- #

    async def _post_json(self, path: str, body: dict[str, object]) -> dict[str, object]:
        """Shared helper for non-streaming JSON endpoints.

        Reuses the same auth headers and error-code mapping as the chat
        path so ancillary calls (``/getAvailableModels`` /
        ``/getCustomAgents``) speak the same dialect of failures.
        """
        acc = self.load_account()
        url = f"{self.base_url}/{path}"
        headers = build_headers(acc, accept="application/json")
        client = self._get_client()
        try:
            resp = await client.post(url, json=body, headers=headers)
        except httpx.HTTPError as e:
            raise NotionAgentError(
                f"notion transport error: {e}",
                code=ErrorCode.TRANSPORT,
            ) from e
        if resp.status_code != 200:
            snippet = resp.text[:400] if resp.text else ""
            if resp.status_code in (401, 403):
                raise NotionAgentError(
                    f"notion auth failed ({resp.status_code}): token_v2 "
                    f"expired or rejected. body={snippet!r}",
                    code=ErrorCode.AUTH_INVALID,
                )
            raise NotionAgentError(
                f"{path} failed: HTTP {resp.status_code} body={snippet!r}",
                code=ErrorCode.HTTP_ERROR,
            )
        return resp.json()

    async def fetch_available_models(self) -> dict[str, object]:
        """Call ``/api/v3/getAvailableModels`` and return the parsed JSON.

        Used by ``notion-agent models refresh`` to refresh the local
        ``~/.notionagents/models.json`` map without a code release.
        Raises :class:`NotionAgentError` on non-200.
        """
        return await self._post_json(
            "getAvailableModels",
            {"spaceId": self.load_account().space_id},
        )

    async def fetch_custom_agents(self) -> dict[str, object]:
        """Call ``/api/v3/getCustomAgents`` and return the parsed JSON.

        Used by ``notion-agent agents list`` / ``notion-agent threads list``
        — the response carries ``agentIds`` + ``mostRecentTranscripts`` +
        ``activityScores`` for the bound workspace. Raises
        :class:`NotionAgentError` on non-200.
        """
        return await self._post_json(
            "getCustomAgents",
            {"spaceId": self.load_account().space_id},
        )

    async def fetch_workflow_records(
        self, workflow_ids: list[str],
    ) -> dict[str, object]:
        """Batch-fetch Notion ``workflow`` records by id via ``syncRecordValuesMain``.

        ``agents list`` returns workflow ids (Notion stores Custom
        Agents as workflow records — verified by a live capture of
        ``getInferenceTranscriptsForWorkflow`` returning
        ``recordMap.workflow.<workflowId>`` with the agent's
        metadata). Each pointer is ``{table: "workflow", id, spaceId}``;
        Notion's workflow records are space-scoped so the spaceId is
        required. Returns the raw response — use
        :func:`notion_agent_cli.agents.parse_workflow_records` to
        flatten ``recordMap.workflow`` into ``{workflow_id: value}``.

        The bot table is **not** the right query: agent_ids returned
        by getCustomAgents are not bot record ids; querying them on
        ``table=bot`` returns only an ACL probe ``{role: "editor"}``
        (see ``tests/fixtures/sync_record_values_agent_id_probes.json``
        for the regression case).
        """
        if not workflow_ids:
            return {"recordMap": {"workflow": {}}}
        space_id = self.load_account().space_id
        body = {
            "requests": [
                {
                    "pointer": {"table": "workflow", "id": wid, "spaceId": space_id},
                    "version": -1,
                }
                for wid in workflow_ids
            ],
        }
        return await self._post_json("syncRecordValuesMain", body)

    # ----------------------------- async runs ------------------------- #

    async def start_run_detached(
        self,
        *,
        prompt: str,
        workflow_id: str | None = None,
        system: str | None = None,
        model: str | None = None,
        web_search: bool = True,
        workspace_search: bool = True,
        ask_mode: bool = False,
        thread_id: str | None = None,
    ) -> str:
        """Kick off a ``runInferenceTranscript`` and detach.

        Used by ``notion-agent runs start`` for long-running Custom
        Agent workflows whose NDJSON stream would otherwise block the
        CLI for minutes. We POST the same request as :meth:`complete`,
        wait for Notion's 200 (which means the workflow has been
        accepted server-side), then close the socket without reading
        the event stream. Notion keeps the run alive — ``runs list``
        can poll ``getInferenceTranscriptsForWorkflow`` afterwards to
        see progress.

        Returns the ``thread_id`` Notion will file the run under.
        Thread state is persisted as in :meth:`complete` so the
        operator can ``chat --thread-id <id>`` a follow-up once the
        run completes.

        R5-A confirmed the run survives socket close: Notion's edge
        accepts the inference request before any NDJSON event is
        written, and ``saveAllThreadOperations: true`` is already in
        the request body so server-side state is persisted
        regardless of how the client behaves after the 200.
        """
        prep = self._prepare_call(
            prompt=prompt,
            system=system,
            model=model,
            web_search=web_search,
            workspace_search=workspace_search,
            ask_mode=ask_mode,
            thread_id=thread_id,
            workflow_id=workflow_id,
        )
        async with self._inference_stream(prep.url, prep.body, prep.headers) as resp:
            if resp.status_code != 200:
                await self._raise_for_http(resp)
            # Socket closes when the context manager exits without
            # consuming aiter_lines — server keeps the workflow
            # running because the 200 has already been emitted.
        prep.save_state()
        return prep.active_thread_id

    async def fetch_workflow_transcripts(
        self, workflow_id: str, *, limit: int = 10, user_id: str | None = None,
    ) -> dict[str, object]:
        """Wrap ``/api/v3/getInferenceTranscriptsForWorkflow``.

        R5-A § getInferenceTranscriptsForWorkflow: returns a transcript
        list (id + title + ``usage_summary.last_updated_time``) for
        threads filed under ``workflow_id``. The optional ``user_id``
        narrows the listing to runs that user initiated (chat-panel
        UI sends this filter on the right-rail thread list).

        Use :func:`notion_agent_cli.runs.parse_workflow_transcripts`
        to unwrap the response into :class:`TranscriptRun` rows.
        """
        body: dict[str, object] = {
            "workflowId": workflow_id,
            "spaceId":    self.load_account().space_id,
            "limit":      limit,
        }
        if user_id is not None:
            body["userId"] = user_id
        return await self._post_json("getInferenceTranscriptsForWorkflow", body)

    async def fetch_user_transcripts(
        self, *, limit: int = 50, include_writer_chats: bool = False,
    ) -> dict[str, object]:
        """Wrap ``/api/v3/getInferenceTranscriptsForUser``.

        Body shape was rewritten in Notion's 2026-05-19 web release
        (recap harness archive ``docs/recap/archives/2026-05-19T1904…``
        and ``2026-05-19T1909…``). The endpoint dropped its top-level
        ``spaceId`` / ``userId`` parameters and replaced them with a
        ``threadParentPointer`` that mirrors the ``runInferenceTranscript``
        request shape. The active user is now inferred from the
        ``x-notion-active-user-header`` + ``token_v2`` cookie instead
        of a body field.

        Returns the same shape as ``fetch_workflow_transcripts`` —
        ``transcripts[]`` rich entries — so the parser in
        ``runs.parse_workflow_transcripts`` still applies.

        ``include_writer_chats`` defaults to ``False`` to mirror the
        chat panel's own pre-fetch; pass ``True`` to also surface
        Writer-mode threads.
        """
        space_id = self.load_account().space_id
        body: dict[str, object] = {
            "threadParentPointer": {
                "table":   "space",
                "id":      space_id,
                "spaceId": space_id,
            },
            "limit":              limit,
            "includeWriterChats": include_writer_chats,
        }
        return await self._post_json("getInferenceTranscriptsForUser", body)

    async def fetch_thread_space_id(self, thread_id: str) -> dict[str, object]:
        """Wrap ``/api/v3/getThreadSpaceId`` — resolve a thread to its workspace.

        Notion added this endpoint in the 2026-05-19 web release. The
        chat panel calls it when it has a ``threadId`` from a URL (e.g.
        ``https://www.notion.so/chat?t=<id>``) but doesn't yet know
        which space the thread belongs to.

        Body:    ``{"threadId": "<uuid>"}``
        Returns: ``{"spaceId": "<uuid>"}``

        Our CLI already knows the space from ``notion_account.json``,
        so this is rarely needed in normal flow — exposed mainly so a
        future "resume by thread URL" workflow doesn't have to bypass
        the wrapper layer.
        """
        return await self._post_json("getThreadSpaceId", {"threadId": thread_id})

    async def mark_transcript_seen(self, thread_id: str) -> dict[str, object]:
        """Wrap ``/api/v3/markInferenceTranscriptSeen`` — clear unread flag.

        Notion added this in the 2026-05-19 web release (the chat panel
        calls it when the user opens a previously-unread thread). Useful
        for scripts that consume threads programmatically and want to
        keep the workspace's unread counter accurate.

        Body:    ``{"spaceId": "<uuid>", "threadId": "<uuid>"}``
        Returns: ``{"ok": true}``
        """
        return await self._post_json(
            "markInferenceTranscriptSeen",
            {
                "spaceId":  self.load_account().space_id,
                "threadId": thread_id,
            },
        )

    async def fetch_unread_transcript_count(
        self, *, thread_parent_id: str | None = None,
    ) -> dict[str, object]:
        """Wrap ``/api/v3/getInferenceTranscriptsUnreadCount``.

        Returns the number of unseen threads under the given
        ``threadParentId`` (defaults to the bound space, which mirrors
        the chat panel's own poll). Captured 2026-05-19.

        Body:    ``{"spaceId": "<uuid>", "threadParentId": "<uuid>"}``
        Returns: ``{"count": <int>}``
        """
        space_id = self.load_account().space_id
        return await self._post_json(
            "getInferenceTranscriptsUnreadCount",
            {
                "spaceId":        space_id,
                "threadParentId": thread_parent_id or space_id,
            },
        )

    async def fetch_paused_workflow_runs(
        self,
        workflow_id: str,
        *,
        count_only: bool = False,
        paused_reasons: tuple[str, ...] = (
            "creditLimit", "runLimit", "runawayCreditUsage",
        ),
    ) -> dict[str, object]:
        """Wrap ``/api/v3/listPausedWorkflowRuns``.

        Returns the raw JSON. R5-A captured the request shape only —
        the operator's workspace had no paused runs at the time, so
        the response shape (both ``countOnly:true`` and
        ``countOnly:false`` branches) is operator-discovery for now.
        The CLI surfaces the raw response as JSON.
        """
        body: dict[str, object] = {
            "spaceId":       self.load_account().space_id,
            "workflowId":    workflow_id,
            "pausedReasons": list(paused_reasons),
            "countOnly":     count_only,
        }
        return await self._post_json("listPausedWorkflowRuns", body)

    async def sync_record(
        self, table: str, record_id: str, *, with_space_id: bool = True,
    ) -> dict[str, object]:
        """Single ``syncRecordValuesMain`` lookup for one record — debug aid.

        Used by ``notion-agent agents inspect``. ``table`` is one of
        ``workflow`` / ``bot`` / ``block`` / ``notion_user`` / ``space``
        / ``team`` / etc. Some tables require ``spaceId`` on the pointer;
        the optional flag lets the operator probe both shapes when an
        endpoint is unfamiliar.
        """
        pointer: dict[str, str] = {"table": table, "id": record_id}
        if with_space_id:
            pointer["spaceId"] = self.load_account().space_id
        body = {"requests": [{"pointer": pointer, "version": -1}]}
        return await self._post_json("syncRecordValuesMain", body)
