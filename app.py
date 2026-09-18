import os
import sys
import json
import time
import math
import queue
import threading
import subprocess
import asyncio
import concurrent.futures
from typing import Optional, Dict, Any, List
from pathlib import Path

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.account_manager import (
    list_accounts, delete_account, export_all_tokens, get_accounts_dir,
    get_active_account, set_active_account
)
from core.manager_process import NotionManagerProcess
from core.bridge_process import NotionBridgeProcess
from core.automator import NotionTrialAutomator, find_browser_executable, switch_to_interactive_desktop
from core.mail_provider import MailTmProvider, ManualMailProvider, ImapMailProvider, CatchAllMailProvider, generate_next_dot_email, generate_next_catchall_email, FIRST_NAMES, LAST_NAMES
from core.preset_manager import list_presets, get_preset, save_preset, delete_preset, DEFAULT_PRESET
from core.proxy_helper import test_proxy

app = FastAPI(title="Notion Business Trial & Manager Studio")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

manager_proc = NotionManagerProcess()
bridge_proc = NotionBridgeProcess()

event_queue: queue.Queue = queue.Queue()
active_registration_lock = threading.Lock()
is_registering = False
current_task_info = {"status": "idle", "step": 0, "logs": []}

manual_code_queue: queue.Queue = queue.Queue()


class RegisterRequest(BaseModel):
    mode: str = "catchall"  # "catchall", "imap", "manual", "temp"
    email: Optional[str] = "huynamhuyvam1228@gmail.com"
    imap_password: Optional[str] = None
    imap_server: Optional[str] = "imap.gmail.com"
    imap_port: Optional[int] = 993
    auto_dot_trick: Optional[bool] = False
    catchall_domain: Optional[str] = None
    catchall_style: Optional[str] = "corporate"
    batch_count: Optional[int] = 1
    concurrency: Optional[int] = 1
    preset_id: Optional[str] = "notion_business_trial_default"
    preset_override: Optional[Dict[str, Any]] = None
    headless: Optional[bool] = None
    proxy: Optional[str] = None


class CodeSubmitRequest(BaseModel):
    code: str


class CatchAllPreviewRequest(BaseModel):
    domain: str
    style: Optional[str] = "corporate"
    count: Optional[int] = 5


if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def broadcast_log(level: str, message: str, extra: Optional[Dict[str, Any]] = None):
    try:
        print(f"[{level.upper()}] {message}", flush=True)
    except Exception:
        try:
            print(f"[{level.upper()}] {message.encode('ascii', errors='replace').decode('ascii')}", flush=True)
        except Exception:
            pass
    event_data = {
        "time": time.strftime("%H:%M:%S"),
        "level": level,
        "message": message,
        "extra": extra or {}
    }
    current_task_info["logs"].append(event_data)
    if extra and "step" in extra:
        current_task_info["step"] = extra["step"]
    event_queue.put(event_data)


