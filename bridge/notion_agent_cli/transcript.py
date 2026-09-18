"""Build the ``/api/v3/runInferenceTranscript`` request body.

Each builder mirrors a chunk of the captured chat-panel payload
documented in ``docs/01-notion-chat-protocol.md §1``.

We intentionally send the full ~50-field config block (vs the ~12
notion_manager Go emits) so threads behave identically to the chat
panel: Notion treats unknown flags as no-ops, but a missing flag
occasionally changes UI affordance. Easier to over-send than to debug
"why doesn't my thread show the Jarvis avatar".
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from notion_agent_cli.account import NotionAccount


def _now_iso(tz: str | None = None) -> str:
    """ISO 8601 with millisecond precision + TZ offset, matching Notion's
    ``currentDatetime``. Example: ``2026-05-15T22:17:16.187-07:00``.

    ``tz`` is an IANA name (e.g. ``America/Los_Angeles``). When set, the
    rendered offset matches that zone — important because Notion stamps
    ``currentDatetime`` against the operator's declared workspace
    timezone, not the host's local tz; mismatched offsets occasionally
    surface as wrong-day output in the model's reply. Passing ``None``
    (or an unknown zone) falls back to the system local tz.
    """
    target: ZoneInfo | None = None
    if tz:
        try:
            target = ZoneInfo(tz)
        except ZoneInfoNotFoundError:
            target = None
    return datetime.now(UTC).astimezone(target).isoformat(timespec="milliseconds")


def new_uuid() -> str:
    return str(uuid.uuid4())


# --------------------------------------------------------------------------- #
# Config block (transcript[0])
# --------------------------------------------------------------------------- #

def build_config_value(
    *,
    notion_model: str,
    is_subsequent_turn: bool = False,
    use_web_search: bool = True,
    use_workspace_search: bool = True,
    use_read_only_mode: bool = False,
) -> dict[str, Any]:
    """Construct the ``config`` transcript entry's ``value`` field."""
    cfg: dict[str, Any] = {
        "type": "workflow",
        "modelFromUser": not is_subsequent_turn,

        "enableAgentAutomations":         True,
        "enableAgentIntegrations":        True,
        "enableCustomAgents":             True,
        "enableExperimentalIntegrations": False,
        "enableAgentDiffs":               True,
        "enableAgentUpdatePagePatch":     True,
        "enableCsvAttachmentSupport":     True,
        "enableDatabaseAgents":           True,
        "showDatabaseAgentsDiscoverability": True,
        "enableAgentThreadTools":         False,
        "enableCrdtOperations":           False,
        "enableAgentCardCustomization":   True,
        "enableSystemPromptAsPage":       False,
        "enableUserSessionContext":       False,
        "enableLargeToolResultComputerOffload": False,

        "enableScriptAgentAdvanced":      False,
        "enableScriptAgent":              True,
        "enableScriptAgentSearchConnectorsInCustomAgent": False,
        "enableScriptAgentGoogleDriveInCustomAgent":      False,
        "enableScriptAgentGoogleDriveOAuthInCustomAgent": False,
        "enableScriptAgentSlack":         True,
        "enableScriptAgentMcpServers":    False,
        "enableScriptAgentMail":          True,
        # Added by Notion 2026-05-19; cap harness saw it absent before, present after.
        "enableScriptAgentGtm":           False,
        "enableScriptAgentCustomToolCalling": True,

        "enableComputer":                 False,
        "enableCreateAndRunThread":       True,
        "enableSoftwareFactoryPage":      False,
        "enableAgentGenerateImage":       True,
        "enableSpeculativeSearch":        False,
        "enableQueryCalendar":            False,
        "enableQueryMail":                False,
        "enableMailExplicitToolCalls":    True,
        "enableMailNotificationPreferences": False,
        "enableMailAgentMultiProviderSupport": False,
        "useRulePrioritization":          True,
        # Workspace-state-dependent: 2026-05-19 cap on AI Home (ai_module
        # surface) sent ``[]`` even when the workspace had calendar
        # connected. The full_page_chat surface populates it via an
        # ``updated-config`` diff on continuation. Sending ``[]`` matches
        # the wire shape Notion expects for first-turn ai_module.
        "availableConnectors":            [],
        "customConnectorInfo":            [],

        "searchScopes":                   [{"type": "everything"}],
        "useSearchToolV2":                False,
        "useWebSearch":                   use_web_search,

        "isHipaa":                        False,
        "yoloMode":                       False,
        "useReadOnlyMode":                use_read_only_mode,
        "writerMode":                     False,

        "model":                          notion_model,

        "isCustomAgent":                  False,
        "isCustomAgentBuilder":           False,
        "isAgentResearchRequest":         False,
        "useCustomAgentDraft":            False,
        "use_draft_actor_pointer":        False,

        "enableUpdatePageAutofixer":      True,
        "enableMarkdownVNext":            False,
        "updatePageStaleViewGuardEnabled": False,
        "enableUpdatePageOrderUpdates":   True,
        "enableAgentSupportPropertyReorder": True,
        "agentShortUpdatePageResult":     True,
        "enableAgentAskSurvey":           True,
        "databaseAgentConfigMode":        False,
        "isOnboardingAgent":              False,
        "isMobile":                       False,
    }
    if not use_workspace_search and not use_web_search:
        cfg.pop("searchScopes", None)
    if is_subsequent_turn:
        cfg["isThreadStartedByAdmin"] = True
    return cfg


