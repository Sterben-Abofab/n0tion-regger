"""Parse ``/api/v3/getCustomAgents`` into agent + thread summaries.

A single round-trip to ``getCustomAgents`` answers both "what custom
agents do I have here?" and "what threads have I run recently?" —
:func:`parse_agents` / :func:`parse_threads` split the response into
two ranked lists the ``agents list`` / ``threads list`` CLI
subcommands print.

The endpoint does **not** return agent metadata (name, icon, page
binding). To resolve names + the ``--agent-page-id`` operators need,
we make a second batched call to ``/api/v3/syncRecordValuesMain``
with ``table=workflow`` — Notion's ``agentIds`` are actually
**workflow IDs**, and each workflow record carries
``data.{name, icon, description, instructions: {id}}`` plus
``data.runtime_actor_pointer.id`` (the bot record id). The
``instructions.id`` is the persistent_instructions_page that the
chat-panel binding (``--agent-page-id``) requires.

History
- v0.1.3: spec-only impl using ``table=block`` on raw agent_ids.
  Never returned a hit on real workspaces.
- v0.1.4: operator-captured wire shape, switched to ``table=bot``.
  Looked right against a sanitized fixture but **still failed in
  prod** — querying agent_ids against ``table=bot`` returns only
  ``{role:"editor"}`` ACL probes, because agent_ids are NOT bot
  record ids.
- v0.1.5 (this): full ``getInferenceTranscriptsForWorkflow`` response
  capture from the operator finally exposed ``recordMap.workflow.<id>``
  with the agent metadata in ``data``. agent_id == workflow_id ⇒
  ``syncRecordValuesMain table=workflow`` is the correct lookup.

The CLI runs this lookup lazily inside ``agents list`` and tolerates
failure: an unresolved agent shows up with ``name=None`` /
``agent_page_id=None`` rather than blocking the listing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True, frozen=True)
class ThreadSummary:
    """One entry from ``mostRecentTranscripts``."""
    thread_id:        str
    title:            str | None
    parent_agent_id:  str | None
    created_at_ms:    int | None
    updated_at_ms:    int | None
    created_by_id:    str | None
    created_source:   str | None


@dataclass(slots=True, frozen=True)
class AgentSummary:
    """One custom agent + its most recent activity breadcrumb.

    Fields resolved from ``recordMap.workflow.<agent_id>.value.value.data``
    via :func:`parse_workflow_records`:

    - ``name`` — agent display name (e.g. ``"Jarvis"``)
    - ``icon`` — emoji or image URL
    - ``description`` — operator-authored description
    - ``agent_page_id`` — the ``instructions.id`` UUID, **exactly the
      value to pass to** ``init --agent-page-id``

    All four default to ``None`` when the workflow lookup is skipped
    (``parse_agents`` without ``workflows=``) or fails for an agent.
    """
    agent_id:                  str
    activity_score:            int | None  # epoch ms, None if no activity recorded
    most_recent_thread_id:     str | None
    most_recent_thread_title:  str | None
    name:                      str | None = None
    icon:                      str | None = None
    description:               str | None = None
    agent_page_id:             str | None = None


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_threads(response: dict[str, Any]) -> list[ThreadSummary]:
    """Pull ``mostRecentTranscripts`` into a list sorted updated→created→id desc.

    Missing fields land as ``None`` rather than raising — Notion returns
    nulls for transcripts that never got past creation.
    """
    out: list[ThreadSummary] = []
    for entry in response.get("mostRecentTranscripts") or []:
        if not isinstance(entry, dict):
            continue
        tid = entry.get("id")
        if not isinstance(tid, str):
            continue
        out.append(ThreadSummary(
            thread_id=       tid,
            title=           entry.get("title") if isinstance(entry.get("title"), str) else None,
            parent_agent_id= entry.get("parent_id") if isinstance(entry.get("parent_id"), str) else None,
            created_at_ms=   _to_int(entry.get("created_time")),
            updated_at_ms=   _to_int(entry.get("updated_time")),
            created_by_id=   entry.get("created_by_id") if isinstance(entry.get("created_by_id"), str) else None,
            created_source=  entry.get("created_source") if isinstance(entry.get("created_source"), str) else None,
        ))
    # Sort newest-first by max(updated, created) so transcripts that
    # never got an updated_at still slot in chronologically by their
    # creation time — matches what the CLI prints in the timestamp
    # column. Tiebreak on thread_id for stable output.
    def _sort_key(t: ThreadSummary) -> tuple[int, str]:
        newest = max(t.updated_at_ms or 0, t.created_at_ms or 0)
        return (-newest, t.thread_id)
    out.sort(key=_sort_key)
    return out


def parse_agents(
    response: dict[str, Any],
    *,
    workflows: dict[str, dict[str, Any]] | None = None,
) -> list[AgentSummary]:
    """Combine ``agentIds`` + ``activityScores`` + most-recent thread title.

    Sorted by activity_score desc (most-recently-used first). Agents
    with no recorded activity sink to the bottom with ``activity_score
    = None``.

    ``workflows`` (optional) maps ``agent_id → unwrapped workflow value
    dict`` (the inner ``recordMap.workflow.<id>.value.value`` payload).
    When provided, :class:`AgentSummary`'s ``name`` / ``icon`` /
    ``description`` / ``agent_page_id`` are populated via
    :func:`extract_workflow_name` / ``…_icon`` / ``…_description`` /
    ``…_instructions_page_id``. Missing keys are silently treated as
    unresolved (all four fields stay ``None``).
    """
    workflows = workflows or {}
    agent_ids: list[str] = [
        x for x in (response.get("agentIds") or []) if isinstance(x, str)
    ]

    activity: dict[str, int] = {}
    for entry in response.get("activityScores") or []:
        if not isinstance(entry, dict):
            continue
        pid = entry.get("parent_id")
        score = _to_int(entry.get("activity_score"))
        if (
            isinstance(pid, str)
            and score is not None
            and (pid not in activity or activity[pid] < score)
        ):
            # If duplicates, keep the highest score (= most recent activity).
            activity[pid] = score

    # Best-effort breadcrumb: agent's most recent thread (title + id).
    threads = parse_threads(response)  # already sorted newest-first
    recent_by_agent: dict[str, ThreadSummary] = {}
    for t in threads:
        if t.parent_agent_id and t.parent_agent_id not in recent_by_agent:
            recent_by_agent[t.parent_agent_id] = t

    out: list[AgentSummary] = []
    for aid in agent_ids:
        recent = recent_by_agent.get(aid)
        wf = workflows.get(aid) or {}
        out.append(AgentSummary(
            agent_id=                aid,
            activity_score=          activity.get(aid),
            most_recent_thread_id=   recent.thread_id if recent else None,
            most_recent_thread_title=recent.title if recent else None,
            name=                    extract_workflow_name(wf),
            icon=                    extract_workflow_icon(wf),
            description=             extract_workflow_description(wf),
            agent_page_id=           extract_workflow_instructions_page_id(wf),
        ))

    def _sort_key(a: AgentSummary) -> tuple[int, str]:
        # None scores sink to the bottom; tiebreak on agent_id for stable order.
        return (-(a.activity_score or 0), a.agent_id)
    out.sort(key=_sort_key)
    return out


# --------------------------------------------------------------------------- #
# Workflow-record extraction (used to resolve agent name + page binding)
# --------------------------------------------------------------------------- #

def _workflow_data(workflow_value: dict[str, Any]) -> dict[str, Any] | None:
    """Return the inner ``data`` dict or ``None``.

    Custom Agents store their user-visible metadata under
    ``workflow.value.value.data`` — that's where ``name`` / ``icon`` /
    ``description`` / ``instructions`` live. Anything outside ``data``
    is bookkeeping (created_by, version, alive, …).
    """
    if not isinstance(workflow_value, dict):
        return None
    data = workflow_value.get("data")
    return data if isinstance(data, dict) else None


def extract_workflow_name(workflow_value: dict[str, Any]) -> str | None:
    """Return ``workflow.data.name`` trimmed, or ``None`` if missing."""
    data = _workflow_data(workflow_value)
    if data is None:
        return None
    name = data.get("name")
    if not isinstance(name, str):
        return None
    out = name.strip()
    return out or None


def extract_workflow_icon(workflow_value: dict[str, Any]) -> str | None:
    """Return ``workflow.data.icon`` (emoji or URL), trimmed, or ``None``."""
    data = _workflow_data(workflow_value)
    if data is None:
        return None
    icon = data.get("icon")
    if not isinstance(icon, str):
        return None
    out = icon.strip()
    return out or None


def extract_workflow_description(workflow_value: dict[str, Any]) -> str | None:
    """Return ``workflow.data.description``, trimmed, or ``None``."""
    data = _workflow_data(workflow_value)
    if data is None:
        return None
    desc = data.get("description")
    if not isinstance(desc, str):
        return None
    out = desc.strip()
    return out or None


def extract_workflow_instructions_page_id(workflow_value: dict[str, Any]) -> str | None:
    """Return ``workflow.data.instructions.id`` — the page UUID a chat
    binding needs.

    This is the value operators copy into ``init --agent-page-id``: the
    persistent_instructions_page where the agent's prompt lives.
    Returns ``None`` if the agent's workflow record lacks an
    ``instructions`` pointer (e.g. agent types that bind by other means).
    """
    data = _workflow_data(workflow_value)
    if data is None:
        return None
    inst = data.get("instructions")
    if not isinstance(inst, dict):
        return None
    pid = inst.get("id")
    return pid if isinstance(pid, str) and pid else None


def parse_workflow_records(
    sync_response: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Unwrap ``syncRecordValuesMain``'s response into ``{workflow_id: value}``.

    The wire shape is ``recordMap.workflow.<id> = {value: {value: {...}}}``
    (the same double-nested role/value envelope ``bot`` / ``block`` use).
    Single-nested fallback kept defensively. Returns ``{}`` when the
    response lacks ``recordMap.workflow``, which is Notion's signature
    for "no records matched".
    """
    out: dict[str, dict[str, Any]] = {}
    rm = sync_response.get("recordMap") if isinstance(sync_response, dict) else None
    if not isinstance(rm, dict):
        return out
    workflows = rm.get("workflow")
    if not isinstance(workflows, dict):
        return out
    for wid, record in workflows.items():
        if not isinstance(record, dict):
            continue
        val = record.get("value")
        if isinstance(val, dict):
            inner = val.get("value")
            if isinstance(inner, dict):
                out[wid] = inner
            else:
                out[wid] = val
    return out