def run_registration_worker(req: RegisterRequest):
    global is_registering
    with active_registration_lock:
        is_registering = True
        current_task_info["status"] = "running"
        current_task_info["logs"] = []
        switch_to_interactive_desktop()

        try:
            preset = req.preset_override or get_preset(req.preset_id or "notion_business_trial_default")
            preset_name = preset.get("name", "Custom")

            total_runs = max(1, min(req.batch_count or 1, 50))
            concurrency = max(1, min(req.concurrency or 1, 10))
            effective_concurrency = min(concurrency, total_runs)
            total_passes = math.ceil(total_runs / effective_concurrency)

            successful_accounts = []
            results_lock = threading.Lock()

            broadcast_log(
                "info",
                f"🚀 Запуск регистрации. Режим: '{req.mode.upper()}', Пресет: '{preset_name}', "
                f"Запланировано аккаунтов: {total_runs}, Параллельных потоков: {effective_concurrency}, Проходок: {total_passes}"
            )

            account_cursor = 1
            for pass_idx in range(1, total_passes + 1):
                items_in_pass = min(effective_concurrency, total_runs - (account_cursor - 1))
                pass_account_nums = [account_cursor + i for i in range(items_in_pass)]
                account_cursor += items_in_pass

                range_str = f"#{pass_account_nums[0]}" if items_in_pass == 1 else f"#{pass_account_nums[0]}-#{pass_account_nums[-1]}"
                broadcast_log(
                    "info",
                    f"🔄 [Проходка {pass_idx}/{total_passes}] Запуск {items_in_pass} параллельных браузеров (Аккаунты {range_str} из {total_runs})..."
                )

                def execute_single(item_idx: int):
                    prefix = f"[Поток #{item_idx}/{total_runs}]"

                    def item_logger(level: str, msg: str, extra: Optional[Dict[str, Any]] = None):
                        broadcast_log(level, f"{prefix} {msg}", extra)

                    if req.mode == "catchall":
                        if not req.catchall_domain:
                            raise ValueError("Для режима Catch-All укажите ваш домен (например: mycompany.xyz)")
                        if not req.imap_password:
                            raise ValueError("Не указан пароль приложения (App Password) для IMAP ящика-получателя")

                        recipient_box = (req.email or "huynamhuyvam1228@gmail.com").strip()
                        if not recipient_box:
                            raise ValueError("Укажите email IMAP ящика, куда пересылаются письма с вашего домена")

                        provider = CatchAllMailProvider(
                            domain=req.catchall_domain,
                            imap_user=recipient_box,
                            imap_password=req.imap_password,
                            style=req.catchall_style or "corporate",
                            imap_server=req.imap_server or "imap.gmail.com",
                            imap_port=req.imap_port or 993,
                            log_callback=lambda msg: item_logger("info", f"🌐 [Catch-All IMAP] {msg}", {"step": 3})
                        )
                        gen_email = provider.get_email()
                        item_logger("info", f"🌐 Catch-All: Сгенерирован адрес '{gen_email}' (письма придут в {recipient_box})", {"step": 1, "target_email": gen_email})

                    elif req.mode == "imap":
                        if not req.email:
                            raise ValueError("Для режима IMAP необходимо указать базовый email")
                        if not req.imap_password:
                            raise ValueError("Не указан пароль приложения (App Password) для IMAP")

                        target_email = req.email.strip()
                        if req.auto_dot_trick and "@gmail.com" in target_email.lower():
                            with results_lock:
                                existing_accs = list_accounts()
                                existing_emails = [a.get("user_email", "") for a in existing_accs]
                                dot_email = generate_next_dot_email(target_email, existing_emails)
                            item_logger("info", f"⚡ Gmail Dot Trick: сгенерирована вариация '{dot_email}'", {"step": 1, "target_email": dot_email})
                            target_email = dot_email
                        else:
                            item_logger("info", f"Используем почту: {target_email}", {"step": 1, "target_email": target_email})

                        provider = ImapMailProvider(
                            target_email=target_email,
                            imap_user=req.email.strip(),
                            imap_password=req.imap_password,
                            imap_server=req.imap_server or "imap.gmail.com",
                            imap_port=req.imap_port or 993,
                            log_callback=lambda msg: item_logger("info", f"📬 [IMAP] {msg}", {"step": 3})
                        )

                    elif req.mode == "manual":
                        if not req.email:
                            raise ValueError("Для ручного режима необходимо указать email")
                        target_email = req.email.strip()
                        def manual_code_getter(timeout):
                            item_logger("warning", f"Ожидание ввода кода для {target_email}. Введите его в панели управления!", {"step": 3, "waiting_for_code": True})
                            try:
                                return manual_code_queue.get(timeout=timeout)
                            except queue.Empty:
                                return None
                        provider = ManualMailProvider(target_email, code_getter_callback=manual_code_getter)

                    else:
                        item_logger("info", "Генерируем чистый почтовый ящик через Mail.tm API...", {"step": 1})
                        provider = MailTmProvider()
                        item_logger("info", f"Сгенерирован временный адрес: {provider.get_email()}", {"step": 1, "email": provider.get_email()})

                    automator = NotionTrialAutomator(
                        email_provider=provider,
                        preset=preset,
                        override_headless=req.headless,
                        proxy=req.proxy if req.proxy else None,
                        log_callback=item_logger
                    )

                    result = automator.run()
                    with results_lock:
                        successful_accounts.append(result.get("email"))
                        try:
                            bridge_proc.reload_account()
                            item_logger("success", "⚡ Автономный API Мост автоматически переключен на новый аккаунт!")
                        except Exception:
                            pass

                    item_logger("success", f"🏆 Аккаунт [{item_idx}/{total_runs}] успешно активирован: {result.get('email')}", {"step": 8, "result": result})
                    return result

                # Запускаем браузеры текущей проходки параллельно в потоках
                with concurrent.futures.ThreadPoolExecutor(max_workers=items_in_pass) as executor:
                    futures = {executor.submit(execute_single, num): num for num in pass_account_nums}
                    for future in concurrent.futures.as_completed(futures):
                        num = futures[future]
                        try:
                            future.result()
                        except Exception as e:
                            broadcast_log("error", f"❌ Ошибка в потоке [#{num}/{total_runs}]: {str(e)}", {"error": str(e), "item": num})

                broadcast_log("info", f"✔ [Проходка {pass_idx}/{total_passes}] завершена. Браузеры проходки закрыты.")

                if pass_idx < total_passes:
                    broadcast_log("info", "⏳ Пауза 5 секунд перед запуском следующей проходки...")
                    time.sleep(5)

            current_task_info["status"] = "completed"
            broadcast_log("success", f"🎉 Пакетная регистрация завершена! Всего успешно создано аккаунтов: {len(successful_accounts)} из {total_runs}")

        except Exception as e:
            current_task_info["status"] = "failed"
            broadcast_log("error", f"❌ Ошибка регистрации: {str(e)}", {"error": str(e), "status": "failed"})
        finally:
            is_registering = False


