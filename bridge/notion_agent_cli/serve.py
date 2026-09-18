"""FastAPI wrapper exposing the CLI over HTTP — see ``notion-agent serve``.

Optional install group ``notion-agent-cli[serve]`` brings in ``fastapi``
+ ``uvicorn``; importing this module raises ``ImportError`` when those
aren't installed, so the CLI checks availability before forwarding.

Endpoints (full surface in ``docs/NEXT-SESSION.md §D3``):

- ``POST /chat``       — body mirrors :meth:`NotionAgentClient.complete`
                         kwargs; ``stream=true`` returns SSE chunks,
                         ``stream=false`` returns the final ChatResponse JSON.
- ``GET  /healthz``    — doctor-style live ping; 200 on a passing account,
                         503 otherwise.
- ``GET  /agents``     — wraps :func:`agents.parse_agents`.
- ``GET  /threads``    — wraps :func:`agents.parse_threads`; supports
                         ``agent`` + ``limit`` query params.

The single :class:`NotionAgentClient` is created in the lifespan handler
and yielded via a FastAPI dependency. Tests pre-populate
``app.state.client`` with a MockTransport-backed instance; the lifespan
respects that and only constructs a fresh client when one isn't set.

The ``--reload`` codepath in the CLI passes uvicorn an import string
(:func:`_reload_app`) and reads the account path from the
``NOTION_AGENT_CLI_ACCOUNT`` environment variable. Normal runs use
:func:`create_app` directly with the path argument.

Error → HTTP-status mapping lives in :data:`_ERROR_STATUS`. The
``ErrorCode`` string from :class:`NotionAgentError` is surfaced in the
response body so HTTP consumers branch on the same taxonomy as direct
library callers.
"""
import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from notion_agent_cli.agents import parse_agents, parse_threads
from notion_agent_cli.bootstrap import fetch_user_content
from notion_agent_cli.exceptions import ErrorCode, NotionAgentError
from notion_agent_cli.provider import NotionAgentClient

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Error → HTTP status mapping
# --------------------------------------------------------------------------- #

_ERROR_STATUS: dict[str, int] = {
    ErrorCode.AUTH_INVALID:           401,
    ErrorCode.PREMIUM_REQUIRED:       402,
    ErrorCode.EMPTY_PROMPT:           400,
    ErrorCode.INVALID_CALLBACK:       400,
    ErrorCode.THREAD_STATE_MISSING:   404,
    ErrorCode.THREAD_STATE_MALFORMED: 500,
    ErrorCode.ACCOUNT_MISSING:        500,
    ErrorCode.ACCOUNT_MALFORMED:      500,
    ErrorCode.ACCOUNT_INVALID:        500,
    ErrorCode.NOTION_ERROR:           502,
    ErrorCode.HTTP_ERROR:             502,
    ErrorCode.TRANSPORT:              503,
    ErrorCode.EMPTY_TEXT:             502,
    ErrorCode.WORKSPACE_EMPTY:        500,
    ErrorCode.WORKSPACE_AMBIGUOUS:    500,
    ErrorCode.UNKNOWN:                500,
}


def _error_status(err: NotionAgentError) -> int:
    return _ERROR_STATUS.get(str(err.code), 500)


def _error_body(err: NotionAgentError) -> dict[str, str]:
    return {"code": str(err.code), "message": str(err)}


# --------------------------------------------------------------------------- #
# Request schemas
# --------------------------------------------------------------------------- #

class ChatBody(BaseModel):
    prompt: str
    system: str | None = None
    model: str | None = None
    web_search: bool = True
    workspace_search: bool = True
    ask_mode: bool = False
    thread_id: str | None = None
    stream: bool = Field(False, description="If true, return SSE; else single JSON.")


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #

