import os
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional

ACCOUNTS_DIR = Path(__file__).resolve().parent.parent / "accounts"


def get_accounts_dir() -> Path:
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    return ACCOUNTS_DIR


def list_accounts() -> List[Dict[str, Any]]:
    """Возвращает список всех сохраненных аккаунтов с расширенной метаинформацией."""
    acc_dir = get_accounts_dir()
    accounts = []
    
    for f in acc_dir.glob("*.json"):
        if f.name.startswith(".") or f.name == "active_account.json":
            continue  # Пропускаем служебные файлы и файл активного указателя active_account.json
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                data["filename"] = f.name
                data["filepath"] = str(f.resolve())
                
                # Расчет оставшихся дней триала
                created_at = data.get("created_at") or data.get("trial_started_at")
                if created_at:
                    try:
                        dt = datetime.fromisoformat(created_at)
                        expires_dt = dt + timedelta(days=30)
                        diff = expires_dt - datetime.now()
                        data["days_remaining"] = max(0, diff.days)
                        data["expires_at_formatted"] = expires_dt.strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        data["days_remaining"] = 30
                else:
                    data["days_remaining"] = 30
                    
                accounts.append(data)
        except Exception as e:
            print(f"Ошибка при чтении аккаунта {f}: {e}")
            
    # Сортировка: свежие сверху
    accounts.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return accounts


def get_account_by_filename(filename: str) -> Optional[Dict[str, Any]]:
    acc_path = get_accounts_dir() / filename
    if not acc_path.exists():
        return None
    try:
        with open(acc_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            data["filename"] = filename
            return data
    except Exception:
        return None


def save_account(
    token_v2: str,
    user_id: str,
    space_id: str,
    user_email: str,
    user_name: str = "Notion User",
    space_name: str = "Business Workspace",
    space_view_id: str = "",
    plan_type: str = "business_trial",
    cookies: Optional[Dict[str, str]] = None,
    extra: Optional[Dict[str, Any]] = None
) -> str:
    """
    Сохраняет аккаунт в accounts/<email_clean>.json
    в формате, на 100% совместимом с notion-manager.
    """
    acc_dir = get_accounts_dir()
    
    # Очистка email для безопасного имени файла
    safe_email = user_email.replace("@", "_at_").replace(".", "_")
    filename = f"{safe_email}.json"
    target_path = acc_dir / filename

    now = datetime.now()
    trial_expires = now + timedelta(days=30)

    account_data: Dict[str, Any] = {
        "token_v2": token_v2,
        "user_id": user_id,
        "user_name": user_name,
        "user_email": user_email,
        "space_id": space_id,
        "space_name": space_name,
        "space_view_id": space_view_id,
        "plan_type": plan_type,
        "registered_via": "business_trial_automator",
        "trial_started_at": now.isoformat(),
        "trial_expires_at": trial_expires.isoformat(),
        "created_at": now.isoformat(),
        "timezone": "UTC",
        "client_version": "23.13.20260313.1423",
        "cookies": cookies or {"token_v2": token_v2}
    }

    if extra:
        account_data.update(extra)

    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(account_data, f, ensure_ascii=False, indent=2)

    # Автоматически делаем новый аккаунт активным для API
    try:
        set_active_account(filename)
    except Exception:
        pass

    return str(target_path)


def get_active_account() -> Optional[Dict[str, Any]]:
    """Возвращает текущий активный аккаунт для API."""
    acc_dir = get_accounts_dir()
    active_file = acc_dir / "active_account.json"
    if active_file.exists():
        try:
            with open(active_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                data["filename"] = "active_account.json"
                return data
        except Exception:
            pass
    # Если active_account.json еще не создан, берем самый свежий
    accounts = list_accounts()
    return accounts[0] if accounts else None


def set_active_account(identifier: str) -> bool:
    """Делает указанный аккаунт активным для API и синхронизирует его."""
    acc_dir = get_accounts_dir()
    target = None
    if (acc_dir / identifier).exists():
        target = acc_dir / identifier
    else:
        for f in acc_dir.glob("*.json"):
            if f.name == "active_account.json" or f.name.startswith("."):
                continue
            if identifier in f.name:
                target = f
                break
    if not target or not target.exists():
        return False

    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 1. Локальный active_account.json в папке accounts/
        with open(acc_dir / "active_account.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        # 2. Совместимость с ~/.notionagents/notion_account.json
        home_notion = Path.home() / ".notionagents"
        home_notion.mkdir(parents=True, exist_ok=True)
        with open(home_notion / "notion_account.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        return True
    except Exception as e:
        print(f"Ошибка установки активного аккаунта: {e}")
        return False


def delete_account(filename: str) -> bool:
    if filename == "active_account.json" or filename.startswith("."):
        return False
    acc_path = get_accounts_dir() / filename
    if acc_path.exists():
        acc_path.unlink()
        return True
    return False


def export_all_tokens(format_type: str = "json") -> str:
    """Экспорт всех аккаунтов в формате JSON или TXT (список token_v2)."""
    accounts = list_accounts()
    if format_type == "txt":
        lines = []
        for acc in accounts:
            lines.append(f"{acc.get('user_email')}----{acc.get('token_v2')}----{acc.get('space_id')}")
        return "\n".join(lines)
    else:
        return json.dumps(accounts, ensure_ascii=False, indent=2)
