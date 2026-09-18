"""``notion-agent`` console entry point.

Subcommands: ``init`` / ``chat`` / ``doctor`` / ``models`` /
``agents`` / ``threads`` / ``serve``. Run ``notion-agent --help``.

See ``docs/ROADMAP.md`` for what's pending.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import re
import shlex
import sys
import time
from pathlib import Path

from notion_agent_cli import __version__
from notion_agent_cli.account import (
    NotionAccount,
    build_cookie_header,
    load_notion_account,
    save_notion_account,
)
from notion_agent_cli.agents import (
    AgentSummary,
    ThreadSummary,
    parse_agents,
    parse_threads,
    parse_workflow_records,
)
from notion_agent_cli.bootstrap import (
    AmbiguousWorkspaceError,
    bootstrap_account,
    fetch_user_content,
    parse_browser_cookie,
)
from notion_agent_cli.exceptions import (
    NotionAgentError,
    exit_code_for,
    retry_policy_for,
)
from notion_agent_cli.models import (
    DEFAULT_USER_MODELS_PATH,
    load_user_model_map,
    parse_available_models,
    resolve_alias,
    save_user_model_map,
)
from notion_agent_cli.profile import (
    DEFAULT_PROFILE_DIR,
    current_profile,
    list_profiles,
    migrate_account_to_profile,
    use_profile,
)
from notion_agent_cli.provider import (
    NotionAgentClient,
    fetch_live_client_version,
    fetch_session_warmup,
)
from notion_agent_cli.runs import TranscriptRun, parse_workflow_transcripts

DEFAULT_ACCOUNT_PATH = Path.home() / ".notionagents" / "notion_account.json"


# --------------------------------------------------------------------------- #
# init subcommand
# --------------------------------------------------------------------------- #

def _add_init_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "init",
        help="Bootstrap notion_account.json from a token_v2 cookie.",
        description=(
            "Bootstrap workflow: pass just `--token-v2 <value>` — Notion's "
            "/loadUserContent endpoint reports your user_id back, so the "
            "token alone is enough. For convenience you can also paste a "
            "full `document.cookie` string via `--cookie` (token_v2, "
            "notion_user_id, notion_browser_id are extracted automatically); "
            "individual flags override matching values inside --cookie."
        ),
    )
    p.add_argument("--cookie", default=None,
                   help="Full document.cookie string from DevTools "
                        "(Application -> Cookies -> notion.so, then 'copy all'). "
                        "token_v2, notion_user_id, notion_browser_id are "
                        "extracted automatically. "
                        "Pass `--cookie -` to read the cookie from stdin "
                        "(keeps it out of shell history).")
    p.add_argument("--token-v2", default=None,
                   help="token_v2 cookie value (URL-encoded form is fine). "
                        "Required unless --cookie supplies it. "
                        "Pass `--token-v2 -` to read it from stdin "
                        "(keeps the token out of shell history / `ps`).")
    p.add_argument("--user-id", default=None,
                   help="Your Notion user_id (UUID). Optional — auto-derived "
                        "from /loadUserContent if neither --user-id nor "
                        "--cookie supplies it.")
    p.add_argument("--browser-id", default=None,
                   help="notion_browser_id cookie value (random UUID if "
                        "neither --browser-id nor --cookie supplies it).")
    p.add_argument("--space-name", default=None,
                   help="Workspace display name when you have multiple workspaces.")
    p.add_argument("--space-domain", default=None,
                   help="Workspace domain slug when you have multiple workspaces.")
    p.add_argument("--agent-name", default="",
                   help="Custom-agent display name (e.g. 'Jarvis') for chat-panel binding.")
    p.add_argument("--agent-accessory", default="",
                   help="Custom-agent avatar accessory (e.g. 'cat').")
    p.add_argument("--agent-page-id", default="",
                   help="Custom-agent persistent_instructions_page UUID.")
    p.add_argument("--default-model", default="opus-4.8",
                   help="Default model alias (default: %(default)s).")
    p.add_argument("--timezone", default="America/Los_Angeles",
                   help="IANA timezone for the context block (default: %(default)s).")
    p.add_argument("--account", type=Path, default=DEFAULT_ACCOUNT_PATH,
                   help="Where to write the credential file (default: %(default)s).")
    p.add_argument("--force", action="store_true",
                   help="Overwrite an existing account file without prompting.")
    p.set_defaults(func=_cmd_init)


async def _run_init(args: argparse.Namespace) -> NotionAccount:
    return await bootstrap_account(
        token_v2=args.token_v2,
        user_id=args.user_id,
        browser_id=args.browser_id,
        space_name=args.space_name,
        space_domain=args.space_domain,
        agent_name=args.agent_name,
        agent_accessory=args.agent_accessory,
        agent_context_page_id=args.agent_page_id,
        default_model=args.default_model,
        timezone=args.timezone,
    )


async def _lookup_agent_by_page_id(
    acc: NotionAccount, page_id: str,
) -> AgentSummary | None:
    """Return the agents-list entry whose ``agent_page_id`` matches, or None.

    Called from ``init`` to reverse-resolve a user-supplied
    ``--agent-page-id`` back to the workflow-registered name. The
    in-memory ``acc`` is injected into the client so we don't need to
    write the credential to disk first.
    """
    async with NotionAgentClient(None, account=acc) as client:
        raw = await client.fetch_custom_agents()
        agent_ids = [x for x in (raw.get("agentIds") or []) if isinstance(x, str)]
        if not agent_ids:
            return None
        wf_raw = await client.fetch_workflow_records(agent_ids)
    workflows = parse_workflow_records(wf_raw)
    agents = parse_agents(raw, workflows=workflows)
    for a in agents:
        if a.agent_page_id == page_id:
            return a
    return None


def _maybe_enrich_agent_binding(
    acc: NotionAccount, args: argparse.Namespace,
) -> NotionAccount:
    """If --agent-page-id matches a registered Custom Agent, enrich agent_name.

    Behavior matrix:
    - page_id matches a workflow record → fill agent_name from
      ``workflow.data.name`` (only when ``--agent-name`` wasn't supplied,
      so a user override always wins), set ``agent_binding_mode =
      "persona_overlay"``.
    - page_id does NOT match + ``--agent-name`` supplied → no enrichment
      to agent_name (user value wins), set ``agent_binding_mode =
      "free_form_steering"`` so chat output can label the surface
      accurately.
    - page_id does NOT match + no ``--agent-name`` → emit a one-line
      warning; ``has_jarvis_binding`` stays False (no name) so this
      effectively falls through to ``default_ai``.
    - Network/parse failure → silently skip; the binding still gets
      written with whatever the user supplied, ``agent_binding_mode``
      stays empty so chat output reports ``null``.
    """
    if not args.agent_page_id:
        return acc
    try:
        match = asyncio.run(_lookup_agent_by_page_id(acc, args.agent_page_id))
    except NotionAgentError:
        # Best-effort enrichment: a transient lookup failure mustn't
        # block init. The user already supplied a page id; we just
        # couldn't confirm whether it's a registered Custom Agent.
        return acc
    if match is not None and match.name:
        print(
            f"[init] page_id matched registered Custom Agent: {match.name!r} "
            "(binding_mode: persona_overlay)",
            file=sys.stderr,
        )
        # Replace agent_name only if the user didn't pass --agent-name;
        # an explicit user value wins because operators sometimes alias
        # an agent by a different display name in the local profile.
        updates: dict[str, str] = {"agent_binding_mode": "persona_overlay"}
        if not args.agent_name:
            updates["agent_name"] = match.name
        return dataclasses.replace(acc, **updates)
    if not args.agent_name:
        print(
            f"warning: --agent-page-id {args.agent_page_id} not found in "
            "Custom Agents list — binding as a free-form steering page "
            "(chat panel will use it as persistent_instructions_page "
            "context, but `surface=\"custom_agent\"` reflects the bound "
            "page, not a registered Custom Agent persona). Pass "
            "--agent-name to silence this warning.",
            file=sys.stderr,
        )
        return acc
    # User supplied both --agent-name and --agent-page-id but the page
    # isn't a registered Custom Agent → free-form steering with their
    # chosen display name. Record the mode so chat output stays honest.
    print(
        f"[init] page_id not in Custom Agents list — binding "
        f"{args.agent_name!r} as free-form steering page "
        "(binding_mode: free_form_steering)",
        file=sys.stderr,
    )
    return dataclasses.replace(acc, agent_binding_mode="free_form_steering")


def _cmd_init(args: argparse.Namespace) -> int:
    if args.cookie == "-":
        # Keep the cookie out of shell history / `ps` output by reading
        # it from stdin instead of argv.
        args.cookie = sys.stdin.read().strip()
        if not args.cookie:
            print("error: --cookie - read an empty cookie from stdin",
                  file=sys.stderr)
            return 2
    if args.token_v2 == "-":
        args.token_v2 = sys.stdin.read().strip()
        if not args.token_v2:
            print("error: --token-v2 - read an empty token from stdin",
                  file=sys.stderr)
            return 2
    if args.cookie:
        parsed = parse_browser_cookie(args.cookie)
        # The full document.cookie should contain a token_v2= entry. If
        # the operator passed a bare token string to --cookie, the parse
        # yields nothing useful — surface that as a clearer hint than the
        # generic "--token-v2 is required" we used to print.
        if not parsed.get("token_v2") and not args.token_v2:
            print(
                "error: --cookie didn't contain `token_v2=...` — did you "
                "mean `--token-v2 <value>` instead? "
                "--cookie expects a full document.cookie string "
                "(name=value pairs separated by `;`).",
                file=sys.stderr,
            )
            return 2
        args.token_v2 = args.token_v2 or parsed.get("token_v2", "")
        args.user_id = args.user_id or parsed.get("notion_user_id", "")
        if args.browser_id is None:
            args.browser_id = parsed.get("notion_browser_id") or None

    if not args.token_v2:
        print(
            "error: --token-v2 (or --cookie containing token_v2=...) is required. "
            "If you only have the token value, pass `--token-v2 <value>` "
            "(or `--token-v2 -` to read from stdin).",
            file=sys.stderr,
        )
        return 2

    # Heads-up: an unbound --agent-name silently lands the account in
    # "default chat" because has_jarvis_binding requires BOTH name and
    # page_id. Surface that before we write so the operator doesn't think
    # the flag worked.
    if args.agent_name and not args.agent_page_id:
        print(
            f"warning: --agent-name {args.agent_name!r} given without "
            "--agent-page-id; no custom-agent binding will be created "
            "(threads will appear under default ✦ AI). Pass "
            "--agent-page-id <uuid> from the agent's instructions-page "
            "URL to bind. The CLI does not resolve agent names server-side.",
            file=sys.stderr,
        )

    if args.account.exists() and not args.force:
        print(
            f"error: {args.account} already exists — pass --force to overwrite",
            file=sys.stderr,
        )
        return 2
    try:
        acc = asyncio.run(_run_init(args))
    except AmbiguousWorkspaceError as e:
        print("Multiple workspaces available — re-run with --space-name or --space-domain:",
              file=sys.stderr)
        for w in e.workspaces:
            print(
                f"  - name={w.space_name!r}  domain={w.domain!r}  id={w.space_id}",
                file=sys.stderr,
            )
        # Suggest a ready-to-copy rerun for the first workspace; shlex
        # quotes the name so workspaces containing spaces or curly quotes
        # (e.g. "Lucien Chen's space") don't get mis-split by the shell.
        first = e.workspaces[0]
        rerun = (
            "  notion-agent init --token-v2 <your-token-v2> "
            f"--space-name {shlex.quote(first.space_name or first.domain)}"
        )
        print("Try:", file=sys.stderr)
        print(rerun, file=sys.stderr)
        return 3
    except NotionAgentError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # Reverse-resolve --agent-page-id against the workspace's Custom
    # Agents list: if it matches a registered agent, enrich agent_name
    # (when the user didn't override); if it doesn't, warn that this is
    # a free-form steering page rather than a registered persona.
    acc = _maybe_enrich_agent_binding(acc, args)

    # Persist the FULL document.cookie when the operator passed one. The
    # chat path sends ``full_cookie`` verbatim (build_cookie_header), so
    # this carries the Cloudflare (``__cf_bm`` / ``_cfuvid``) and session
    # cookies the browser holds — the difference that lets Notion's trust
    # rule treat the CLI like the real client instead of bare automation.
    # A lone ``--token-v2`` (no full jar) leaves it empty → minimal
    # stitched cookie, same as before.
    if args.cookie and "token_v2=" in args.cookie:
        acc = dataclasses.replace(acc, full_cookie=args.cookie)

    save_notion_account(acc, args.account)
    print(f"[init] wrote {args.account}")
    if acc.full_cookie:
        print("[init] stored full browser cookie (includes Cloudflare/session "
              "cookies) — re-run init with a fresh cookie if chat starts "
              "failing with a trust-rule denial")
    print(f"[init] workspace: {acc.space_name!r}  ({acc.space_id})")
    print(f"[init] user:      {acc.user_name!r} <{acc.user_email}>")
    if acc.has_jarvis_binding:
        mode = acc.agent_binding_mode or "unknown"
        print(
            f"[init] agent:     {acc.agent_name!r}  "
            f"page={acc.agent_context_page_id}  "
            f"binding_mode={mode}"
        )
    else:
        print("[init] agent:     (default chat — no custom-agent binding)")
    return 0


# --------------------------------------------------------------------------- #
# chat subcommand
# --------------------------------------------------------------------------- #

def _add_chat_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "chat",
        help="Send one prompt, print the reply.",
        description=(
            "One round-trip to Notion's ✦ AI chat endpoint. The conversation "
            "appears in the chat panel under the bound custom agent (if set "
            "during `init`)."
        ),
    )
    p.add_argument("prompt", nargs="?",
                   help="Prompt text. If omitted, read from stdin.")
    p.add_argument("--system", default=None,
                   help="Optional system-style preamble stitched into the user message.")
    p.add_argument("--model", default=None,
                   help=("Friendly alias (opus-4.8 / sonnet-4.6 / haiku-4.5 / ...), "
                         "Anthropic id (claude-opus-4-8), or Notion internal id "
                         "(ambrosia-tart-high). Defaults to account.default_model "
                         "(opus-4.8). Run `notion-agent models refresh` to sync "
                         "Notion's latest id rotation."))
    p.add_argument("--ask-mode", action="store_true",
                   help="useReadOnlyMode=true — model answers but skips page edits.")
    p.add_argument("--no-web-search", action="store_true",
                   help="Disable the built-in web search tool.")
    p.add_argument("--no-workspace-search", action="store_true",
                   help="Disable workspace search.")
    p.add_argument("--stream", action="store_true",
                   help="Print text chunks as they stream in (vs all-at-end).")
    p.add_argument("--json", dest="json_out", action="store_true",
                   help="Output a structured JSON object to stdout (text + usage + thread_id).")
    p.add_argument("--ndjson", action="store_true",
                   help="Pipe Notion's raw NDJSON event stream straight to stdout "
                        "(no parsing, no terminator). Mutually exclusive with --stream/--json. "
                        "Thread state still persists on success — --thread-id round-trips.")
    p.add_argument("--thread-id", default=None,
                   help="Continue an existing thread (use the thread_id from "
                        "a prior --json response). Requires the thread's "
                        "state file under ~/.notionagents/threads/. The "
                        "thread's original model is locked — --model is "
                        "ignored on continuation.")
    p.add_argument("--account", type=Path, default=DEFAULT_ACCOUNT_PATH,
                   help="Account file (default: %(default)s).")
    p.set_defaults(func=_cmd_chat)


def _read_prompt(args: argparse.Namespace) -> str:
    if args.prompt is not None:
        return args.prompt
    if sys.stdin.isatty():
        print("error: no prompt given (pass a positional arg or pipe stdin)",
              file=sys.stderr)
        sys.exit(2)
    return sys.stdin.read()


CLIENT_VERSION_TTL_HOURS = 24.0


# __cf_bm lives ~30 min; refresh a little early so an in-flight call never
# races the expiry.
CF_COOKIE_TTL_MINUTES = 25.0

# Cookies worth capturing from the /ai warm-up response: Cloudflare's bot
# clearance (the short-lived one Notion's trust rule checks) plus any
# rotated session token.
_REFRESHABLE_COOKIES = ("__cf_bm", "_cfuvid", "token_v2")


def _merge_cookie_header(cookie_str: str, updates: dict[str, str]) -> str:
    """Overlay ``updates`` onto a ``name=value; …`` cookie string.

    Preserves existing order, appends genuinely-new names, and overwrites
    values in place. Used to fold a freshly-minted ``__cf_bm`` back into
    the stored ``full_cookie`` without disturbing the rest of the jar.
    """
    pairs: dict[str, str] = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            pairs[k.strip()] = v.strip()
    pairs.update(updates)
    return "; ".join(f"{k}={v}" for k, v in pairs.items())


def _is_fresh(ts: object, **delta: float) -> bool:
    import datetime as _dt

    if not isinstance(ts, str):
        return False
    try:
        last = _dt.datetime.fromisoformat(ts)
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=_dt.UTC)
    return _dt.datetime.now(_dt.UTC) - last < _dt.timedelta(**delta)


async def _maybe_refresh_session(
    account_path: Path,
    *,
    cv_ttl_hours: float = CLIENT_VERSION_TTL_HOURS,
    cf_ttl_minutes: float = CF_COOKIE_TTL_MINUTES,
) -> None:
    """Keep the session browser-fresh before an inference call.

    Two things age out and get the CLI denied if left stale:

    - ``client_version`` — Notion ships a new web build ~daily and rejects
      too-old ``notion-client-version`` headers.
    - ``__cf_bm`` — Cloudflare's bot-clearance cookie (~30 min TTL) that
      Notion's trust rule checks once it tightens.

    Both are refreshed from a single GET of the ``/ai`` shell (see
    :func:`fetch_session_warmup`): the HTML carries the live build id, and
    Cloudflare mints a fresh ``__cf_bm`` on that navigation when no live
    one is presented. We persist both into the account file (with
    ``client_version_refreshed_at`` / ``cf_refreshed_at`` stamps) so the
    next call inside the TTL skips the round-trip. Entirely best-effort:
    any failure leaves the last-known values in place.
    """
    import datetime as _dt

    try:
        acc = load_notion_account(account_path)
    except NotionAgentError:
        return  # let the downstream client surface the account problem

    cv_fresh = _is_fresh(acc.extras.get("client_version_refreshed_at"), hours=cv_ttl_hours)
    cf_fresh = (
        "__cf_bm=" in (acc.full_cookie or "")
        and _is_fresh(acc.extras.get("cf_refreshed_at"), minutes=cf_ttl_minutes)
    )
    if cv_fresh and cf_fresh:
        return  # nothing to do — skip the network round-trip

    cookie = acc.full_cookie or build_cookie_header(acc)
    try:
        version, set_cookies = await fetch_session_warmup(
            user_agent=acc.user_agent, cookie=cookie,
        )
    except NotionAgentError:
        return  # offline-safe: keep the stored session

    now_iso = _dt.datetime.now(_dt.UTC).isoformat()
    extras = {**acc.extras}
    new_cv = acc.client_version
    if version:
        new_cv = version
        extras["client_version_refreshed_at"] = now_iso

    new_full = acc.full_cookie
    cf_updates = {k: v for k, v in set_cookies.items() if k in _REFRESHABLE_COOKIES}
    if cf_updates:
        new_full = _merge_cookie_header(cookie, cf_updates)
    # Stamp cf freshness whenever the warm-up succeeded: either CF reissued
    # __cf_bm (captured above) or the one we sent is still live (no reissue),
    # both of which mean the clearance is good for another window.
    if "__cf_bm=" in (new_full or "") or "__cf_bm=" in cookie:
        extras["cf_refreshed_at"] = now_iso

    if new_cv == acc.client_version and new_full == acc.full_cookie and extras == acc.extras:
        return  # nothing actually changed
    updated = dataclasses.replace(acc, client_version=new_cv, full_cookie=new_full or "", extras=extras)
    save_notion_account(updated, account_path)


def _emit_error(e: NotionAgentError, *, json_out: bool) -> int:
    """Print a failure and return its process exit code.

    The human-readable ``error: ...`` line always goes to stderr, so
    non-JSON callers, logs, and ``--ndjson`` keep their existing
    behaviour. When ``--json`` is set we ALSO emit a structured
    ``{"error": {...}}`` object on stdout so an automated caller can
    branch on ``code`` / ``retryable`` / ``retry_after_seconds`` without
    scraping stderr. A successful ``chat --json`` carries no ``"error"``
    key, so the key's presence is the success/failure discriminator.

    Exit code is derived from the error code (75 trust-rule, 77 auth,
    else 1) so callers that can't read stdout can still branch.
    """
    print(f"error: {e}", file=sys.stderr)
    if json_out:
        retryable, retry_after = retry_policy_for(e.code)
        # An inline Notion denial reports its own isRetryable — prefer
        # that authoritative value over our per-code default.
        if e.retryable is not None:
            retryable = e.retryable
        print(json.dumps({"error": {
            "code":                str(e.code),
            "subtype":             e.subtype,
            "message":             str(e),
            "retryable":           retryable,
            "retry_after_seconds": retry_after,
        }}, ensure_ascii=False))
    return exit_code_for(e.code)


async def _run_chat(args: argparse.Namespace, prompt: str) -> int:
    await _maybe_refresh_session(args.account)
    on_delta = None
    if args.stream and not args.json_out:
        def on_delta(chunk: str) -> None:
            print(chunk, end="", flush=True)

    async with NotionAgentClient(args.account) as client:
        if args.ndjson:
            try:
                async for line in client.stream_lines(
                    prompt=prompt,
                    system=args.system,
                    model=args.model,
                    web_search=not args.no_web_search,
                    workspace_search=not args.no_workspace_search,
                    ask_mode=args.ask_mode,
                    thread_id=args.thread_id,
                ):
                    print(line, flush=True)
            except NotionAgentError as e:
                # --ndjson is mutually exclusive with --json, so json_out
                # is always False here: stderr line + exit code only.
                return _emit_error(e, json_out=args.json_out)
            return 0

        try:
            resp = await client.complete(
                prompt=prompt,
                system=args.system,
                model=args.model,
                web_search=not args.no_web_search,
                workspace_search=not args.no_workspace_search,
                ask_mode=args.ask_mode,
                on_text_delta=on_delta,
                thread_id=args.thread_id,
            )
        except NotionAgentError as e:
            return _emit_error(e, json_out=args.json_out)

    if args.json_out:
        # Resolve Notion's internal id ("apricot-sorbet-high") back to the
        # friendly alias ("opus-4.7") so agents see something they can
        # reason about. user_map wins so post-`models refresh` ids stay
        # accurate even before a release.
        alias = resolve_alias(resp.model, user_map=load_user_model_map())
        # Load the account once more to surface which chat surface this
        # turn landed on. has_jarvis_binding == True means the thread
        # shows up in Notion's ✦ AI panel under the bound custom agent.
        try:
            acc = load_notion_account(args.account)
            bound = acc.has_jarvis_binding
            agent_name = acc.agent_name or None
            recorded_mode = acc.agent_binding_mode or None
        except NotionAgentError:
            bound = False
            agent_name = None
            recorded_mode = None
        # binding_mode tells LLM operators which Notion AI configuration
        # this call landed in. surface is the UI layer (custom_agent /
        # default_chat); binding_mode adds the *identity* layer: a
        # `custom_agent` surface might be a registered persona overlay
        # OR a free-form steering page. null means the account file
        # predates v0.1.6.1 (re-run init to populate). See AGENTS.md
        # "Two kinds of --agent-page-id" for the full taxonomy.
        if not bound:
            binding_mode: str | None = "default_ai"
        else:
            binding_mode = recorded_mode  # None when account file is legacy
        out = {
            "text":         resp.text,
            "model":        resp.model,
            "model_alias":  alias,
            "thread_id":    resp.thread_id,
            "surface":      "custom_agent" if bound else "default_chat",
            "agent_name":   agent_name,
            "binding_mode": binding_mode,
            "usage": {
                "input_tokens":   resp.usage.input_tokens,
                "output_tokens":  resp.usage.output_tokens,
                "cache_read":     resp.usage.cache_read,
                "cache_creation": resp.usage.cache_creation,
            },
            "thinking_chars": len(resp.thinking),
        }
        print(json.dumps(out, ensure_ascii=False))
    elif args.stream:
        # The text has already been printed delta-by-delta — terminate the line.
        print()
    else:
        print(resp.text)
    return 0


def _cmd_chat(args: argparse.Namespace) -> int:
    if args.ndjson and (args.json_out or args.stream):
        print("error: --ndjson is mutually exclusive with --json / --stream",
              file=sys.stderr)
        return 2
    prompt = _read_prompt(args)
    return asyncio.run(_run_chat(args, prompt))


# --------------------------------------------------------------------------- #
# doctor subcommand — validate account file + ping Notion
# --------------------------------------------------------------------------- #

def _add_doctor_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "doctor",
        help="Validate the account file and ping /loadUserContent.",
        description=(
            "Sanity-checks an account file: file readable + required fields "
            "present + token_v2 still accepted by Notion + bound space_id "
            "still in the user's workspace list. Use when chat suddenly "
            "errors with 'token_v2 expired' or 'space not found' — doctor "
            "pinpoints which check failed."
        ),
    )
    p.add_argument("--account", type=Path, default=DEFAULT_ACCOUNT_PATH,
                   help="Account file to check (default: %(default)s).")
    p.add_argument("--json", dest="json_out", action="store_true",
                   help="Emit a structured JSON report to stdout.")
    p.add_argument("--refresh-client-version", action="store_true",
                   help="Fetch Notion's current web build version and write "
                        "it to the account file's client_version. Fixes the "
                        "'chat suddenly 400s' failure that creeps in as the "
                        "stored version ages behind Notion's near-daily "
                        "releases.")
    p.set_defaults(func=_cmd_doctor)


_DoctorStatus = str  # one of "ok", "fail", "info"


def _render_doctor(
    checks: list[tuple[_DoctorStatus, str, str]],
    *,
    as_json: bool,
) -> str:
    if as_json:
        payload = [
            {"status": status, "check": name, "detail": detail}
            for status, name, detail in checks
        ]
        return json.dumps(payload, indent=2, ensure_ascii=False)
    icons = {"ok": "[ok]  ", "fail": "[FAIL]", "info": "[..]  "}
    lines = []
    for status, name, detail in checks:
        icon = icons.get(status, "[?]   ")
        line = f"{icon} {name}"
        if detail:
            line += f"  -- {detail}"
        lines.append(line)
    return "\n".join(lines)


async def _ping_load_user_content(acc: NotionAccount) -> dict:
    return await fetch_user_content(
        token_v2=acc.token_v2,
        user_id=acc.user_id,
        browser_id=acc.browser_id or "",
    )


def _client_version_days_behind(stored: str, live: str) -> int | None:
    """Whole days between two ``MAJ.MIN.YYYYMMDD.HHMM`` build ids.

    Returns the gap in days (live minus stored), or ``None`` if either
    string doesn't carry a parseable ``YYYYMMDD`` segment. Used only to
    make doctor's staleness message actionable — never load-bearing.
    """
    import datetime as _dt

    def _date(v: str) -> _dt.date | None:
        parts = v.split(".")
        if len(parts) < 3 or len(parts[2]) != 8 or not parts[2].isdigit():
            return None
        try:
            return _dt.datetime.strptime(parts[2], "%Y%m%d").date()
        except ValueError:
            return None

    sd, ld = _date(stored), _date(live)
    if sd is None or ld is None:
        return None
    return (ld - sd).days


def _cmd_doctor(args: argparse.Namespace) -> int:
    checks: list[tuple[_DoctorStatus, str, str]] = []

    # 1) Account file readable + required fields
    try:
        acc = load_notion_account(args.account)
    except NotionAgentError as e:
        checks.append(("fail", "account file readable", f"[{e.code}] {e}"))
        print(_render_doctor(checks, as_json=args.json_out))
        return 1
    checks.append(("ok", "account file readable", str(args.account)))
    checks.append(("ok", "required fields present",
                   f"user={acc.user_email or acc.user_id}  "
                   f"space={acc.space_name!r} ({acc.space_id})"))

    # 2) Jarvis binding (informational)
    if acc.has_jarvis_binding:
        mode = acc.agent_binding_mode or "unknown — re-run `notion-agent init` to record"
        checks.append(("ok", "custom-agent binding",
                       f"{acc.agent_name!r}  page={acc.agent_context_page_id}  "
                       f"binding_mode={mode}"))
    else:
        checks.append(("info", "custom-agent binding",
                       "(none — chats appear in the default ✦ AI surface)"))

    # 3) Live ping: token_v2 + /loadUserContent
    try:
        data = asyncio.run(_ping_load_user_content(acc))
    except NotionAgentError as e:
        checks.append(("fail", "token_v2 accepted by /loadUserContent",
                       f"[{e.code}] {e}"))
        print(_render_doctor(checks, as_json=args.json_out))
        return 1
    checks.append(("ok", "token_v2 accepted by /loadUserContent", ""))

    # 4) Bound space_id still in the response
    rm_spaces = list((data.get("recordMap") or {}).get("space") or {})
    if acc.space_id in rm_spaces:
        checks.append(("ok", "bound space_id present in workspaces",
                       f"{len(rm_spaces)} workspaces total"))
    else:
        checks.append(("fail", "bound space_id present in workspaces",
                       f"bound={acc.space_id!r} but server returned {rm_spaces!r}"))
        print(_render_doctor(checks, as_json=args.json_out))
        return 1

    # 5) User model map (informational)
    user_map = load_user_model_map()
    if user_map:
        checks.append(("ok", "user model map loaded",
                       f"{len(user_map)} aliases at {DEFAULT_USER_MODELS_PATH}"))
    else:
        checks.append(("info", "user model map",
                       "(none — falls back to DEFAULT_MODEL_MAP in models.py)"))

    # 6) client_version freshness vs Notion's live web build. Best-effort:
    #    a failed fetch is informational, not a doctor failure, since the
    #    stored version may still be accepted.
    stored_cv = acc.client_version
    try:
        live_cv = asyncio.run(fetch_live_client_version())
    except NotionAgentError as e:
        checks.append(("info", "client_version freshness",
                       f"could not read live build ([{e.code}]); "
                       f"stored={stored_cv}"))
    else:
        behind = _client_version_days_behind(stored_cv, live_cv)
        gap = f" ({behind}d behind)" if behind and behind > 0 else ""
        if args.refresh_client_version:
            if stored_cv == live_cv:
                checks.append(("ok", "client_version refreshed",
                               f"already current ({live_cv})"))
            else:
                updated = dataclasses.replace(acc, client_version=live_cv)
                save_notion_account(updated, args.account)
                was = f" (was {behind}d behind)" if behind and behind > 0 else ""
                checks.append(("ok", "client_version refreshed",
                               f"{stored_cv} → {live_cv}{was}"))
        elif stored_cv == live_cv:
            checks.append(("ok", "client_version current", live_cv))
        elif stored_cv < live_cv:
            checks.append(("info", "client_version stale",
                           f"stored={stored_cv} live={live_cv}{gap}; run "
                           f"`notion-agent doctor --refresh-client-version`"))
        else:
            checks.append(("ok", "client_version current",
                           f"stored={stored_cv} (ahead of live {live_cv})"))

    print(_render_doctor(checks, as_json=args.json_out))
    return 0


# --------------------------------------------------------------------------- #
# models refresh subcommand
# --------------------------------------------------------------------------- #

def _add_models_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "models",
        help="Manage the local friendly-alias → Notion model id map.",
        description=(
            "Notion rotates internal model ids ('apricot-sorbet-high', "
            "etc.) every few months. `models refresh` pulls the current "
            "list from /api/v3/getAvailableModels and writes it to "
            f"{DEFAULT_USER_MODELS_PATH}, which `notion-agent chat` "
            "prefers over the hard-coded fallback in models.py."
        ),
    )
    msub = p.add_subparsers(dest="models_cmd", required=True)

    refresh = msub.add_parser(
        "refresh",
        help="Fetch the current model list from Notion and save the map.",
    )
    refresh.add_argument("--account", type=Path, default=DEFAULT_ACCOUNT_PATH,
                         help="Account file (default: %(default)s).")
    refresh.add_argument("--output", type=Path, default=None,
                         help=f"Output path (default: {DEFAULT_USER_MODELS_PATH}).")
    refresh.add_argument("--json", dest="json_out", action="store_true",
                         help="Print the resulting alias map as JSON to stdout.")
    refresh.set_defaults(func=_cmd_models_refresh)


async def _run_models_refresh(args: argparse.Namespace) -> dict[str, object]:
    async with NotionAgentClient(args.account) as client:
        return await client.fetch_available_models()


def _cmd_models_refresh(args: argparse.Namespace) -> int:
    try:
        raw = asyncio.run(_run_models_refresh(args))
    except NotionAgentError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    aliases = parse_available_models(raw)
    if not aliases:
        print("error: /getAvailableModels returned no usable models",
              file=sys.stderr)
        return 1

    out_path = save_user_model_map(aliases, args.output)
    if args.json_out:
        print(json.dumps(aliases, indent=2, ensure_ascii=False))
    else:
        print(f"[models refresh] wrote {out_path} ({len(aliases)} models)")
        for alias, mid in sorted(aliases.items()):
            print(f"  {alias:<28} -> {mid}")
    return 0


# --------------------------------------------------------------------------- #
# agents / threads subcommands — wrap /getCustomAgents
# --------------------------------------------------------------------------- #

def _add_agents_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "agents",
        help="List custom agents in the bound workspace.",
        description=(
            "Calls /api/v3/getCustomAgents and shows agent page ids "
            "ranked by activity, with the most recent thread title as a "
            "breadcrumb. Useful when you need to find the page id to "
            "pass to `init --agent-page-id` without copying it out of a "
            "Notion URL."
        ),
    )
    asub = p.add_subparsers(dest="agents_cmd", required=True)

    ls = asub.add_parser("list", help="Print one line per custom agent.")
    ls.add_argument("--account", type=Path, default=DEFAULT_ACCOUNT_PATH,
                    help="Account file (default: %(default)s).")
    ls.add_argument("--limit", type=int, default=20,
                    help="Max rows to print (default: %(default)s).")
    ls.add_argument("--json", dest="json_out", action="store_true",
                    help="Emit JSON list instead of the human table.")
    ls.add_argument(
        "--no-names", action="store_true",
        help=(
            "Skip the extra syncRecordValuesMain round-trip used to "
            "resolve agent name + agent_page_id. Use when you only "
            "need agent_ids and want one fewer HTTP request."
        ),
    )
    ls.set_defaults(func=_cmd_agents_list)

    route = asub.add_parser(
        "route",
        help="Pick the best-matching Custom Agent for a task description.",
        description=(
            "Best-effort keyword router: scores each Custom Agent by "
            "token overlap between the query and (name + description). "
            "Latin words plus single CJK characters are tokenized "
            "without an external segmenter dependency, so it handles "
            "mixed-language queries. For ambiguous cases, ask the "
            "bound agent directly — this is a hint, not an oracle."
        ),
    )
    route.add_argument("query", help="Task description to route, e.g. '处理邮件'.")
    route.add_argument("--account", type=Path, default=DEFAULT_ACCOUNT_PATH,
                       help="Account file (default: %(default)s).")
    route.add_argument("--limit", type=int, default=5,
                       help="Max rows to print (default: %(default)s).")
    route.add_argument("--json", dest="json_out", action="store_true",
                       help="Emit JSON {best_match, alternatives} instead of the table.")
    route.set_defaults(func=_cmd_agents_route)

    inspect = asub.add_parser(
        "inspect",
        help="Debug: raw syncRecordValuesMain lookup for one record id.",
        description=(
            "Calls /api/v3/syncRecordValuesMain with one pointer "
            "({table, id, spaceId}) and dumps the raw response. Useful "
            "when reverse-engineering which Notion table a UUID lives "
            "in — defaults to `workflow` since that's the table "
            "`agents list` queries, but the operator can probe `bot`, "
            "`block`, `notion_user`, `space`, `team`, etc. ACL-only "
            "responses ({\"role\":\"editor\"}) signal the id-space "
            "mismatch case (Notion returns a permission probe instead "
            "of the record)."
        ),
    )
    inspect.add_argument("record_id", help="UUID to look up.")
    inspect.add_argument(
        "--table", default="workflow",
        help="Notion record-table name (default: %(default)s).",
    )
    inspect.add_argument(
        "--no-space-id", action="store_true",
        help="Omit spaceId from the pointer (some tables reject it).",
    )
    inspect.add_argument("--account", type=Path, default=DEFAULT_ACCOUNT_PATH,
                         help="Account file (default: %(default)s).")
    inspect.set_defaults(func=_cmd_agents_inspect)


def _add_threads_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "threads",
        help="Inspect / manage chat threads in the bound workspace.",
        description=(
            "Wraps the read-side thread endpoints Notion's chat panel "
            "uses: list recent threads, count unread, mark one as read, "
            "or resolve a thread id to its workspace."
        ),
    )
    tsub = p.add_subparsers(dest="threads_cmd", required=True)

    ls = tsub.add_parser("list", help="Print one line per recent thread.")
    ls.add_argument("--account", type=Path, default=DEFAULT_ACCOUNT_PATH,
                    help="Account file (default: %(default)s).")
    ls.add_argument("--limit", type=int, default=20,
                    help="Max rows to print (default: %(default)s).")
    ls.add_argument("--agent", default=None,
                    help="Filter to threads under this agent (page id).")
    ls.add_argument("--json", dest="json_out", action="store_true",
                    help="Emit JSON list instead of the human table.")
    ls.set_defaults(func=_cmd_threads_list)

    uc = tsub.add_parser(
        "unread-count",
        help="Print the unread thread count for the bound workspace.",
        description=(
            "Calls /api/v3/getInferenceTranscriptsUnreadCount (Notion "
            "2026-05-19 addition). Returns one integer on stdout — "
            "easy to consume from shell (e.g. notifications)."
        ),
    )
    uc.add_argument("--account", type=Path, default=DEFAULT_ACCOUNT_PATH,
                    help="Account file (default: %(default)s).")
    uc.add_argument("--json", dest="json_out", action="store_true",
                    help="Emit JSON {count: int} instead of just the int.")
    uc.set_defaults(func=_cmd_threads_unread_count)

    mr = tsub.add_parser(
        "mark-read",
        help="Mark one thread as read (clears the unread badge).",
        description=(
            "Calls /api/v3/markInferenceTranscriptSeen (Notion 2026-05-19 "
            "addition). Useful for scripts that consume threads "
            "programmatically and want the workspace's unread counter to "
            "stay accurate."
        ),
    )
    mr.add_argument("thread_id", help="Thread UUID to mark as read.")
    mr.add_argument("--account", type=Path, default=DEFAULT_ACCOUNT_PATH,
                    help="Account file (default: %(default)s).")
    mr.set_defaults(func=_cmd_threads_mark_read)

    rs = tsub.add_parser(
        "resolve-space",
        help="Resolve a thread id to its workspace (spaceId).",
        description=(
            "Calls /api/v3/getThreadSpaceId (Notion 2026-05-19 addition). "
            "Useful when given a chat URL "
            "(https://www.notion.so/chat?t=<id>) without knowing which "
            "workspace owns the thread."
        ),
    )
    rs.add_argument("thread_id", help="Thread UUID to resolve.")
    rs.add_argument("--account", type=Path, default=DEFAULT_ACCOUNT_PATH,
                    help="Account file (default: %(default)s).")
    rs.add_argument("--json", dest="json_out", action="store_true",
                    help="Emit JSON {spaceId: <uuid>} instead of just the uuid.")
    rs.set_defaults(func=_cmd_threads_resolve_space)


async def _run_fetch_custom_agents(args: argparse.Namespace) -> dict[str, object]:
    async with NotionAgentClient(args.account) as client:
        return await client.fetch_custom_agents()


async def _run_sync_record(
    args: argparse.Namespace,
) -> dict[str, object]:
    async with NotionAgentClient(args.account) as client:
        return await client.sync_record(
            args.table, args.record_id,
            with_space_id=not args.no_space_id,
        )


def _cmd_agents_inspect(args: argparse.Namespace) -> int:
    try:
        raw = asyncio.run(_run_sync_record(args))
    except NotionAgentError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(raw, indent=2, ensure_ascii=False))
    return 0


# Tokenizer: latin word, decimal number, or single CJK ideograph. No
# third-party segmenter — CJK is matched character-by-character, which
# means "处理邮件" tokenizes to {处, 理, 邮, 件}. That's coarse, but it
# still beats substring-only because partial-character overlaps still
# score (e.g. "邮件" query hits "邮 件" in a description). Keeps the CLI
# dependency-free.
_ROUTE_TOKEN_RE = re.compile(r"[a-z0-9]+|[一-鿿]", re.IGNORECASE)


def _route_tokenize(text: str | None) -> set[str]:
    if not text:
        return set()
    return {m.lower() for m in _ROUTE_TOKEN_RE.findall(text)}


def _route_score(query_tokens: set[str], agent: AgentSummary) -> float:
    """Token-recall score with a small name-substring bonus.

    The base score is the recall of query tokens against the agent's
    (name + description) corpus: ``|q ∩ a| / |q|``. The bonus (+0.2)
    fires when *any* query token appears as a substring of the agent's
    name — this nudges exact-name matches above incidental description
    keywords so ``route "Email Agent"`` doesn't get out-ranked by an
    agent whose description happens to namedrop email.

    Agents without a description take a 0.5x penalty on the final
    score: an unauthored description is a weak signal, and a
    name-only match shouldn't out-rank a description-backed match
    just because the query happens to share a name keyword.
    """
    if not query_tokens:
        return 0.0
    haystack = " ".join(filter(None, [agent.name, agent.description]))
    a_tokens = _route_tokenize(haystack)
    if not a_tokens:
        return 0.0
    overlap = len(query_tokens & a_tokens) / len(query_tokens)
    name_lower = (agent.name or "").lower()
    name_bonus = 0.2 if name_lower and any(t in name_lower for t in query_tokens) else 0.0
    raw = overlap + name_bonus
    if not (agent.description or "").strip():
        raw *= 0.5
    return raw


def _cmd_agents_route(args: argparse.Namespace) -> int:
    try:
        raw = asyncio.run(_run_fetch_custom_agents(args))
    except NotionAgentError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    agent_ids = [x for x in (raw.get("agentIds") or []) if isinstance(x, str)]
    workflows = asyncio.run(_run_fetch_agent_workflows(args, agent_ids))
    agents = parse_agents(raw, workflows=workflows)
    if not agents:
        print("(no custom agents found in the bound workspace)")
        return 0

    query_tokens = _route_tokenize(args.query)
    scored = [(_route_score(query_tokens, a), a) for a in agents]
    # Sort by score desc, ties broken by recent activity (more active
    # agent wins) so identical-score candidates surface the one the
    # operator actually uses.
    scored.sort(key=lambda t: (-t[0], -(t[1].activity_score or 0), t[1].agent_id))
    nonzero = [(s, a) for s, a in scored if s > 0]
    visible = nonzero[: max(args.limit, 0)] if args.limit > 0 else nonzero

    if args.json_out:
        def _row(score: float, a: AgentSummary) -> dict[str, object]:
            return {
                "score":         round(score, 4),
                "agent_id":      a.agent_id,
                "name":          a.name,
                "icon":          a.icon,
                "agent_page_id": a.agent_page_id,
                "description":   a.description,
            }
        best = _row(*visible[0]) if visible else None
        alternatives = [_row(s, a) for s, a in visible[1:]]
        print(json.dumps(
            {"query": args.query, "best_match": best, "alternatives": alternatives},
            indent=2, ensure_ascii=False,
        ))
        return 0

    if not visible:
        print(
            f"(no agents matched {args.query!r} by keyword overlap — "
            "run `notion-agent agents list` to browse all agents)",
        )
        return 0
    print(f"{'score':<6} {'agent_id':<40} name")
    for score, a in visible:
        label = a.name or "(no name)"
        if a.icon:
            label = f"{a.icon} {label}"
        print(f"{score:<6.2f} {a.agent_id:<40} {label}")
    return 0


async def _run_fetch_agent_workflows(
    args: argparse.Namespace, agent_ids: list[str],
) -> dict[str, dict[str, object]]:
    """Batch-resolve agent_id → unwrapped workflow record via syncRecordValuesMain.

    Returns an empty dict on any failure — the caller falls back to
    name=None / agent_page_id=None per agent rather than aborting the
    listing. The lookup is best-effort: a workflow id with no record
    (404, deleted agent, etc.) simply omits that agent from the map.
    """
    if not agent_ids:
        return {}
    try:
        async with NotionAgentClient(args.account) as client:
            raw = await client.fetch_workflow_records(agent_ids)
    except NotionAgentError as e:
        log_msg = f"agent-workflow lookup failed ({e.code}); listing without metadata"
        print(f"warning: {log_msg}", file=sys.stderr)
        return {}
    return parse_workflow_records(raw)


def _format_ms_epoch(ms: int | None) -> str:
    if ms is None:
        return "-"
    from datetime import UTC, datetime
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")


def _cmd_agents_list(args: argparse.Namespace) -> int:
    try:
        raw = asyncio.run(_run_fetch_custom_agents(args))
    except NotionAgentError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    agent_ids = [x for x in (raw.get("agentIds") or []) if isinstance(x, str)]
    # Trim the workflow-lookup set to the agents we'll actually print so we
    # don't pay for resolving metadata on agents we're about to slice off.
    visible_ids = agent_ids if args.limit <= 0 else agent_ids[: max(args.limit * 2, args.limit)]
    workflows: dict[str, dict[str, object]] = {}
    if not args.no_names:
        workflows = asyncio.run(_run_fetch_agent_workflows(args, visible_ids))

    agents: list[AgentSummary] = parse_agents(raw, workflows=workflows)
    if args.limit > 0:
        agents = agents[: args.limit]

    if args.json_out:
        payload = [{
            "agent_id":                 a.agent_id,
            "name":                     a.name,
            "icon":                     a.icon,
            "agent_page_id":            a.agent_page_id,
            "description":              a.description,
            "activity_score":           a.activity_score,
            "most_recent_thread_id":    a.most_recent_thread_id,
            "most_recent_thread_title": a.most_recent_thread_title,
        } for a in agents]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    if not agents:
        print("(no custom agents found in the bound workspace)")
        return 0
    print(f"{'agent_id':<40} {'name':<24} {'agent_page_id':<40} last_active")
    for a in agents:
        last = _format_ms_epoch(a.activity_score)
        # Prefix the name with its icon when present so the table reads
        # at a glance — emojis double as a visual breadcrumb operators
        # use to recognize agents in the Notion sidebar.
        label = f"{a.icon} {a.name}" if a.icon and a.name else (a.name or "(no name)")
        label = label[:24]
        page = a.agent_page_id or "(unbound)"
        print(f"{a.agent_id:<40} {label:<24} {page:<40} {last}")
    return 0


def _cmd_threads_list(args: argparse.Namespace) -> int:
    try:
        raw = asyncio.run(_run_fetch_custom_agents(args))
    except NotionAgentError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    threads: list[ThreadSummary] = parse_threads(raw)
    if args.agent:
        threads = [t for t in threads if t.parent_agent_id == args.agent]
    if args.limit > 0:
        threads = threads[: args.limit]

    if args.json_out:
        payload = [{
            "thread_id":       t.thread_id,
            "title":           t.title,
            "parent_agent_id": t.parent_agent_id,
            "created_at_ms":   t.created_at_ms,
            "updated_at_ms":   t.updated_at_ms,
            "created_by_id":   t.created_by_id,
            "created_source":  t.created_source,
        } for t in threads]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    if not threads:
        print("(no recent threads)")
        return 0
    print(f"{'updated':<17} {'thread_id':<40} title")
    for t in threads:
        when = _format_ms_epoch(t.updated_at_ms or t.created_at_ms)
        title = t.title or "(no title)"
        print(f"{when:<17} {t.thread_id:<40} {title}")
    return 0


# --- 2026-05-19 additions ------------------------------------------------- #

async def _run_unread_count(args: argparse.Namespace) -> dict[str, object]:
    async with NotionAgentClient(args.account) as client:
        return await client.fetch_unread_transcript_count()


def _cmd_threads_unread_count(args: argparse.Namespace) -> int:
    try:
        raw = asyncio.run(_run_unread_count(args))
    except NotionAgentError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    count = raw.get("count")
    if args.json_out:
        print(json.dumps({"count": count}))
    else:
        print(count if count is not None else "")
    return 0


async def _run_mark_read(args: argparse.Namespace) -> dict[str, object]:
    async with NotionAgentClient(args.account) as client:
        return await client.mark_transcript_seen(args.thread_id)


def _cmd_threads_mark_read(args: argparse.Namespace) -> int:
    try:
        raw = asyncio.run(_run_mark_read(args))
    except NotionAgentError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print("ok" if raw.get("ok") else json.dumps(raw, ensure_ascii=False))
    return 0


async def _run_resolve_space(args: argparse.Namespace) -> dict[str, object]:
    async with NotionAgentClient(args.account) as client:
        return await client.fetch_thread_space_id(args.thread_id)


def _cmd_threads_resolve_space(args: argparse.Namespace) -> int:
    try:
        raw = asyncio.run(_run_resolve_space(args))
    except NotionAgentError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    space_id = raw.get("spaceId")
    if args.json_out:
        print(json.dumps({"spaceId": space_id}))
    else:
        print(space_id or "")
    return 0


# --------------------------------------------------------------------------- #
# runs subcommand — async Custom Agent runs (R5-A)
# --------------------------------------------------------------------------- #

def _add_runs_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "runs",
        help="Fire-and-forget Custom Agent runs + post-hoc inspection.",
        description=(
            "Wraps the workflow-mode side of Notion's "
            "/runInferenceTranscript endpoint plus the "
            "/getInferenceTranscriptsForWorkflow + "
            "/listPausedWorkflowRuns inspection endpoints captured "
            "in R5-A. Use `runs start` to kick off a long-running "
            "Custom Agent run without blocking the CLI; `runs list` "
            "to see what's been run / is running on a workflow; "
            "`runs paused` to surface runs Notion stopped for credit "
            "or run-limit reasons."
        ),
    )
    rsub = p.add_subparsers(dest="runs_cmd", required=True)

    start = rsub.add_parser(
        "start",
        help="POST runInferenceTranscript in workflow mode and detach.",
        description=(
            "Kicks off a workflow-mode run (the long-batch path real "
            "Custom Agents take in the chat panel), waits for "
            "Notion's 200, then closes the socket. Notion continues "
            "executing the run server-side. Prints the thread_id "
            "you can poll with `runs list` or follow up on later "
            "with `chat --thread-id`."
        ),
    )
    start.add_argument("prompt", nargs="?",
                       help="Prompt text. If omitted, read from stdin.")
    start.add_argument("--workflow", dest="workflow_id", required=True,
                       help="Workflow id (same as agent_id from `agents list`).")
    start.add_argument("--system", default=None,
                       help="Optional system-style preamble stitched into the prompt.")
    start.add_argument("--model", default=None,
                       help=("Friendly alias (opus-4.8 / sonnet-4.6 / haiku-4.5 / ...), "
                             "Anthropic id (claude-opus-4-8), or Notion internal id "
                             "(ambrosia-tart-high). Defaults to account.default_model "
                             "(opus-4.8). Run `notion-agent models refresh` to sync "
                             "Notion's latest id rotation."))
    start.add_argument("--ask-mode", action="store_true",
                       help="useReadOnlyMode=true — model answers but skips page edits.")
    start.add_argument("--no-web-search", action="store_true",
                       help="Disable the built-in web search tool.")
    start.add_argument("--no-workspace-search", action="store_true",
                       help="Disable workspace search.")
    start.add_argument("--json", dest="json_out", action="store_true",
                       help="Emit a JSON {thread_id, workflow_id} record to stdout.")
    start.add_argument("--stream", action="store_true",
                       help="Keep the socket open and print text chunks as "
                            "they stream in (LLM-style). Disables detach.")
    start.add_argument("--ndjson", action="store_true",
                       help="Keep the socket open and pipe raw NDJSON lines "
                            "straight to stdout. Disables detach. Mutually "
                            "exclusive with --stream/--json.")
    start.add_argument("--account", type=Path, default=DEFAULT_ACCOUNT_PATH,
                       help="Account file (default: %(default)s).")
    start.set_defaults(func=_cmd_runs_start)

    ls = rsub.add_parser(
        "list",
        help="List recent runs on a workflow (newest first).",
        description=(
            "Wraps /getInferenceTranscriptsForWorkflow. Each row "
            "carries cumulative usage_summary so operators can spot "
            "in-flight runs by comparing last_updated to wall clock. "
            "Pass `--stale-after <sec>` to flag runs whose "
            "last_updated_time is within that window — heuristic, "
            "since the endpoint has no native status field."
        ),
    )
    ls.add_argument("--workflow", dest="workflow_id", required=True,
                    help="Workflow id to list runs for.")
    ls.add_argument("--limit", type=int, default=10,
                    help="Max rows to fetch / print (default: %(default)s).")
    ls.add_argument("--user", dest="user_only", action="store_true",
                    help="Scope to runs created by the bound account's user.")
    ls.add_argument("--stale-after", type=int, default=90,
                    help="Seconds since last_updated under which a run is "
                         "flagged as 'in_progress' (default: %(default)s). "
                         "Heuristic only.")
    ls.add_argument("--json", dest="json_out", action="store_true",
                    help="Emit JSON list instead of the human table.")
    ls.add_argument("--account", type=Path, default=DEFAULT_ACCOUNT_PATH,
                    help="Account file (default: %(default)s).")
    ls.set_defaults(func=_cmd_runs_list)

    paused = rsub.add_parser(
        "paused",
        help="Print listPausedWorkflowRuns raw response.",
        description=(
            "Wraps /listPausedWorkflowRuns. R5-A captured only the "
            "request shape (the operator's workspace had no paused "
            "runs at the time) so the response is surfaced as raw "
            "JSON until we have a captured shape to parse."
        ),
    )
    paused.add_argument("--workflow", dest="workflow_id", required=True,
                        help="Workflow id to query.")
    paused.add_argument("--count-only", action="store_true",
                        help="Pass countOnly:true (server returns just the count).")
    paused.add_argument("--account", type=Path, default=DEFAULT_ACCOUNT_PATH,
                        help="Account file (default: %(default)s).")
    paused.set_defaults(func=_cmd_runs_paused)


def _runs_read_prompt(args: argparse.Namespace) -> str:
    if args.prompt is not None:
        return args.prompt
    if sys.stdin.isatty():
        print("error: no prompt given (pass a positional arg or pipe stdin)",
              file=sys.stderr)
        sys.exit(2)
    return sys.stdin.read()


async def _run_runs_start(args: argparse.Namespace, prompt: str) -> str:
    await _maybe_refresh_session(args.account)
    async with NotionAgentClient(args.account) as client:
        return await client.start_run_detached(
            prompt=prompt,
            workflow_id=args.workflow_id,
            system=args.system,
            model=args.model,
            web_search=not args.no_web_search,
            workspace_search=not args.no_workspace_search,
            ask_mode=args.ask_mode,
        )


async def _run_runs_start_attached(args: argparse.Namespace, prompt: str) -> str:
    """Synchronous workflow-mode run: keep socket open, print events live.

    Used when the operator passes ``--stream`` or ``--ndjson`` to
    ``runs start``. Threads the same ``workflow_id`` through
    :meth:`complete` / :meth:`stream_lines`, so the request body is
    identical to the detached path — only the post-200 read loop
    differs.
    """
    await _maybe_refresh_session(args.account)
    async with NotionAgentClient(args.account) as client:
        if args.ndjson:
            last_thread_id = ""
            async for line in client.stream_lines(
                prompt=prompt,
                workflow_id=args.workflow_id,
                system=args.system,
                model=args.model,
                web_search=not args.no_web_search,
                workspace_search=not args.no_workspace_search,
                ask_mode=args.ask_mode,
            ):
                print(line, flush=True)
                # Best-effort thread_id capture from the first event
                # that names one. NDJSON-mode consumers usually parse
                # themselves; we surface it on stderr for convenience.
                if not last_thread_id and '"threadId"' in line:
                    try:
                        obj = json.loads(line)
                        tid = obj.get("threadId") if isinstance(obj, dict) else None
                        if isinstance(tid, str):
                            last_thread_id = tid
                    except json.JSONDecodeError:
                        pass
            return last_thread_id

        def on_delta(chunk: str) -> None:
            print(chunk, end="", flush=True)

        resp = await client.complete(
            prompt=prompt,
            workflow_id=args.workflow_id,
            system=args.system,
            model=args.model,
            web_search=not args.no_web_search,
            workspace_search=not args.no_workspace_search,
            ask_mode=args.ask_mode,
            on_text_delta=on_delta,
        )
        print()  # terminate the streamed line
        return resp.thread_id


def _cmd_runs_start(args: argparse.Namespace) -> int:
    if args.ndjson and (args.stream or args.json_out):
        print("error: --ndjson is mutually exclusive with --stream / --json",
              file=sys.stderr)
        return 2
    prompt = _runs_read_prompt(args)
    try:
        if args.stream or args.ndjson:
            thread_id = asyncio.run(_run_runs_start_attached(args, prompt))
        else:
            thread_id = asyncio.run(_run_runs_start(args, prompt))
    except NotionAgentError as e:
        return _emit_error(e, json_out=args.json_out)

    if args.stream or args.ndjson:
        # Streamed output already on stdout; surface thread_id on stderr
        # so a `--stream | tee` pipeline doesn't muddy the captured body.
        print(
            f"[runs start] thread_id={thread_id or '(not captured)'}  "
            f"workflow={args.workflow_id}",
            file=sys.stderr,
        )
        return 0

    if args.json_out:
        print(json.dumps(
            {"thread_id": thread_id, "workflow_id": args.workflow_id},
            ensure_ascii=False,
        ))
    else:
        print(f"[runs start] workflow={args.workflow_id}")
        print(f"[runs start] thread_id={thread_id}")
        print("[runs start] run is in flight server-side; "
              "poll with `notion-agent runs list --workflow "
              f"{args.workflow_id}`.")
    return 0


async def _run_runs_list(args: argparse.Namespace) -> dict[str, object]:
    async with NotionAgentClient(args.account) as client:
        acc = client.load_account()
        return await client.fetch_workflow_transcripts(
            args.workflow_id,
            limit=args.limit,
            user_id=acc.user_id if args.user_only else None,
        )


def _runs_in_progress(t: TranscriptRun, *, stale_after_ms: int, now_ms: int) -> bool:
    """Heuristic: last_updated_time is within the stale window.

    R5-A § "对 CLI 实现的判断": the transcripts endpoint has no
    native status field, so operators compare ``last_updated_time``
    to wall clock. Within the window → likely still running. Outside
    → likely done (or hung — operators decide).
    """
    last = t.last_updated_time_ms or t.updated_at_ms or t.created_at_ms or 0
    if last <= 0:
        return False
    return (now_ms - last) <= stale_after_ms


def _cmd_runs_list(args: argparse.Namespace) -> int:
    try:
        raw = asyncio.run(_run_runs_list(args))
    except NotionAgentError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    rows = parse_workflow_transcripts(raw)
    now_ms = int(time.time() * 1000)
    stale_after_ms = max(args.stale_after, 0) * 1000

    if args.json_out:
        payload = [{
            "thread_id":               t.thread_id,
            "title":                   t.title,
            "created_at_ms":           t.created_at_ms,
            "updated_at_ms":           t.updated_at_ms,
            "last_updated_time_ms":    t.last_updated_time_ms,
            "completion_count":        t.completion_count,
            "agent_inference_count":   t.agent_inference_count,
            "spend_usd":               t.spend_usd,
            "input_tokens":            t.input_tokens,
            "output_tokens":           t.output_tokens,
            "created_by_display_name": t.created_by_display_name,
            "type":                    t.type,
            "in_progress":             _runs_in_progress(
                t, stale_after_ms=stale_after_ms, now_ms=now_ms,
            ),
        } for t in rows]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    if not rows:
        print(f"(no runs found for workflow {args.workflow_id})")
        return 0
    print(f"{'last_updated':<17} {'state':<11} {'thread_id':<40} title")
    for t in rows:
        last = t.last_updated_time_ms or t.updated_at_ms or t.created_at_ms
        when = _format_ms_epoch(last)
        in_flight = _runs_in_progress(t, stale_after_ms=stale_after_ms, now_ms=now_ms)
        state = "in_progress" if in_flight else "completed?"
        title = t.title or "(no title)"
        print(f"{when:<17} {state:<11} {t.thread_id:<40} {title}")
    return 0


async def _run_runs_paused(args: argparse.Namespace) -> dict[str, object]:
    async with NotionAgentClient(args.account) as client:
        return await client.fetch_paused_workflow_runs(
            args.workflow_id, count_only=args.count_only,
        )


def _cmd_runs_paused(args: argparse.Namespace) -> int:
    try:
        raw = asyncio.run(_run_runs_paused(args))
    except NotionAgentError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(raw, indent=2, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------- #
# profile subcommand — multi-workspace symlink swap
# --------------------------------------------------------------------------- #

def _add_profile_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "profile",
        help="Switch between named ~/.notionagents/<name>.json credentials.",
        description=(
            "Manage multi-workspace credentials. Each profile is a "
            "separate notion_account.json saved under ~/.notionagents/ "
            "with a custom name (e.g. tplink.json, personal.json). The "
            "active credential is a symlink at notion_account.json — "
            "`profile use <name>` retargets it."
        ),
    )
    psub = p.add_subparsers(dest="profile_cmd", required=True)

    ls = psub.add_parser("list", help="Print available profiles, * marks active.")
    ls.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR,
                    help="Profile directory (default: %(default)s).")
    ls.set_defaults(func=_cmd_profile_list)

    cur = psub.add_parser("current", help="Print the currently-active profile name.")
    cur.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR,
                     help="Profile directory (default: %(default)s).")
    cur.set_defaults(func=_cmd_profile_current)

    use = psub.add_parser("use", help="Retarget notion_account.json at <name>.json.")
    use.add_argument("name", help="Profile name (without the .json extension).")
    use.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR,
                     help="Profile directory (default: %(default)s).")
    use.set_defaults(func=_cmd_profile_use)

    mig = psub.add_parser(
        "migrate",
        help="Promote an existing notion_account.json into a named profile.",
    )
    mig.add_argument("name", help="Profile name to create (without .json).")
    mig.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR,
                     help="Profile directory (default: %(default)s).")
    mig.set_defaults(func=_cmd_profile_migrate)


def _cmd_profile_list(args: argparse.Namespace) -> int:
    profiles = list_profiles(args.profile_dir)
    if not profiles:
        print(f"(no profiles in {args.profile_dir})")
        return 0
    for entry in profiles:
        marker = "* " if entry.is_active else "  "
        print(f"{marker}{entry.name}")
    return 0


def _cmd_profile_current(args: argparse.Namespace) -> int:
    entry = current_profile(args.profile_dir)
    if entry is None:
        print("(no active profile — notion_account.json is not a symlink "
              "or no named profile matches it)", file=sys.stderr)
        return 1
    print(entry.name)
    return 0


def _cmd_profile_use(args: argparse.Namespace) -> int:
    try:
        link = use_profile(args.name, args.profile_dir)
    except NotionAgentError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"[profile use] {link} -> {args.name}.json")
    return 0


def _cmd_profile_migrate(args: argparse.Namespace) -> int:
    try:
        link = migrate_account_to_profile(args.name, args.profile_dir)
    except NotionAgentError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"[profile migrate] notion_account.json -> {args.name}.json")
    print(f"[profile migrate] symlink at {link}")
    return 0


# --------------------------------------------------------------------------- #
# serve subcommand — FastAPI wrapper
# --------------------------------------------------------------------------- #

def _add_serve_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "serve",
        help="Run a local FastAPI server exposing /chat /agents /threads /healthz.",
        description=(
            "Start a uvicorn-hosted FastAPI app wrapping this CLI. Useful "
            "when an orchestrator (n8n, Make, a Go service) prefers HTTP "
            "over shell-out. Requires the [serve] extra: "
            "`pip install 'notion-agent-cli[serve]'`."
        ),
    )
    p.add_argument("--host", default="127.0.0.1",
                   help="Bind address (default: %(default)s — localhost only).")
    p.add_argument("--port", type=int, default=8000,
                   help="Bind port (default: %(default)s).")
    p.add_argument("--account", type=Path, default=DEFAULT_ACCOUNT_PATH,
                   help="Account file (default: %(default)s).")
    p.add_argument("--reload", action="store_true",
                   help="Dev-only: uvicorn auto-reload on code changes.")
    p.set_defaults(func=_cmd_serve)


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn

        from notion_agent_cli.serve import create_app
    except ImportError:
        print(
            "error: `notion-agent serve` requires the [serve] extra. "
            "Run: pip install 'notion-agent-cli[serve]'",
            file=sys.stderr,
        )
        return 2

    if args.reload:
        # uvicorn --reload needs an import string, not an app instance.
        # We expose a factory at module level for that path.
        import os
        os.environ["NOTION_AGENT_CLI_ACCOUNT"] = str(args.account.expanduser())
        uvicorn.run(
            "notion_agent_cli.serve:_reload_app",
            host=args.host, port=args.port, reload=True, factory=True,
        )
    else:
        app = create_app(args.account)
        uvicorn.run(app, host=args.host, port=args.port)
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="notion-agent",
        description=(
            "Direct CLI for Notion's ✦ AI / Custom Agent endpoint.\n"
            "\n"
            "Two usage modes:\n"
            "  • Short / synchronous  →  `chat [--thread-id <tid>] \"<prompt>\"`\n"
            "      Blocks until Notion finishes the turn. Use for replies\n"
            "      that come back in seconds. `--thread-id` continues an\n"
            "      earlier chat thread (same sync path).\n"
            "  • Long / async (detach) →  `runs start --workflow <wid> \"<prompt>\"`\n"
            "      Posts the run, returns the thread_id immediately, and\n"
            "      lets Notion finish server-side. Poll with `runs list`\n"
            "      or follow up with `chat --thread-id <tid>` once done.\n"
            "\n"
            "Pick `runs start` over `chat` for any task expected to take\n"
            "more than ~60s — holding the chat socket open that long is\n"
            "what makes long Custom Agent calls look 'stuck'."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)
    _add_init_parser(sub)
    _add_chat_parser(sub)
    _add_doctor_parser(sub)
    _add_models_parser(sub)
    _add_agents_parser(sub)
    _add_threads_parser(sub)
    _add_runs_parser(sub)
    _add_profile_parser(sub)
    _add_serve_parser(sub)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