def create_app(account_path: Path | str | None = None) -> FastAPI:
    """Build a FastAPI app bound to ``account_path``."""
    resolved_path = (
        Path(account_path).expanduser()
        if account_path is not None
        else Path.home() / ".notionagents" / "notion_account.json"
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Tests pre-populate ``app.state.client`` with a MockTransport-
        # backed instance; don't clobber it.
        owns_client = not getattr(app.state, "client", None)
        if owns_client:
            app.state.client = NotionAgentClient(resolved_path)
        try:
            yield
        finally:
            if owns_client:
                await app.state.client.aclose()

    app = FastAPI(
        title="notion-agent-cli",
        version="0.1.0",
        description="HTTP wrapper around Notion's ✦ AI / Custom Agent endpoint.",
        lifespan=lifespan,
    )

    def get_client() -> NotionAgentClient:
        """Dependency — request-scoped accessor for the lifespan-managed client."""
        return app.state.client

    # --------------- /chat ------------------------------------------------- #

    async def _chat_blocking(body: ChatBody, client: NotionAgentClient) -> JSONResponse:
        try:
            resp = await client.complete(
                prompt=body.prompt,
                system=body.system,
                model=body.model,
                web_search=body.web_search,
                workspace_search=body.workspace_search,
                ask_mode=body.ask_mode,
                thread_id=body.thread_id,
            )
        except NotionAgentError as e:
            return JSONResponse(_error_body(e), status_code=_error_status(e))
        return JSONResponse({
            "text":      resp.text,
            "model":     resp.model,
            "thread_id": resp.thread_id,
            "thinking":  resp.thinking,
            "usage": {
                "input_tokens":   resp.usage.input_tokens,
                "output_tokens":  resp.usage.output_tokens,
                "cache_read":     resp.usage.cache_read,
                "cache_creation": resp.usage.cache_creation,
            },
        })

    async def _chat_sse(body: ChatBody, client: NotionAgentClient) -> StreamingResponse:
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def on_delta(chunk: str) -> None:
            await queue.put(chunk)

        async def runner() -> NotionAgentError | None:
            try:
                await client.complete(
                    prompt=body.prompt,
                    system=body.system,
                    model=body.model,
                    web_search=body.web_search,
                    workspace_search=body.workspace_search,
                    ask_mode=body.ask_mode,
                    thread_id=body.thread_id,
                    on_text_delta_async=on_delta,
                )
                return None
            except NotionAgentError as e:
                return e
            finally:
                await queue.put(None)  # sentinel

        async def event_stream() -> AsyncIterator[bytes]:
            task = asyncio.create_task(runner())
            try:
                while True:
                    chunk = await queue.get()
                    if chunk is None:
                        break
                    # JSON-encode so embedded newlines + quotes don't
                    # break SSE framing.
                    yield f"data: {json.dumps({'text': chunk}, ensure_ascii=False)}\n\n".encode()
                err = await task
                if err is not None:
                    yield (
                        "event: error\ndata: "
                        + json.dumps(_error_body(err), ensure_ascii=False)
                        + "\n\n"
                    ).encode()
                yield b"data: [DONE]\n\n"
            finally:
                # Defensive — runner usually finishes naturally above,
                # but a downstream disconnect mid-stream would orphan it.
                if not task.done():
                    task.cancel()

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    ClientDep = Annotated[NotionAgentClient, Depends(get_client)]

    @app.post("/chat")
    async def chat(
        body: Annotated[ChatBody, Body(...)],
        client: ClientDep,
    ) -> Any:
        if body.stream:
            return await _chat_sse(body, client)
        return await _chat_blocking(body, client)

    # --------------- /healthz --------------------------------------------- #

    @app.get("/healthz")
    async def healthz(client: ClientDep) -> JSONResponse:
        checks: list[dict[str, str]] = []
        try:
            acc = client.load_account()
        except NotionAgentError as e:
            checks.append({"status": "fail", "check": "account",
                           "detail": f"[{e.code}] {e}"})
            return JSONResponse({"status": "fail", "checks": checks}, status_code=503)
        checks.append({"status": "ok", "check": "account", "detail": str(acc.space_name)})

        try:
            await fetch_user_content(
                token_v2=acc.token_v2,
                user_id=acc.user_id,
                browser_id=acc.browser_id or "",
                http_client=client._get_client(),
            )
        except NotionAgentError as e:
            checks.append({"status": "fail", "check": "live_ping",
                           "detail": f"[{e.code}] {e}"})
            return JSONResponse({"status": "fail", "checks": checks}, status_code=503)
        checks.append({"status": "ok", "check": "live_ping", "detail": ""})
        return JSONResponse({"status": "ok", "checks": checks})

    # --------------- /agents ---------------------------------------------- #

    @app.get("/agents")
    async def agents(
        client: ClientDep,
        limit: Annotated[int, Query(ge=0)] = 20,
    ) -> list[dict[str, Any]]:
        try:
            raw = await client.fetch_custom_agents()
        except NotionAgentError as e:
            raise HTTPException(status_code=_error_status(e), detail=_error_body(e)) from e
        items = parse_agents(raw)
        if limit > 0:
            items = items[:limit]
        return [
            {
                "agent_id":                 a.agent_id,
                "activity_score":           a.activity_score,
                "most_recent_thread_id":    a.most_recent_thread_id,
                "most_recent_thread_title": a.most_recent_thread_title,
            }
            for a in items
        ]

    # --------------- /threads --------------------------------------------- #

    @app.get("/threads")
    async def threads(
        client: ClientDep,
        limit: Annotated[int, Query(ge=0)] = 20,
        agent: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            raw = await client.fetch_custom_agents()
        except NotionAgentError as e:
            raise HTTPException(status_code=_error_status(e), detail=_error_body(e)) from e
        items = parse_threads(raw)
        if agent:
            items = [t for t in items if t.parent_agent_id == agent]
        if limit > 0:
            items = items[:limit]
        return [
            {
                "thread_id":       t.thread_id,
                "title":           t.title,
                "parent_agent_id": t.parent_agent_id,
                "created_at_ms":   t.created_at_ms,
                "updated_at_ms":   t.updated_at_ms,
                "created_by_id":   t.created_by_id,
                "created_source":  t.created_source,
            }
            for t in items
        ]

    return app


def _reload_app() -> FastAPI:
    """Factory used by ``notion-agent serve --reload``.

    uvicorn's reload mode requires an import string + factory; the CLI
    sets ``NOTION_AGENT_CLI_ACCOUNT`` before re-launching uvicorn so the
    re-imported worker can rebuild the same app.
    """
    account = os.environ.get("NOTION_AGENT_CLI_ACCOUNT")
    return create_app(account)