# --------------------------------------------------------------------------- #
# Context block (transcript[1])
# --------------------------------------------------------------------------- #

def build_context_value(
    acc: NotionAccount,
    *,
    current_datetime: str | None = None,
    workflow_id: str | None = None,
) -> dict[str, Any]:
    """``context`` entry value — includes Jarvis-style binding when set.

    ``workflow_id`` switches the context block into workflow-run mode
    (R5-A § runInferenceTranscript): ``surface`` becomes
    ``"custom_agent"`` and ``workflowId`` is added so Notion files the
    run under the bound workflow rather than the default ✦ AI panel.
    Leave it ``None`` for the chat-panel persona-overlay path.
    """
    ctx: dict[str, Any] = {
        "timezone":         acc.timezone,
        "userName":         acc.user_name,
        "userId":           acc.user_id,
        "userEmail":        acc.user_email,
        "spaceName":        acc.space_name,
        "spaceId":          acc.space_id,
        "spaceViewId":      acc.space_view_id,
        "currentDatetime":  current_datetime or _now_iso(acc.timezone),
        "surface":          "custom_agent" if workflow_id else "ai_module",
    }
    if workflow_id:
        ctx["workflowId"] = workflow_id
    if acc.has_jarvis_binding:
        ctx["agentName"] = acc.agent_name
        if acc.agent_accessory:
            ctx["agentAccessory"] = acc.agent_accessory
        ctx["context_page_id"] = acc.agent_context_page_id
    return ctx


# --------------------------------------------------------------------------- #
# Full transcript (first turn, createThread=True)
# --------------------------------------------------------------------------- #

