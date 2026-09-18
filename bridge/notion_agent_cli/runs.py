"""Parse Notion's async-run inspection endpoints.

R5-A (docs/r5a-capture/R5-A-REPORT.md) confirmed there is no native
``runStart`` / ``runStatus`` endpoint — Notion runs Custom Agents
inside ``runInferenceTranscript``'s NDJSON long-stream and exposes
post-hoc visibility via three companion endpoints:

- ``/api/v3/getInferenceTranscriptsForWorkflow`` — per-workflow
  history list (one entry per thread, with cumulative
  ``usage_summary`` and a ``last_updated_time`` operators can compare
  to wall clock to guess "still running"). The 0.1.7 ``runs list``
  command wraps this.
- ``/api/v3/getInferenceTranscriptsForUser`` — same shape, scoped to
  one user across all workflows.
- ``/api/v3/listPausedWorkflowRuns`` — runs Notion paused for
  credit / run-limit reasons. The response shape wasn't captured in
  R5-A (the operator's workspace had no paused runs at the time),
  so the wrapper surfaces the raw response and the CLI prints it as
  JSON until we have a real fixture.

The transcripts list schema has NO ``status`` / ``in_progress``
field — staleness is the operator's best heuristic. We expose the
raw ``last_updated_time_ms`` and let the CLI decide where the
in-progress / completed boundary sits.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True, frozen=True)
class TranscriptRun:
    """One entry from ``getInferenceTranscriptsForWorkflow.transcripts``.

    Mirrors the operator-captured wire shape (R5-A § getInferenceTranscriptsForWorkflow):

    - ``thread_id``               — transcript / thread UUID (also the
      ``threadId`` to pass to ``--thread-id`` for continuation).
    - ``title``                   — assistant-generated thread title; ``None``
      while the workflow hasn't produced one yet (fresh trigger runs).
    - ``created_at_ms`` /
      ``updated_at_ms``           — Notion's thread-level epoch-ms timestamps.
    - ``last_updated_time_ms``    — inner ``usage_summary.last_updated_time``;
      ticks per inference call, so it's the freshest "still running"
      breadcrumb operators have.
    - ``completion_count`` /
      ``agent_inference_count``   — counters off ``usage_summary``.
    - ``spend_usd``               — cumulative dollar spend on the run.
    - ``created_by_display_name`` — human or bot label for the run
      initiator (Notion uses the bot's name for trigger-fired runs).
    - ``type``                    — ``"workflow"`` for Custom Agent
      threads; the field exists for forward-compat with other surfaces.
    """
    thread_id:               str
    title:                   str | None
    created_at_ms:           int | None
    updated_at_ms:           int | None
    created_by_display_name: str | None
    type:                    str | None
    spend_usd:               float | None
    completion_count:        int | None
    agent_inference_count:   int | None
    last_updated_time_ms:    int | None
    input_tokens:            int | None
    output_tokens:           int | None


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def parse_workflow_transcripts(response: dict[str, Any]) -> list[TranscriptRun]:
    """Pull ``transcripts`` into a list sorted newest-first.

    ``getInferenceTranscriptsForWorkflow`` returns ``transcripts``
    (the rich list with ``usage_summary``) alongside ``threadIds``
    (the same ids, flat). We parse the rich list — operators who
    only need ids can read ``threadIds`` off the raw response.

    Sort key prefers ``last_updated_time`` (the freshest "still
    moving" signal) and falls back to ``updated_at`` / ``created_at``
    for entries Notion hasn't stamped yet. Tiebreak on ``thread_id``
    for stable output.

    Entries with no ``id`` or non-dict payloads are skipped — the
    endpoint occasionally returns transcripts with no inner
    ``usage_summary`` for trigger runs that haven't started
    inferencing, and we don't want one weird row to blow up the
    whole listing.
    """
    out: list[TranscriptRun] = []
    for entry in response.get("transcripts") or []:
        if not isinstance(entry, dict):
            continue
        tid = entry.get("id")
        if not isinstance(tid, str):
            continue
        usage = entry.get("usage_summary") or {}
        if not isinstance(usage, dict):
            usage = {}
        out.append(TranscriptRun(
            thread_id=               tid,
            title=                   _opt_str(entry.get("title")),
            created_at_ms=           _to_int(entry.get("created_at")),
            updated_at_ms=           _to_int(entry.get("updated_at")),
            created_by_display_name= _opt_str(entry.get("created_by_display_name")),
            type=                    _opt_str(entry.get("type")),
            spend_usd=               _to_float(usage.get("spend_usd")),
            completion_count=        _to_int(usage.get("completion_count")),
            agent_inference_count=   _to_int(usage.get("agent_inference_count")),
            last_updated_time_ms=    _to_int(usage.get("last_updated_time")),
            input_tokens=            _to_int(usage.get("input_tokens")),
            output_tokens=           _to_int(usage.get("output_tokens")),
        ))

    def _sort_key(t: TranscriptRun) -> tuple[int, str]:
        newest = max(
            t.last_updated_time_ms or 0,
            t.updated_at_ms or 0,
            t.created_at_ms or 0,
        )
        return (-newest, t.thread_id)

    out.sort(key=_sort_key)
    return out
