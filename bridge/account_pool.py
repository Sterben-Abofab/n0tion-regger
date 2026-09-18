from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional

from notion_agent_cli.account import load_notion_account
from notion_agent_cli.exceptions import ErrorCode, NotionAgentError
from notion_agent_cli.provider import NotionAgentClient

log = logging.getLogger('notion_bridge.pool')


class AccountPool:
    """Puul of Notion accounts for automatic rotation and failover."""

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.accounts_dir = repo_root / 'accounts'
        self.current_path: Optional[Path] = None
        self.client: Optional[NotionAgentClient] = None
        self.exhausted_accounts: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self.total_rotations = 0

    def list_accounts(self) -> list[Path]:
        """Lists all valid account files in accounts/."""
        if not self.accounts_dir.exists():
            return []
        files = [
            f for f in self.accounts_dir.glob('*.json')
            if not f.name.startswith('.') and f.name != 'active_account.json'
        ]
        files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        return files

    def get_active_account_path(self) -> Path:
        """Gets current active or best available account."""
        if self.current_path and self.current_path.exists():
            return self.current_path

        local_active = self.accounts_dir / 'active_account.json'
        if local_active.exists():
            self.current_path = local_active
            return local_active

        accounts = self.list_accounts()
        if accounts:
            self.current_path = accounts[0]
            return accounts[0]

        home_acc = Path.home() / '.notionagents' / 'notion_account.json'
        return home_acc

    async def get_client(self) -> NotionAgentClient:
        """Returns the initialized NotionAgentClient for active account."""
        async with self._lock:
            if self.client is None:
                acc_path = self.get_active_account_path()
                if not acc_path.exists():
                    raise RuntimeError('No Notion accounts found in accounts/ pool.')
                self.client = NotionAgentClient(acc_path)
                self.current_path = acc_path
                self._sync_active_file(acc_path)
            return self.client

    def _sync_active_file(self, acc_path: Path):
        """Syncs active_account.json so UI and cli always see current account."""
        try:
            if not acc_path.exists():
                return
            data = acc_path.read_text(encoding='utf-8')
            active_target = self.accounts_dir / 'active_account.json'
            active_target.write_text(data, encoding='utf-8')

            home_dir = Path.home() / '.notionagents'
            home_dir.mkdir(parents=True, exist_ok=True)
            (
                home_dir / 'notion_account.json'
            ).write_text(data, encoding='utf-8')
        except Exception as exc:
            log.warning(f'Failed to sync active_account.json: {exc}')

    async def rotate(self, reason: str = '') -> NotionAgentClient:
        """Rotates to next account in pool."""
        async with self._lock:
            accounts = self.list_accounts()
            if not accounts:
                raise RuntimeError('No accounts in pool to rotate to.')

            curr_str = str(self.current_path.resolve()) if self.current_path else ''
            if curr_str:
                self.exhausted_accounts[curr_str] = time.time()

            now = time.time()
            candidates = [f for f in accounts if str(f.resolve()) != curr_str]
            fresh = [
                f for f in candidates if (now - self.exhausted_accounts.get(str(f.resolve()), 0)) > 1800
            ]

            next_acc = fresh[0] if fresh else (candidates[0] if candidates else accounts[0])

            prev_name = self.current_path.name if self.current_path else 'None'
            log.info(f'Switching account: {prev_name} -> {next_acc.name} (Reason: {reason})')
            try:
                print(
                    f'[POOL ROTATION] Ротация аккаунта: {prev_name} -> {next_acc.name} (Причина: {reason})',
                    flush=True
                )
            except Exception:
                pass

            if self.client is not None:
                try:
                    await self.client.aclose()
                except Exception:
                    pass

            self.current_path = next_acc
            self.client = NotionAgentClient(next_acc)
            self._sync_active_file(next_acc)
            self.total_rotations += 1
            return self.client

    def is_rotation_error(self, exc: Exception) -> bool:
        """Verifies if error is quota/rate-limit related."""
        err_str = str(exc).lower()
        if isinstance(exc, NotionAgentError):
            if exc.code in (
                ErrorCode.AUTH_INVALID,
                ErrorCode.PREMIUM_REQUIRED,
                ErrorCode.HTTP_ERROR,
                ErrorCode.NOTION_ERROR,
            ):
                return True
        keywords = (
            'rate limit',
            'too many requests',
            'quota',
            'usage limit',
            'exhausted',
            'credit',
            '429',
            '401',
            '403',
            'premium-feature-unavailable',
            'trust-rule-denied',
            'unauthorized',
        )
        return any(k in err_str for k in keywords)

    async def run_with_retry(
        self,
        callable: Callable[[NotionAgentClient], Coroutine[Any, Any, Any]],
        max_retries: Optional[int] = None,
    ) -> Any:
        """Runs function with auto-rotation on quota/rate-limit."""
        accounts = self.list_accounts()
        limit = max_retries if max_retries is not None else max(1, len(accounts))

        last_error = None
        for attempt in range(limit):
            client = await self.get_client()
            try:
                return await callable(client)
            except Exception as exc:
                last_error = exc
                if self.is_rotation_error(exc) and attempt < limit - 1:
                    await self.rotate(reason=str(exc))
                    continue
                raise exc

        raise last_error
