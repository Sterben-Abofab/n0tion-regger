import os
import json
from pathlib import Path
from typing import List, Dict, Any, Optional

PRESETS_DIR = Path(__file__).resolve().parent.parent / "presets"

DEFAULT_PRESET: Dict[str, Any] = {
    "id": "notion_business_trial_default",
    "name": "⚡ Notion Business Trial (Автоматический)",
    "description": "Полный цикл автоматической регистрации: ввод email -> получение кода -> выбор Work -> Free план -> Teamspaces Open -> Start free trial -> сохранение в пул",
    "browser": {
        "channel": "chrome",
        "headless": False,
        "use_persistent_context": True,
        "user_data_dir": "browser_profiles/default",
        "viewport": {"width": 1280, "height": 800},
        "user_agent": None,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox"
        ]
    },
    "urls": {
        "signup": "https://app.notion.com/signup",
        "login": "https://www.notion.so/login"
    },
    "selectors": {
        "email_input": "input[type='email'], input[placeholder*='name@company.com' i], input[placeholder*='email' i], input[type='text']",
        "continue_btn": "button:has-text('Continue with email'), button:has-text('Continue')",
        "code_input": "input[placeholder*='code' i], input[autocomplete='one-time-code'], input[type='text']",
        "onboarding_work": "text='For work', text='With my team', text='For my team', text='Work', [data-testid='onboarding-for-work']",
        "skip_buttons": "button:has-text('Start from scratch'), div[role='button']:has-text('Start from scratch'), button:has-text('Skip for now'), button:has-text('Skip'), text='Start from scratch', text='Skip for now', text='Skip'",
        "free_plan": "text='Free plan', text='Free', button:has-text('Continue with Free'), [data-testid='free-plan-card']",
        "settings_btn": "text='Settings & members', text='Settings', [data-tab-name='settings']",
        "teamspaces_tab": "text='Teamspaces', [data-tab-name='teamspaces']",
        "new_teamspace_btn": "button:has-text('New teamspace'), text='New teamspace', text='Create a teamspace'",
        "open_access": "text='Open', label:has-text('Open'), [data-value='open']",
        "start_trial_btn": "button:has-text('Start free trial'), text='Start free trial', button:has-text('Try Business'), text='Upgrade to Business'"
    },
    "delays": {
        "after_navigate_ms": 4000,
        "after_click_ms": 1500,
        "step_timeout_sec": 25,
        "mail_timeout_sec": 120
    },
    "workspace": {
        "default_name": "Business Team",
        "teamspace_name": "Main Teamspace"
    }
}

STEALTH_PRESET: Dict[str, Any] = {
    "id": "notion_stealth_profile",
    "name": "🛡️ Постоянный профиль (Persistent Anti-Detect)",
    "description": "Использует сохранённый профиль Chrome в папке browser_profiles/stealth. Позволяет один раз пройти Cloudflare, сохранить куки и не вызывать подозрения антифрода Notion.",
    "browser": {
        "channel": "chrome",
        "headless": False,
        "use_persistent_context": True,
        "user_data_dir": "browser_profiles/stealth",
        "viewport": {"width": 1366, "height": 768},
        "user_agent": None,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox"
        ]
    },
    "urls": {
        "signup": "https://app.notion.com/signup",
        "login": "https://www.notion.so/login"
    },
    "selectors": DEFAULT_PRESET["selectors"],
    "delays": {
        "after_navigate_ms": 5000,
        "after_click_ms": 2000,
        "step_timeout_sec": 35,
        "mail_timeout_sec": 120
    },
    "workspace": {
        "default_name": "Pro Teamspace",
        "teamspace_name": "Core Space"
    }
}

MANUAL_ASSIST_PRESET: Dict[str, Any] = {
    "id": "notion_manual_assist",
    "name": "✋ Ручной ассистент / Захват токена",
    "description": "Открывает окно браузера для самостоятельных действий. Вы можете войти или пройти шаги сами, а затем нажать одну кнопку 'Захватить токен в пул'.",
    "browser": {
        "channel": "chrome",
        "headless": False,
        "use_persistent_context": False,
        "user_data_dir": "browser_profiles/manual",
        "viewport": {"width": 1280, "height": 800},
        "user_agent": None,
        "args": ["--disable-blink-features=AutomationControlled"]
    },
    "urls": {
        "signup": "https://app.notion.com/signup",
        "login": "https://www.notion.so/login"
    },
    "selectors": DEFAULT_PRESET["selectors"],
    "delays": {
        "after_navigate_ms": 3000,
        "after_click_ms": 1000,
        "step_timeout_sec": 60,
        "mail_timeout_sec": 180
    },
    "workspace": {
        "default_name": "My Workspace",
        "teamspace_name": "Team Space"
    }
}


def ensure_default_presets():
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    defaults = [DEFAULT_PRESET, STEALTH_PRESET, MANUAL_ASSIST_PRESET]
    for p in defaults:
        p_path = PRESETS_DIR / f"{p['id']}.json"
        if not p_path.exists():
            with open(p_path, "w", encoding="utf-8") as f:
                json.dump(p, f, ensure_ascii=False, indent=2)


def list_presets() -> List[Dict[str, Any]]:
    ensure_default_presets()
    presets = []
    for f in PRESETS_DIR.glob("*.json"):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                data["filename"] = f.name
                presets.append(data)
        except Exception as e:
            print(f"Ошибка чтения пресета {f}: {e}")
    return presets


def get_preset(preset_id: str) -> Dict[str, Any]:
    ensure_default_presets()
    p_path = PRESETS_DIR / f"{preset_id}.json"
    if p_path.exists():
        try:
            with open(p_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return DEFAULT_PRESET


def save_preset(preset_data: Dict[str, Any]) -> str:
    ensure_default_presets()
    preset_id = preset_data.get("id") or "custom_preset"
    p_path = PRESETS_DIR / f"{preset_id}.json"
    with open(p_path, "w", encoding="utf-8") as f:
        json.dump(preset_data, f, ensure_ascii=False, indent=2)
    return str(p_path)


def delete_preset(preset_id: str) -> bool:
    # Защищаем базовый дефолтный пресет от случайного удаления
    if preset_id == "notion_business_trial_default":
        return False
    p_path = PRESETS_DIR / f"{preset_id}.json"
    if p_path.exists():
        p_path.unlink()
        return True
    return False