def build_full_transcript(
    acc: NotionAccount,
    *,
    user_text: str,
    notion_model: str,
    use_web_search: bool = True,
    use_workspace_search: bool = True,
    use_read_only_mode: bool = False,
    config_id: str | None = None,
    context_id: str | None = None,
    now: str | None = None,
    workflow_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build a transcript for the first turn of a conversation.

    ``workflow_id`` flips the context block into workflow-run mode
    (used by ``runs start``); leave it ``None`` for the default
    chat-panel path.
    """
    now = now or _now_iso(acc.timezone)
    return [
        {
            "id":    config_id or new_uuid(),
            "type":  "config",
            "value": build_config_value(
                notion_model=notion_model,
                is_subsequent_turn=False,
                use_web_search=use_web_search,
                use_workspace_search=use_workspace_search,
                use_read_only_mode=use_read_only_mode,
            ),
        },
        {
            "id":    context_id or new_uuid(),
            "type":  "context",
            "value": build_context_value(
                acc, current_datetime=now, workflow_id=workflow_id,
            ),
        },
        {
            "id":        new_uuid(),
            "type":      "user",
            "value":     [[user_text]],
            "userId":    acc.user_id,
            "createdAt": now,
        },
    ]


# --------------------------------------------------------------------------- #
# Partial transcript (subsequent turns, isPartialTranscript=True)
# --------------------------------------------------------------------------- #

def build_partial_transcript(
    acc: NotionAccount,
    *,
    new_user_text: str,
    notion_model: str,
    config_id: str,
    context_id: str,
    updated_config_ids: list[str],
    use_web_search: bool = True,
    use_workspace_search: bool = True,
    use_read_only_mode: bool = False,
    original_datetime: str | None = None,
    workflow_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build a transcript for a subsequent turn (continuation of a thread).

    Reuses the original config/context ids + a placeholder
    ``updated-config`` entry per previous turn so Notion can look up
    stored assistant responses.
    """
    transcript: list[dict[str, Any]] = [
        {
            "id":    config_id,
            "type":  "config",
            "value": build_config_value(
                notion_model=notion_model,
                is_subsequent_turn=True,
                use_web_search=use_web_search,
                use_workspace_search=use_workspace_search,
                use_read_only_mode=use_read_only_mode,
            ),
        },
        {
            "id":    context_id,
            "type":  "context",
            "value": build_context_value(
                acc,
                current_datetime=original_datetime,
                workflow_id=workflow_id,
            ),
        },
    ]
    for uc_id in updated_config_ids:
        transcript.append({"id": uc_id, "type": "updated-config"})
    transcript.append({
        "id":        new_uuid(),
        "type":      "user",
        "value":     [[new_user_text]],
        "userId":    acc.user_id,
        "createdAt": _now_iso(acc.timezone),
    })
    return transcript


# --------------------------------------------------------------------------- #
# Top-level request body
# --------------------------------------------------------------------------- #

def build_inference_request(
    acc: NotionAccount,
    *,
    transcript: list[dict[str, Any]],
    thread_id: str,
    create_thread: bool,
    is_partial_transcript: bool,
    as_patch_response: bool = True,
    generate_title: bool = True,
    trace_id: str | None = None,
    workflow_id: str | None = None,
) -> dict[str, Any]:
    """Full POST body for /api/v3/runInferenceTranscript.

    ``workflow_id`` (R5-A § runInferenceTranscript) opts into
    workflow-mode runs: ``threadParentPointer`` files the thread
    under the workflow record (vs the space), ``createdSource`` flips
    to ``"custom_agent"``, and the run executes the agent's full
    tool stack (the long-running batch path ``runs start`` wraps).
    Leave ``None`` for the chat-panel default-AI / persona-overlay
    path.
    """
    body: dict[str, Any] = {
        "traceId":                 trace_id or new_uuid(),
        "spaceId":                 acc.space_id,
        "transcript":              transcript,
        "threadId":                thread_id,
        "createThread":            create_thread,
        "isPartialTranscript":     is_partial_transcript,
        "generateTitle":           generate_title and create_thread,
        "saveAllThreadOperations": True,
        "setUnreadState":          True,
        "threadType":              "workflow",
        "asPatchResponse":         as_patch_response,
        "hasHeartbeat":            False,
        "createdSource":           "custom_agent" if workflow_id else "ai_module",
        "isUserInAnySalesAssistedSpace": False,
        "isSpaceSalesAssisted":         False,
        "debugOverrides": {
            "emitAgentSearchExtractedResults": True,
            "cachedInferences":     {},
            "annotationInferences": {},
            "emitInferences":       False,
        },
    }
    if create_thread:
        if workflow_id:
            body["threadParentPointer"] = {
                "table":   "workflow",
                "id":      workflow_id,
                "spaceId": acc.space_id,
            }
        else:
            body["threadParentPointer"] = {
                "table":   "space",
                "id":      acc.space_id,
                "spaceId": acc.space_id,
            }
    return body