@app.on_event("startup")
def startup_event():
    if not manager_proc.is_running():
        manager_proc.start()
    if not bridge_proc.is_running():
        bridge_proc.start()


@app.get("/", response_class=HTMLResponse)
def read_root():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        with open(index_file, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>Notion Business Studio: UI загружается...</h1>"


@app.get("/api/status")
def get_system_status():
    proc_status = manager_proc.get_status()
    bridge_status = bridge_proc.get_status()
    active_acc = get_active_account()
    accounts = list_accounts()
    return {
        "manager": proc_status,
        "bridge": bridge_status,
        "active_account": active_acc,
        "accounts_count": len(accounts),
        "registration": {
            "is_running": is_registering,
            "status": current_task_info["status"],
            "step": current_task_info["step"],
            "logs": current_task_info.get("logs", [])
        }
    }


@app.post("/api/imap/test")
def test_imap_connection(req: Dict[str, Any]):
    email_user = (req.get("email") or "").strip()
    password = (req.get("imap_password") or "").replace(" ", "").strip()
    server = (req.get("imap_server") or "imap.gmail.com").strip()
    port = int(req.get("imap_port") or 993)

    if not email_user or not password:
        raise HTTPException(status_code=400, detail="Укажите email и пароль приложения")

    try:
        import imaplib
        mail = imaplib.IMAP4_SSL(server, port)
        mail.login(email_user, password)
        status, counts = mail.select("INBOX", readonly=True)
        mail.logout()
        return {
            "success": True,
            "message": f"Успешное подключение к {server}! Найдено писем в INBOX: {counts[0].decode() if counts else '0'}"
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка подключения к IMAP: {str(e)}")


@app.post("/api/proxy/test")
def test_proxy_connection(req: Dict[str, Any]):
    proxy_str = (req.get("proxy") or "").strip()
    if not proxy_str:
        raise HTTPException(status_code=400, detail="Укажите строку прокси")

    res = test_proxy(proxy_str)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "Не удалось подключиться к прокси"))
    return res




# ── Пресеты Playwright ────────────────────────────────────────────────────────
@app.get("/api/presets")
def get_all_presets():
    return {"presets": list_presets()}


@app.get("/api/presets/{preset_id}")
def get_single_preset(preset_id: str):
    return get_preset(preset_id)


@app.post("/api/presets")
def create_or_update_preset(preset_data: Dict[str, Any]):
    path = save_preset(preset_data)
    return {"success": True, "path": path}


@app.delete("/api/presets/{preset_id}")
def remove_preset(preset_id: str):
    ok = delete_preset(preset_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Невозможно удалить базовый дефолтный пресет")
    return {"success": True}


@app.post("/api/playwright/codegen")
def launch_playwright_codegen(url: Optional[str] = "https://app.notion.com/signup"):
    """Запускает официальный генератор селекторов Playwright Codegen в отдельном окне"""
    python_exe = sys.executable
    cmd = [python_exe, "-m", "playwright", "codegen", url or "https://app.notion.com/signup"]
    subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0)
    return {"success": True, "message": "Playwright Codegen запущен в отдельном окне."}


@app.post("/api/catchall/preview")
def preview_catchall(req: CatchAllPreviewRequest):
    domain = (req.domain or "").strip().lower().lstrip("@")
    if not domain:
        domain = "myteam-work.xyz"
    emails = []
    temp_used = []
    for _ in range(max(1, min(req.count or 5, 20))):
        e = generate_next_catchall_email(domain, style=req.style or "corporate", existing_emails=temp_used, save=False)
        temp_used.append(e)
        emails.append(e)
    return {"success": True, "domain": domain, "style": req.style, "sample_emails": emails}


# ── Управление Notion Manager ─────────────────────────────────────────────────
@app.post("/api/manager/start")
def start_manager():
    ok = manager_proc.start()
    return {"success": ok, "status": manager_proc.get_status()}


@app.post("/api/manager/stop")
def stop_manager():
    ok = manager_proc.stop()
    return {"success": ok, "status": manager_proc.get_status()}


@app.post("/api/manager/restart")
def restart_manager():
    manager_proc.stop()
    time.sleep(0.5)
    ok = manager_proc.start()
    return {"success": ok, "status": manager_proc.get_status()}


# ── Управление Codex Bridge (Автономный кодинг на порту 8765) ────────────────
@app.post("/api/bridge/start")
def start_bridge():
    ok = bridge_proc.start()
    return {"success": ok, "status": bridge_proc.get_status()}


@app.post("/api/bridge/stop")
def stop_bridge():
    ok = bridge_proc.stop()
    return {"success": ok, "status": bridge_proc.get_status()}


@app.post("/api/bridge/restart")
def restart_bridge():
    bridge_proc.stop()
    time.sleep(0.5)
    ok = bridge_proc.start()
    return {"success": ok, "status": bridge_proc.get_status()}


@app.get("/api/active-account")
def get_current_active_account():
    return {"account": get_active_account()}


@app.post("/api/active-account/{filename}")
def set_current_active_account(filename: str):
    ok = set_active_account(filename)
    if not ok:
        raise HTTPException(status_code=400, detail="Не удалось установить активный аккаунт")
    bridge_proc.reload_account()
    return {"success": True, "account": get_active_account()}


@app.post("/api/pool/rotate")
def rotate_pool_account():
    try:
        req = urllib.request.Request("http://127.0.0.1:8765/api/pool/rotate", method="POST")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {"success": True, "data": data, "active_account": get_active_account()}
    except Exception as e:
        accounts = list_accounts()
        if len(accounts) > 1:
            curr = get_active_account()
            curr_email = curr.get("user_email") if curr else ""
            candidates = [a for a in accounts if a.get("user_email") != curr_email]
            next_acc = candidates[0] if candidates else accounts[0]
            set_active_account(next_acc["filename"])
            return {"success": True, "active_account": get_active_account()}
        return {"success": False, "error": str(e)}


# ── Аккаунты ──────────────────────────────────────────────────────────────────
@app.get("/api/accounts")
def get_accounts():
    return {"accounts": list_accounts()}


@app.delete("/api/accounts/{filename}")
def remove_account(filename: str):
    ok = delete_account(filename)
    if not ok:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    return {"success": True}


@app.get("/api/accounts/export")
def export_accounts(format: str = "json"):
    content = export_all_tokens(format_type=format)
    if format == "txt":
        return PlainTextResponse(content, headers={"Content-Disposition": "attachment; filename=notion_tokens.txt"})
    return JSONResponse(json.loads(content), headers={"Content-Disposition": "attachment; filename=notion_accounts.json"})


# ── Регистрация и SSE ─────────────────────────────────────────────────────────
@app.post("/api/register/start")
def start_register(req: RegisterRequest, background_tasks: BackgroundTasks):
    global is_registering
    if is_registering:
        raise HTTPException(status_code=400, detail="Процесс регистрации уже запущен")

    background_tasks.add_task(run_registration_worker, req)
    return {"success": True, "message": "Процесс регистрации запущен"}


@app.post("/api/register/submit-code")
def submit_code(req: CodeSubmitRequest):
    code = req.code.strip()
    if not code:
        raise HTTPException(status_code=400, detail="Код не может быть пустым")
    manual_code_queue.put(code)
    broadcast_log("info", f"Код {code} передан в скрипт регистрации.")
    return {"success": True}


@app.get("/api/register/events")
async def registration_events(request: Request):
    async def event_generator():
        for log_item in current_task_info["logs"][-30:]:
            yield f"data: {json.dumps(log_item, ensure_ascii=False)}\n\n"

        while True:
            if await request.is_disconnected():
                break
            try:
                item = event_queue.get_nowait()
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            except queue.Empty:
                yield ": ping\n\n"
                await asyncio.sleep(1.0)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
