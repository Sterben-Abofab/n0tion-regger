import os
import re
import time
import json
import random
import string
import socket
import subprocess
import urllib.request
from pathlib import Path
from typing import Optional, Callable, Dict, Any
from playwright.sync_api import sync_playwright, Page, BrowserContext, Browser

from core.mail_provider import MailProvider, MailTmProvider, ManualMailProvider
from core.account_manager import save_account
from core.preset_manager import get_preset, DEFAULT_PRESET
from core.proxy_helper import parse_proxy_string

def find_browser_executable() -> Optional[str]:
    import shutil
    for bin_name in ("chrome", "msedge", "google-chrome", "brave"):
        w = shutil.which(bin_name)
        if w and os.path.isfile(w):
            return w

    candidates = [
        # Edge (предустановлен на всех Windows 10/11)
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
        # Google Chrome
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        # Brave
        os.path.expandvars(r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None


def switch_to_interactive_desktop():
    """Переключает текущий поток на физический интерактивный рабочий стол Default пользователя."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        DESKTOP_ALL_ACCESS = 0x01FF
        hDesk = user32.OpenDesktopW("Default", 0, False, DESKTOP_ALL_ACCESS)
        if hDesk:
            user32.SetThreadDesktop(hDesk)
    except Exception:
        pass


def bring_window_to_front():
    """Без принудительного развертывания на весь экран."""
    pass



class NotionTrialAutomator:
    """
    Универсальный автоматизатор регистрации и активации Business Trial на Playwright
    с полной поддержкой кастомных пресетов.
    """

    def __init__(
        self,
        email_provider: MailProvider,
        preset: Optional[Dict[str, Any]] = None,
        override_headless: Optional[bool] = None,
        proxy: Optional[str] = None,
        log_callback: Optional[Callable[[str, str, Optional[Dict[str, Any]]], None]] = None
    ):
        self.provider = email_provider
        self.preset = preset or DEFAULT_PRESET
        self.headless = override_headless if override_headless is not None else self.preset.get("browser", {}).get("headless", False)
        self.proxy = proxy or self.preset.get("browser", {}).get("proxy")
        self.log_callback = log_callback or (lambda level, msg, extra=None: print(f"[{level}] {msg}"))

    def _log(self, level: str, message: str, extra: Optional[Dict[str, Any]] = None):
        self.log_callback(level, message, extra)

    def _wait_and_click(self, page: Page, selector: str, timeout_sec: int = 10) -> bool:
        """Ожидает селектор и кликает по первому найденному элементу."""
        candidates = [s.strip() for s in selector.split(",") if s.strip()]
        per_timeout = max(800, int((timeout_sec * 1000) / max(len(candidates), 1)))

        for sel in candidates:
            try:
                loc = page.wait_for_selector(sel, timeout=per_timeout, state="visible")
                if loc:
                    loc.click()
                    return True
            except Exception:
                pass

            if sel.startswith("text="):
                clean_text = sel[5:].strip("'\"")
                try:
                    loc = page.wait_for_selector(f":has-text('{clean_text}')", timeout=per_timeout, state="visible")
                    if loc:
                        loc.click()
                        return True
                except Exception:
                    pass

                try:
                    elem = page.locator("button:visible, [role='button']:visible, div:visible, a:visible, span:visible").filter(has_text=clean_text).first
                    if elem.is_visible():
                        elem.click(timeout=1000)
                        return True
                except Exception:
                    pass

        return False

    def _parse_proxy_string(self, proxy_str: str) -> Optional[Dict[str, str]]:
        return parse_proxy_string(proxy_str)

    def run(self) -> Dict[str, Any]:
        email = self.provider.get_email()
        self._log("info", f"Старт регистрации для email: {email}", {"step": 1, "email": email})

        exe_path = find_browser_executable()
        if not exe_path:
            raise RuntimeError("Не найден браузер Chrome или Edge на компьютере.")

        browser_cfg = self.preset.get("browser", {})
        urls_cfg = self.preset.get("urls", {})
        selectors = self.preset.get("selectors", {})
        delays = self.preset.get("delays", {})
        workspace_cfg = self.preset.get("workspace", {})

        signup_url = urls_cfg.get("signup", "https://app.notion.com/signup")
        use_persistent = browser_cfg.get("use_persistent_context", False)
        user_data_dir = browser_cfg.get("user_data_dir", "browser_profiles/default")

        launch_args = browser_cfg.get("args", [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-infobars",
            "--disable-dev-shm-usage",
            "--lang=en-US,en"
        ])

        default_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        user_agent_val = browser_cfg.get("user_agent") or default_ua

        proxy_dict = None
        if self.proxy:
            proxy_dict = self._parse_proxy_string(self.proxy)
            if proxy_dict:
                self._log("info", f"Подключен прокси: {proxy_dict.get('server')}")

        if not self.headless:
            switch_to_interactive_desktop()

        # Находим свободный локальный порт для CDP
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('127.0.0.1', 0))
            cdp_port = s.getsockname()[1]

        clean_slug = re.sub(r'[^a-zA-Z0-9_]', '_', email)
        full_profile_dir = Path(__file__).resolve().parent.parent / "browser_profiles" / f"prof_{clean_slug}_{int(time.time())}_{random.randint(1000, 9999)}"
        full_profile_dir.mkdir(parents=True, exist_ok=True)

        chrome_cmd = [
            exe_path,
            f"--remote-debugging-port={cdp_port}",
            f"--user-data-dir={full_profile_dir}",
            "--disable-blink-features=AutomationControlled",
            "--disable-extensions",
            "--disable-default-apps",
            "--disable-component-extensions-with-background-pages",
            "--no-first-run",
            "--no-default-browser-check",
            "--start-maximized"
        ]
        if self.headless:
            chrome_cmd.append("--headless=new")
        if self.proxy:
            chrome_cmd.append(f"--proxy-server={self.proxy}")
        chrome_cmd.append("about:blank")

        self._log("info", f"Запуск чистого Chrome через CDP (порт {cdp_port}, Headless: {self.headless})...")
        chrome_proc = subprocess.Popen(chrome_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        start_wait = time.time()
        cdp_ready = False
        while time.time() - start_wait < 15:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/version", timeout=1) as resp:
                    if resp.status == 200:
                        cdp_ready = True
                        break
            except Exception:
                time.sleep(0.3)

        if not cdp_ready:
            try:
                chrome_proc.terminate()
            except Exception:
                pass
            raise RuntimeError(f"Не удалось подключиться к порту отладки Chrome {cdp_port}")

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
            context = browser.contexts[0]

            # Закрываем любые лишние фоновые вкладки (расширения, справка и т.д.)
            for extra_p in list(context.pages)[1:]:
                try:
                    extra_p.close()
                except Exception:
                    pass

            page = context.pages[0] if len(context.pages) > 0 else context.new_page()

            # Автоматически закрываем любые сторонние вкладки, которые могут попытаться открыться
            def _close_unwanted_tabs(new_p):
                try:
                    new_p.wait_for_load_state("domcontentloaded", timeout=3000)
                    u = new_p.url.lower()
                    if not any(domain in u for domain in ["notion.so", "notion.com", "about:blank"]):
                        new_p.close()
                except Exception:
                    try:
                        new_p.close()
                    except Exception:
                        pass

            context.on("page", _close_unwanted_tabs)

            if not self.headless:
                page.bring_to_front()

            captured_session = {}

            def _on_request(req):
                try:
                    c_hdr = req.headers.get("cookie", "")
                    if "token_v2=" in c_hdr:
                        m = re.search(r"token_v2=([^;\s]+)", c_hdr)
                        if m:
                            captured_session["token_v2"] = m.group(1).strip()
                    if "notion_user_id=" in c_hdr:
                        m = re.search(r"notion_user_id=([^;\s]+)", c_hdr)
                        if m:
                            captured_session["notion_user_id"] = m.group(1).strip()
                except Exception:
                    pass

            def _on_response(res):
                try:
                    for h_name, h_val in res.headers.items():
                        if "set-cookie" in h_name.lower():
                            if "token_v2=" in h_val:
                                m = re.search(r"token_v2=([^;\s]+)", h_val)
                                if m:
                                    captured_session["token_v2"] = m.group(1).strip()
                            if "notion_user_id=" in h_val:
                                m = re.search(r"notion_user_id=([^;\s]+)", h_val)
                                if m:
                                    captured_session["notion_user_id"] = m.group(1).strip()
                except Exception:
                    pass

            context.on("request", _on_request)
            context.on("response", _on_response)

            try:
                # ── Шаг 1: Переход на страницу регистрации ────────────────────────
                self._log("info", f"Открываем {signup_url}...", {"step": 1})
                page.goto(signup_url, wait_until="commit", timeout=45000)
                if not self.headless:
                    page.bring_to_front()
                    bring_window_to_front()
                page.wait_for_timeout(delays.get("after_navigate_ms", 4000))

                # ── Шаг 2: Ожидание и ввод Email ──────────────────────────────────
                self._log("info", f"Вводим email {email}...", {"step": 2})

                email_selector = selectors.get("email_input", "input[type='email'], input[placeholder*='name@company.com' i], input[type='text']")
                step_timeout = delays.get("step_timeout_sec", 25)

                try:
                    # Надежное ожидание появления поля ввода
                    email_elem = page.wait_for_selector(email_selector, timeout=step_timeout * 1000, state="visible")
                    if not email_elem:
                        raise RuntimeError("Поле ввода email не появилось вовремя")
                    email_elem.click()
                    page.wait_for_timeout(300)
                    email_elem.fill(email)
                except Exception as e:
                    # Если открылась альтернативная страница входа Notion
                    self._log("warning", f"Поиск через основной селектор не удался ({e}), пробуем альтернативный поиск input...")
                    inputs = page.locator("input:visible").all()
                    if inputs:
                        inputs[0].click()
                        inputs[0].fill(email)
                    else:
                        page.screenshot(path="debug_error_step2.png")
                        raise RuntimeError(f"Не удалось обнаружить поле ввода email на странице {page.url}. Проверьте скриншот debug_error_step2.png")

                page.wait_for_timeout(500)

                # Перехватываем ответ сервера Notion на попытку запроса кода
                notion_rejected_reason = []

                def _check_notion_auth_resp(res):
                    if "getLoginOptions" in res.url or "sendTemporaryPassword" in res.url:
                        try:
                            if res.status >= 400:
                                b_txt = res.text()
                                if "Login is not allowed" in b_txt or "login_generic_error" in b_txt:
                                    notion_rejected_reason.append("Login is not allowed (IP заблокирован антифрод-системой Notion)")
                                else:
                                    notion_rejected_reason.append(f"HTTP {res.status}: {b_txt[:120]}")
                        except Exception:
                            pass

                context.on("response", _check_notion_auth_resp)

                # Нажимаем кнопку 'Continue'
                cont_btn_sel = selectors.get("continue_btn", "div[role='button']:has-text('Continue'), button:has-text('Continue'), button:has-text('Continue with email'), [role='button']:has-text('Continue'), [data-testid='submit-email-button']")
                clicked = self._wait_and_click(page, cont_btn_sel, timeout_sec=8)
                if not clicked:
                    page.keyboard.press("Enter")

                # Ожидаем завершения фоновой проверки Notion (появление поля кода или ошибки)
                try:
                    page.wait_for_selector("input[placeholder*='code' i], input[autocomplete='one-time-code'], text='We sent a code', [role='alert'], :has-text('problem')", timeout=15000)
                except Exception:
                    pass

                page.wait_for_timeout(1000)

                # 1. Проверяем сетевой ответ Notion
                if notion_rejected_reason:
                    page.screenshot(path="scratch/notion_blocked_ip.png")
                    raise RuntimeError(f"❌ Notion отклонил запрос: {notion_rejected_reason[0]}. Ваш IP отклонен Notion — необходимо вставить рабочий прокси (Польша/Германия) в панели управления или включить VPN!")

                # 2. Проверяем сообщения об ошибке на странице Notion
                page.wait_for_timeout(1500)
                body_text = page.locator("body").inner_text().lower()
                if "invalid email domain" in body_text:
                    page.screenshot(path="scratch/notion_blocked_ip.png")
                    raise RuntimeError(f"❌ Notion отклонил почту '{email}': 'Invalid email domain'. Notion блокирует публичные временные почты (disposable email). Нужен нормальный домен, свой catch-all или реальная почта!")

                if "problem signing up" in body_text or "problem logging in" in body_text or "login is not allowed" in body_text:
                    page.screenshot(path="scratch/notion_blocked_ip.png")
                    raise RuntimeError("❌ Notion заблокировал отправку кода: 'There was a problem signing up'. Причина: IP-адрес VPN/прокси заблокирован антифрод-системой Notion (датацентровый ASN). Необходим резидентский IP или чистый прокси!")

                err_block = page.locator(":has-text('Invalid email domain'), :has-text('There was a problem signing up'), :has-text('problem signing up'), :has-text('There was a problem logging in'), :has-text('Login is not allowed'), :has-text('Signup is not allowed'), :has-text('Invalid email'), [role='alert']")
                if err_block.count() > 0 and err_block.first.is_visible():
                    err_txt = err_block.first.text_content().strip()
                    page.screenshot(path="scratch/notion_blocked_ip.png")
                    if "invalid email domain" in err_txt.lower():
                        raise RuntimeError(f"❌ Notion отклонил домен почты '{email}': 'Invalid email domain'. Временные почты этого сервиса в черном списке Notion!")
                    elif any(phrase in err_txt.lower() for phrase in ["problem signing up", "problem logging in", "not allowed", "something went wrong"]):
                        raise RuntimeError(f"❌ Notion заблокировал отправку кода: '{err_txt}'. Причина: блокировка IP Notion. Смените сервер VPN или используйте резидентский прокси!")
                    else:
                        raise RuntimeError(f"Notion отклонил email '{email}': {err_txt}")

                self._log("info", "Запрос кода отправлен, ожидаем входящее письмо...", {"step": 2})

                # ── Шаг 3: Получение и ввод кода ──────────────────────────────────
                mail_timeout = delays.get("mail_timeout_sec", 120)
                self._log("info", f"Ожидание проверочного кода (до {mail_timeout} сек)...", {"step": 3})
                code = self.provider.wait_for_notion_code(timeout_seconds=mail_timeout)
                if not code:
                    raise RuntimeError("Таймаут: проверочный код от Notion не получен вовремя.")

                self._log("info", f"Код подтверждения получен: {code}. Вводим в форму...", {"step": 3, "code": code})
                page.wait_for_timeout(1500)

                # Поиск поля для ввода кода
                code_sel = selectors.get("code_input", "input[placeholder*='code' i], input[autocomplete='one-time-code'], input[type='text']")
                try:
                    code_elem = page.wait_for_selector(code_sel, timeout=10000, state="visible")
                    if code_elem:
                        code_elem.fill(code)
                    else:
                        page.keyboard.type(code)
                except Exception:
                    page.keyboard.type(code)

                page.wait_for_timeout(800)
                page.keyboard.press("Enter")

                # Дополнительно кликаем Continue, если кнопка видна
                self._wait_and_click(page, "button:has-text('Continue with code'), button:has-text('Submit'), button:has-text('Continue')", timeout_sec=3)

                self._log("info", "Код отправлен. Проверяем принятие кода сервером Notion...", {"step": 4})
                page.wait_for_timeout(3500)

                # Проверяем возможные сообщения об ошибке кода
                try:
                    err_loc = page.locator(":has-text('Incorrect code'), :has-text('Invalid code'), :has-text('Code expired'), :has-text('is not valid'), :has-text('was incorrect')")
                    if err_loc.count() > 0 and err_loc.first.is_visible():
                        err_text = err_loc.first.text_content()
                        if any(phrase in err_text.lower() for phrase in ["incorrect code", "invalid code", "code expired", "is not valid", "was incorrect"]):
                            self._log("warning", f"Введенный код ({code}) отклонен Notion ('{err_text.strip()}'). Ожидаем актуальный код именно для {email}...")
                            fresh_code = self.provider.wait_for_notion_code(timeout_seconds=45)
                            if fresh_code and fresh_code != code:
                                self._log("info", f"Получен новый актуальный код: {fresh_code}. Вводим повторно...")
                                code_elem = page.locator(code_sel).first
                                if code_elem.is_visible():
                                    code_elem.fill(fresh_code)
                                    page.wait_for_timeout(800)
                                    page.keyboard.press("Enter")
                                    self._wait_and_click(page, "button:has-text('Continue with code'), button:has-text('Submit'), button:has-text('Continue')", timeout_sec=3)
                                    page.wait_for_timeout(3500)
                            else:
                                raise RuntimeError(f"Notion отклонил проверочный код '{code}': {err_text}")
                except Exception as e:
                    if "Notion отклонил" in str(e):
                        raise

                # ── Шаг 4: Интеллектуальный Онбординг (все экраны Notion) ───────────
                self._log("info", "Онбординг: Проходим экраны профиля, выбора Work и плана...", {"step": 4})
                if not self.headless:
                    page.bring_to_front()

                onboarding_success = False
                for step_attempt in range(40):
                    page.wait_for_timeout(1500)
                    cur_url = page.url
                    page_text = ""
                    try:
                        page_text = page.locator("body").text_content() or ""
                    except Exception:
                        pass

                    # Проверка: достигли ли мы уже рабочей области (sidebar / Settings)?
                    has_settings_nav = False
                    try:
                        has_settings_nav = (
                            page.locator("[aria-label*='Settings' i], .notion-sidebar, [data-tab-name='settings']").count() > 0
                            or page.locator(":has-text('Settings & members')").count() > 0
                        )
                    except Exception:
                        pass
                    if has_settings_nav and ("/onboarding" not in cur_url and "/signup" not in cur_url):
                        self._log("success", "✔ Успешно вышли в рабочее пространство Notion!", {"step": 5})
                        onboarding_success = True
                        break

                    # 0. Сброс всплывающих cookie-баннеров Notion
                    try:
                        c_btn = page.locator("button:has-text('Accept all'), button:has-text('Reject all'), button:has-text('Принять все'), button:has-text('Отклонить все')")
                        if c_btn.count() > 0 and c_btn.first.is_visible():
                            c_btn.first.click(timeout=1000)
                    except Exception:
                        pass

                    # 1. Экран предложения присоединиться к чужой организации (Workspaces you can join / Join your team)
                    # КРИТИЧЕСКИ ВАЖНО: Категорически отклоняем и всегда выбираем 'Create a new workspace'
                    if any(p in page_text.lower() for p in [
                        "workspaces you can join", "join your team", "join workspace",
                        "select a workspace", "join this workspace", "organisations you can join",
                        "зайти в организацию", "присоединиться к"
                    ]):
                        self._log("info", "Онбординг: Обнаружено предложение вступить в организацию. Отклоняем и выбираем 'Create a new workspace'...")
                        create_ws_sel = "button:has-text('Create a new workspace'), button:has-text('Create new workspace'), button:has-text('Create a workspace'), div[role='button']:has-text('Create a new workspace'), div[role='button']:has-text('Create new workspace'), :has-text('Create a new workspace'), :has-text('Create a workspace'), text='Create new workspace'"
                        if not self._wait_and_click(page, create_ws_sel, timeout_sec=4):
                            try:
                                page.locator("button:visible, [role='button']:visible, a:visible").filter(has_text="Create").first.click(timeout=2000)
                            except Exception:
                                pass
                        page.wait_for_timeout(1500)
                        continue

                    # 2. Экран "Customize your profile" / "Your name"
                    if any(p in page_text for p in ["Customize your profile", "This is how you will appear", "Your name"]):
                        self._log("info", "Онбординг: Экран 'Customize your profile'. Заполняем имя и подтверждаем...")
                        
                        # Способ 1: Установка значения через нативный DOM JS + отправка событий
                        page.evaluate("""() => {
                            const inps = Array.from(document.querySelectorAll("input")).filter(el => {
                                const t = (el.getAttribute("type") || "").toLowerCase();
                                return t !== "checkbox" && t !== "radio" && t !== "hidden" && el.offsetParent !== null;
                            });
                            for (const inp of inps) {
                                inp.focus();
                                if (!inp.value || inp.value.trim() === "") {
                                    inp.value = "Michael Martinez";
                                }
                                inp.dispatchEvent(new Event('input', { bubbles: true }));
                                inp.dispatchEvent(new Event('change', { bubbles: true }));
                            }
                        }""")

                        # Способ 2: Заполнение через Playwright ввод с клавиатуры
                        try:
                            inputs = page.locator("input:visible").all()
                            for inp in inputs:
                                inp_type = (inp.get_attribute("type") or "").lower()
                                if inp_type in ["checkbox", "radio", "hidden"]:
                                    continue
                                cur_val = inp.input_value()
                                if not cur_val or cur_val.strip() == "":
                                    inp.click(force=True)
                                    page.keyboard.type("Michael Martinez", delay=30)
                                    inp.press("Enter")
                                    break
                        except Exception:
                            pass

                        page.wait_for_timeout(500)

                        # Кликаем Continue / Continue ->
                        cont_btn_sel = "button:has-text('Continue'), div[role='button']:has-text('Continue'), button[type='submit'], [data-testid='continue-button'], :has-text('Continue')"
                        if not self._wait_and_click(page, cont_btn_sel, timeout_sec=4):
                            try:
                                page.locator("button:visible, [role='button']:visible").filter(has_text="Continue").first.click(timeout=2000)
                            except Exception:
                                page.keyboard.press("Enter")
                        page.wait_for_timeout(1500)
                        continue

                    # 3. Экран "How do you want to use Notion?" / "For work / With my team"
                    if any(p in page_text for p in ["How do you want to use", "For work", "With my team", "For my team"]):
                        self._log("info", "Онбординг: Выбираем 'For work' (для работы/команды)...")
                        work_clicked = self._wait_and_click(page, "text='For work', text='With my team', text='For my team', text='For work or business', [data-testid='onboarding-for-work']", timeout_sec=4)
                        if not work_clicked:
                            cards = page.locator("[role='radio'], [role='button']").all()
                            if cards:
                                try:
                                    cards[0].click()
                                except Exception:
                                    pass
                        page.wait_for_timeout(800)

                        # Обязательно нажимаем Continue после выбора 'For work'
                        self._log("info", "Онбординг: Нажимаем Continue после выбора 'For work'...")
                        cont_btn_sel = "button:has-text('Continue'), div[role='button']:has-text('Continue'), button[type='submit'], [data-testid='continue-button']"
                        if not self._wait_and_click(page, cont_btn_sel, timeout_sec=4):
                            try:
                                page.locator("button:visible, [role='button']:visible").filter(has_text="Continue").first.click(timeout=2000)
                            except Exception:
                                page.keyboard.press("Enter")
                        page.wait_for_timeout(1500)
                        continue

                    # 3.1 Экран "What kind of work do you do?" / "Tailor Notion to your routines"
                    if any(p in page_text.lower() for p in [
                        "what kind of work do you do",
                        "what kind of work",
                        "tailor notion to your routines"
                    ]):
                        self._log("info", "Онбординг: Экран 'What kind of work do you do?'. Выбираем категорию и жмем Continue...")

                        # 1. Выбираем любую категорию (Engineering, Product, Marketing, Founder, Design, Operations, Finance, Sales, Other)
                        selected_cat = None
                        try:
                            selected_cat = page.evaluate("""() => {
                                const categories = ['Engineering', 'Product', 'Marketing', 'Design', 'Founder', 'Operations', 'Finance', 'Sales', 'Other'];
                                const elements = Array.from(document.querySelectorAll('div, button, [role="button"], span, p'));
                                for (const cat of categories) {
                                    const match = elements.find(el => {
                                        if (el.offsetParent === null) return false;
                                        const txt = (el.innerText || el.textContent || '').trim();
                                        return txt.toLowerCase() === cat.toLowerCase();
                                    });
                                    if (match) {
                                        let target = match;
                                        while (target && target.parentElement && target.parentElement !== document.body) {
                                            if (target.getAttribute('role') === 'button' || target.tagName === 'BUTTON' || target.parentElement.children.length >= 4) {
                                                break;
                                            }
                                            target = target.parentElement;
                                        }
                                        (target || match).click();
                                        return cat;
                                    }
                                }
                                return null;
                            }""")
                        except Exception:
                            pass

                        if not selected_cat:
                            for cat in ["Engineering", "Product", "Marketing", "Founder", "Design", "Operations", "Finance", "Sales", "Other"]:
                                try:
                                    loc = page.locator(f":text-is('{cat}'), text='{cat}'").first
                                    if loc.is_visible():
                                        loc.click(timeout=1000)
                                        selected_cat = cat
                                        break
                                except Exception:
                                    pass

                        self._log("info", f"Онбординг: Выбрана категория '{selected_cat or 'Engineering'}'. Ожидаем Continue...")
                        page.wait_for_timeout(800)

                        # 2. Нажимаем кнопку 'Continue ->'
                        cont_clicked = False
                        try:
                            cont_clicked = page.evaluate("""() => {
                                const btns = Array.from(document.querySelectorAll('button, [role="button"], div')).filter(el => {
                                    if (el.offsetParent === null) return false;
                                    const t = (el.innerText || el.textContent || '').trim().toLowerCase();
                                    return t.startsWith('continue') || t.includes('continue');
                                });
                                if (btns.length > 0) {
                                    btns[btns.length - 1].click();
                                    return true;
                                }
                                return false;
                            }""")
                        except Exception:
                            pass

                        if not cont_clicked:
                            cont_selectors = [
                                "button:has-text('Continue')",
                                "div[role='button']:has-text('Continue')",
                                "[data-testid='continue-button']",
                                ":has-text('Continue →')",
                                "button[type='submit']"
                            ]
                            for c_sel in cont_selectors:
                                try:
                                    c_loc = page.locator(c_sel).last
                                    if c_loc.is_visible():
                                        c_loc.click(timeout=1500)
                                        cont_clicked = True
                                        break
                                except Exception:
                                    pass

                        if not cont_clicked:
                            page.keyboard.press("Enter")

                        page.wait_for_timeout(1500)
                        continue

                    # Дополнительные опросы о роли / сфере деятельности / размере команды
                    if any(p in page_text.lower() for p in ["what is your role", "what's your role", "how many people", "company size", "what will you use notion for"]):
                        self._log("info", "Онбординг: Опрос о роли/команде...")
                        if not self._wait_and_click(page, "button:has-text('Skip'), div[role='button']:has-text('Skip')", timeout_sec=2):
                            try:
                                page.evaluate("""() => {
                                    const opts = Array.from(document.querySelectorAll("[role='radio'], [role='option'], [role='button'], button")).filter(el => {
                                        return el.offsetParent !== null && !el.innerText.toLowerCase().includes('continue');
                                    });
                                    if (opts.length > 0) opts[0].click();
                                }""")
                            except Exception:
                                pass
                            options = page.locator("[role='radio']:visible, [role='option']:visible").all()
                            if options:
                                try:
                                    options[0].click()
                                except Exception:
                                    pass
                        page.wait_for_timeout(500)
                        self._wait_and_click(page, "button:has-text('Continue'), div[role='button']:has-text('Continue')", timeout_sec=4)
                        page.keyboard.press("Enter")
                        page.wait_for_timeout(1500)
                        continue

                    # 4. Экран ввода имени воркспейса
                    if any(p in page_text for p in ["name of your company", "name of your team", "Workspace name"]):
                        self._log("info", "Онбординг: Указываем имя воркспейса...")
                        ws_name = workspace_cfg.get("default_name", "Business Team")
                        name_inputs = page.locator("input[placeholder*='workspace' i], input[placeholder*='team' i], input[placeholder*='company' i], input[type='text']").all()
                        if name_inputs:
                            try:
                                name_inputs[0].fill(ws_name)
                            except Exception:
                                pass
                        self._wait_and_click(page, "button:has-text('Continue'), button:has-text('Next'), div[role='button']:has-text('Continue')", timeout_sec=4)
                        page.wait_for_timeout(1500)
                        continue

                    # 5. Экран "Who else is on your team?" / Приглашения команды
                    # СТРОГОЕ ТРЕБОВАНИЕ: Снимаем галочку привязки домена ('Anyone with @... can join your workspace')
                    if any(p in page_text for p in ["Who else is on your team", "Invite teammates", "Add your team members by email", "can join your workspace"]):
                        self._log("info", "Онбординг: Экран 'Who else is on your team'. Снимаем галочку привязки домена и жмем Continue...")
                        
                        # 1. Снимаем галочку через нативный JS в DOM
                        try:
                            page.evaluate("""() => {
                                const inputs = Array.from(document.querySelectorAll("input[type='checkbox']"));
                                for (const inp of inputs) {
                                    if (inp.checked) {
                                        inp.click();
                                    }
                                }
                                const roles = Array.from(document.querySelectorAll("[role='checkbox'][aria-checked='true']"));
                                for (const r of roles) {
                                    r.click();
                                }
                                const allElements = Array.from(document.querySelectorAll("*"));
                                for (const el of allElements) {
                                    if (el.textContent && el.textContent.includes("can join your workspace") && el.children.length === 0) {
                                        let parent = el.closest("div");
                                        if (parent) {
                                            const cb = parent.querySelector("input[type='checkbox'], [role='checkbox'], svg");
                                            if (cb) {
                                                if (cb.checked || cb.getAttribute('aria-checked') === 'true') {
                                                    cb.click();
                                                }
                                            }
                                        }
                                    }
                                }
                            }""")
                        except Exception:
                            pass

                        # 2. Дублируем снятие отметки через Playwright
                        try:
                            checkboxes = page.locator("input[type='checkbox']").all()
                            for cb in checkboxes:
                                try:
                                    if cb.is_checked():
                                        cb.uncheck(force=True)
                                except Exception:
                                    pass
                        except Exception:
                            pass

                        try:
                            custom_cb = page.locator("[role='checkbox'][aria-checked='true']").all()
                            for ccb in custom_cb:
                                try:
                                    ccb.click(force=True)
                                except Exception:
                                    pass
                        except Exception:
                            pass

                        page.wait_for_timeout(600)

                        # Нажимаем кнопку 'Continue ->'
                        self._log("info", "Онбординг: Нажимаем 'Continue ->'...")
                        cont_btn_sel = "button:has-text('Continue'), div[role='button']:has-text('Continue'), button[type='submit'], :has-text('Continue')"
                        if not self._wait_and_click(page, cont_btn_sel, timeout_sec=4):
                            try:
                                page.locator("button:visible, [role='button']:visible").filter(has_text="Continue").first.click(timeout=2000)
                            except Exception:
                                page.keyboard.press("Enter")

                        page.wait_for_timeout(1500)
                        continue

                    # 5. Экран выбора тарифного плана (Choose your Plan -> Выбираем Free через кнопку Continue)
                    if any(p.lower() in page_text.lower() for p in ["choose your plan", "select the best notion experience", "free trial"]):
                        self._log("info", "Онбординг: Экран 'Choose your Plan'. Нажимаем 'Continue' под тарифом Free...", {"step": 5})

                        free_clicked = False
                        try:
                            # Ищем карточку с Free и нажимаем Continue внутри неё
                            free_cards = page.locator("div").filter(has_text="Free").filter(has_text="1000 blocks").all()
                            for fc in free_cards:
                                btn = fc.locator("button:has-text('Continue'), div[role='button']:has-text('Continue')")
                                if btn.count() > 0 and btn.first.is_visible():
                                    btn.first.click()
                                    free_clicked = True
                                    break
                        except Exception:
                            pass

                        # Если не нашли по карточке — нажимаем кнопку Continue (у Business кнопка 'Try free for 30 days', а у Free — 'Continue')
                        if not free_clicked:
                            self._wait_and_click(page, "button:has-text('Continue'), div[role='button']:has-text('Continue')", timeout_sec=4)

                        page.wait_for_timeout(2500)
                        continue

                    # Экран предложения Desktop App ("Notion is 50% faster with the Desktop App")
                    if any(p in page_text for p in ["Desktop App", "faster with the Desktop App", "Get desktop app"]):
                        self._log("info", "Онбординг: Экран 'Desktop App'. Нажимаем 'Skip for now'...")
                        skip_clicked = self._wait_and_click(page, "button:has-text('Skip for now'), div[role='button']:has-text('Skip for now'), text='Skip for now'", timeout_sec=4)
                        if not skip_clicked:
                            try:
                                page.locator("button:visible, [role='button']:visible").filter(has_text="Skip for now").first.click(timeout=2000)
                            except Exception:
                                pass
                        page.wait_for_timeout(2000)
                        continue

                    # Экран бронирования звонка эксперта ("Start with a free Setup Session")
                    if any(p in page_text for p in ["Setup Session", "free Setup Session", "help with a Notion expert", "Звонок с экспертом"]):
                        self._log("info", "Онбординг: Экран 'Setup Session' (звонок с экспертом). Нажимаем 'Skip for now'...")
                        skip_clicked = self._wait_and_click(page, "button:has-text('Skip for now'), div[role='button']:has-text('Skip for now'), text='Skip for now'", timeout_sec=4)
                        if not skip_clicked:
                            try:
                                page.locator("button:visible, [role='button']:visible").filter(has_text="Skip for now").first.click(timeout=2000)
                            except Exception:
                                pass
                        page.wait_for_timeout(2000)
                        continue

                    # Экран предложения интеграций ("Start with your real work in Notion" -> "Start from scratch")
                    if any(p in page_text for p in ["Start with your real work", "Connect email", "Start from scratch"]):
                        self._log("info", "Онбординг: Экран 'Start with your real work'. Нажимаем 'Start from scratch'...")
                        sfs_clicked = self._wait_and_click(page, "button:has-text('Start from scratch'), div[role='button']:has-text('Start from scratch'), :has-text('Start from scratch')", timeout_sec=4)
                        if not sfs_clicked:
                            try:
                                page.locator("button:visible, [role='button']:visible").filter(has_text="Start from scratch").first.click(timeout=2000)
                            except Exception:
                                pass
                        page.wait_for_timeout(2000)
                        continue

                    # 6. Любые общие кнопки Skip / Start from scratch / Continue / Next / Take me to Notion / Got it
                    clicked_any = self._wait_and_click(page, "button:has-text('Start from scratch'), div[role='button']:has-text('Start from scratch'), button:has-text('Skip for now'), button:has-text('Skip'), button:has-text('Take me to Notion'), button:has-text('Get started'), button:has-text('Got it'), button:has-text('Continue'), div[role='button']:has-text('Continue')", timeout_sec=2)
                    if clicked_any:
                        page.wait_for_timeout(1500)
                        continue

                if not onboarding_success:
                    page.screenshot(path="scratch/onboarding_failed.png")
                    raise RuntimeError(f"Онбординг не завершился вовремя. Окно осталось на URL: {page.url}. Скриншот сохранен в scratch/onboarding_failed.png")

                page.wait_for_timeout(3000)

                # ── Шаг 6: Открытие боковой панели (сайдбара) и переход в Settings -> Teamspaces ──
                self._log("info", "Воркспейс создан. Открываем боковое меню (кнопка с тремя полосками ☰ слева вверху)...", {"step": 6})

                # 1. Проверяем, открыт ли сайдбар, и если свернут — нажимаем на три полоски (☰)
                sidebar_is_open = False
                try:
                    sb = page.locator(".notion-sidebar")
                    if sb.count() > 0 and sb.first.is_visible():
                        box = sb.first.bounding_box()
                        if box and box.get("width", 0) > 60:
                            sidebar_is_open = True
                except Exception:
                    pass

                if not sidebar_is_open:
                    self._log("info", "Сайдбар свернут. Кликаем на кнопку меню (три полоски ☰ слева вверху)...")
                    hamburger_selectors = [
                        "div[role='button'][aria-label*='sidebar' i]",
                        "button[aria-label*='sidebar' i]",
                        "[aria-label='Open sidebar']",
                        "[aria-label='Expand sidebar']",
                        ".notion-topbar div[role='button']:first-child",
                        "header div[role='button']:first-child",
                        "svg.hamburger",
                        "[data-testid='sidebar-toggle']"
                    ]
                    clicked_hamburger = False
                    for h_sel in hamburger_selectors:
                        if self._wait_and_click(page, h_sel, timeout_sec=2):
                            clicked_hamburger = True
                            break

                    if not clicked_hamburger:
                        try:
                            clicked_hamburger = page.evaluate("""() => {
                                const btn = document.querySelector("[aria-label*='sidebar' i], [aria-label*='Open sidebar' i], .notion-topbar [role='button'], svg.hamburger");
                                if (btn) {
                                    btn.click();
                                    return true;
                                }
                                return false;
                            }""")
                        except Exception:
                            pass

                    page.wait_for_timeout(1200)

                    # Если сайдбар все еще скрыт, посылаем горячую клавишу Control+\
                    try:
                        sb = page.locator(".notion-sidebar")
                        if sb.count() == 0 or not sb.first.is_visible():
                            page.keyboard.press("Control+\\")
                            page.wait_for_timeout(1500)
                    except Exception:
                        pass

                # 2. В открывшемся сайдбаре кликаем по названию воркспейса (меню воркспейса вверху)
                self._log("info", "Открываем меню воркспейса в сайдбаре...")
                ws_clicked = False
                for _ in range(3):
                    try:
                        ws_drop = page.locator(".notion-sidebar-switcher, .notion-sidebar div[role='button']").first
                        if ws_drop.is_visible():
                            ws_drop.click()
                            ws_clicked = True
                            page.wait_for_timeout(1000)
                            break
                    except Exception:
                        pass
                    page.wait_for_timeout(800)

                if not ws_clicked:
                    self._wait_and_click(page, ".notion-sidebar-switcher, .notion-sidebar div[role='button']", timeout_sec=3)
                    page.wait_for_timeout(1000)

                # 3. В выпадающем меню кликаем 'Settings'
                self._log("info", "Выбираем пункт 'Settings' в меню воркспейса...")
                set_clicked = False
                try:
                    set_item = page.locator("div[role='menuitem'], div[role='button']").filter(has_text="Settings").first
                    if set_item.is_visible():
                        set_item.click()
                        set_clicked = True
                        page.wait_for_timeout(2000)
                except Exception:
                    pass

                if not set_clicked:
                    # Запасной вариант - комбинация Control+,
                    page.keyboard.press("Control+,")
                    page.wait_for_timeout(2000)

                # 4. Закрываем cookie баннер через кнопку (Reject all / Accept all) без удаления узлов DOM
                try:
                    c_btn = page.locator("button:has-text('Reject all'), button:has-text('Accept all')").first
                    if c_btn.is_visible():
                        c_btn.click(timeout=1000)
                except Exception:
                    pass

                # 5. Выбираем раздел Teamspaces в левой колонке настроек
                self._log("info", "Выбираем вкладку 'Teamspaces'...")
                ts_clicked = False
                try:
                    ts_item = page.locator("div[role='button']:has-text('Teamspaces'), div[role='tab']:has-text('Teamspaces'), [data-tab-name='teamspaces']").first
                    if ts_item.is_visible():
                        ts_item.click()
                        ts_clicked = True
                except Exception:
                    pass

                if not ts_clicked:
                    try:
                        ts_fallback = page.locator("text='Teamspaces'").last
                        if ts_fallback.is_visible():
                            ts_fallback.click(force=True)
                            ts_clicked = True
                    except Exception:
                        pass

                if not ts_clicked:
                    teamspaces_tab_sel = selectors.get("teamspaces_tab", "div[role='button']:has-text('Teamspaces'), div[role='tab']:has-text('Teamspaces'), :has-text('Teamspaces'), [data-tab-name='teamspaces']")
                    self._wait_and_click(page, teamspaces_tab_sel, timeout_sec=6)

                page.wait_for_timeout(2000)

                # ── Шаг 7: Создание Teamspace и активация Business Trial ─────
                self._log("info", "Создаем Teamspace и активируем Business Trial...", {"step": 7})

                # Кликаем синюю кнопку "New teamspace" (скриншот 3)
                new_ts_sel = selectors.get("new_teamspace_btn", "button:has-text('New teamspace'), div[role='button']:has-text('New teamspace'), :has-text('New teamspace')")
                new_ts_clicked = self._wait_and_click(page, new_ts_sel, timeout_sec=5)
                if not new_ts_clicked:
                    try:
                        page.locator("button:visible, [role='button']:visible").filter(has_text="New teamspace").first.click(timeout=3000)
                    except Exception:
                        pass
                page.wait_for_timeout(1500)

                # Заполняем имя teamspace при наличии поля ввода
                try:
                    ts_name_inps = page.locator("input[placeholder*='Engineering' i], input[placeholder*='name' i], input[placeholder*='teamspace' i]").all()
                    if ts_name_inps:
                        ts_name_inps[0].fill("Workspace Team")
                except Exception:
                    pass

                # Открываем выпадающий список Security (блок выбора доступа Open / Default)
                self._log("info", "Открываем выпадающий список Security...")
                sec_opened = False
                try:
                    # Ищем контейнер выбора доступа под меткой Security (в скриншоте это блок с текстом 'Open')
                    sec_dropdown_loc = page.locator("div:has-text('Security') ~ div, div[role='button']:has-text('Open'), div[role='button']:has-text('Default')").first
                    if sec_dropdown_loc.is_visible():
                        sec_dropdown_loc.click()
                        sec_opened = True
                except Exception:
                    pass

                if not sec_opened:
                    try:
                        sec_label = page.locator("text='Security'").last
                        if sec_label.is_visible():
                            parent = sec_label.locator("..")
                            drop = parent.locator("div[role='button'], div[tabindex='0']").first
                            if drop.is_visible():
                                drop.click()
                                sec_opened = True
                    except Exception:
                        pass

                page.wait_for_timeout(1000)

                # Теперь нажимаем строго кнопку 'Try for free' внизу выпадающего списка
                self._log("info", "Нажимаем кнопку 'Try for free' внизу выпадающего списка...")
                modal_opened = False

                for attempt in range(5):
                    # 1. Прямой клик мыши по физическим координатам элемента 'Try for free'
                    try:
                        tf_elem = page.locator(":text('Try for free')").last
                        if tf_elem.is_visible():
                            box = tf_elem.bounding_box()
                            if box and box["width"] > 10 and box["height"] > 10:
                                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                                page.wait_for_timeout(1200)
                                if page.locator("text='Start free trial'").count() > 0:
                                    modal_opened = True
                                    self._log("info", "✔ Окно триала открыто кликом по координатам 'Try for free'!")
                                    break
                    except Exception:
                        pass

                    # 2. Прямой клик через Playwright get_by_text
                    if not modal_opened:
                        try:
                            page.get_by_text("Try for free").last.click(force=True, timeout=1500)
                            page.wait_for_timeout(1200)
                            if page.locator("text='Start free trial'").count() > 0:
                                modal_opened = True
                                self._log("info", "✔ Окно триала открыто через get_by_text('Try for free')!")
                                break
                        except Exception:
                            pass

                    # 3. Нативный JS MouseEvent dispatch по 'Try for free'
                    if not modal_opened:
                        try:
                            modal_opened = page.evaluate("""() => {
                                const all = Array.from(document.querySelectorAll("*"));
                                for (let i = all.length - 1; i >= 0; i--) {
                                    const el = all[i];
                                    if (el.textContent && el.textContent.includes("Try for free")) {
                                        const rect = el.getBoundingClientRect();
                                        if (rect.width > 20 && rect.height > 10) {
                                            const target = el.closest("[role='button'], [role='menuitem']") || el.parentElement || el;
                                            target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
                                            target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
                                            target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                                            target.click();
                                            return true;
                                        }
                                    }
                                }
                                return false;
                            }""")
                            page.wait_for_timeout(1200)
                            if page.locator("text='Start free trial'").count() > 0:
                                modal_opened = True
                                self._log("info", "✔ Окно триала открыто через DOM-клик 'Try for free'!")
                                break
                        except Exception:
                            pass

                    # 4. Клавиатурная навигация: стрелками вниз строго до 'Try for free' и Enter
                    if not modal_opened:
                        try:
                            page.keyboard.press("ArrowDown")
                            page.keyboard.press("ArrowDown")
                            page.keyboard.press("ArrowDown")
                            page.keyboard.press("ArrowDown")
                            page.wait_for_timeout(200)
                            page.keyboard.press("Enter")
                            page.wait_for_timeout(1200)
                            if page.locator("text='Start free trial'").count() > 0:
                                modal_opened = True
                                self._log("info", "✔ Окно триала открыто через ArrowDown + Enter!")
                                break
                        except Exception:
                            pass

                    page.wait_for_timeout(500)

                # Даем модальному окну полностью подгрузить JS-зависимости и офферы (subscriptionActionsLazyDeps)
                page.wait_for_timeout(3500)

                # В открывшемся модальном окне 'Try private teamspaces on the free Business trial' нажимаем 'Start free trial'
                self._log("info", "Нажимаем синюю кнопку 'Start free trial'...")
                trial_clicked = False

                for attempt in range(4):
                    try:
                        st_loc = page.locator("div[role='button']:has-text('Start free trial'), button:has-text('Start free trial'), :text('Start free trial')").last
                        if st_loc.is_visible():
                            box_st = st_loc.bounding_box()
                            if box_st and box_st["width"] > 20:
                                page.mouse.click(box_st["x"] + box_st["width"] / 2, box_st["y"] + box_st["height"] / 2)
                                trial_clicked = True
                                break
                            else:
                                st_loc.click(force=True)
                                trial_clicked = True
                                break
                    except Exception:
                        pass

                    if not trial_clicked:
                        try:
                            trial_clicked = page.evaluate("""() => {
                                const btns = Array.from(document.querySelectorAll("button, div[role='button'], div"));
                                for (let i = btns.length - 1; i >= 0; i--) {
                                    const b = btns[i];
                                    if (b.textContent && b.textContent.trim().includes("Start free trial")) {
                                        const rect = b.getBoundingClientRect();
                                        if (rect.width > 30 && rect.height > 15) {
                                            b.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
                                            b.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
                                            b.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                                            b.click();
                                            return true;
                                        }
                                    }
                                }
                                return false;
                            }""")
                            if trial_clicked:
                                break
                        except Exception:
                            pass

                    page.wait_for_timeout(1000)

                # Ожидание ответа сервера и синхронизации
                self._log("info", "Ожидание активации триала серверами Notion...", {"step": 7})
                start_trial_wait = time.time()
                while time.time() - start_trial_wait < 25:
                    # 1. Проверяем, не выскочило ли окно ошибки "Something went wrong"
                    try:
                        err_box = page.locator("div[role='dialog']:has-text('Something went wrong'), :has-text('Something went wrong.OK')")
                        if err_box.count() > 0 and err_box.first.is_visible():
                            self._log("warning", "⚠️ Появился диалог 'Something went wrong'. Сбрасываем модальное окно для обновления Turnstile токена...")
                            ok_btn = page.locator("div[role='button']:has-text('OK'), button:has-text('OK'), text='OK'").last
                            if ok_btn.is_visible():
                                ok_btn.click()
                                page.wait_for_timeout(1000)
                            
                            # Закрываем модальное окно через 'Maybe later' или Escape
                            maybe_later = page.locator("div[role='button']:has-text('Maybe later'), button:has-text('Maybe later')").first
                            if maybe_later.is_visible():
                                maybe_later.click()
                            else:
                                page.keyboard.press("Escape")
                            page.wait_for_timeout(1500)

                            # Повторно открываем 'Try for free'
                            tf_elem = page.locator(":text('Try for free')").last
                            if tf_elem.is_visible():
                                tf_elem.click(force=True)
                                page.wait_for_timeout(3000)
                                st_retry = page.locator("div[role='button']:has-text('Start free trial'), button:has-text('Start free trial'), :text('Start free trial')").last
                                if st_retry.is_visible():
                                    st_retry.click(force=True)
                                    page.wait_for_timeout(3000)
                    except Exception:
                        pass

                    # 2. Проверяем, закрылось ли модальное окно триала
                    try:
                        modal_visible = page.locator("text='Try private teamspaces on the free Business trial'").count() > 0
                        if not modal_visible:
                            self._log("success", "✔ Модальное окно триала успешно закрылось! Синхронизация завершена.", {"step": 7})
                            break
                    except Exception:
                        pass

                    page.wait_for_timeout(1500)

                # ── Шаг 8: Извлечение учетных данных и сохранение в пул ─────────────
                self._log("info", "Извлекаем cookies и сессию...", {"step": 8})

                urls_to_check = [
                    "https://www.notion.so",
                    "https://notion.so",
                    "https://app.notion.com",
                    "https://notion.com"
                ]
                cookies_list = context.cookies(urls_to_check)
                if not cookies_list:
                    cookies_list = context.cookies()

                cookies_dict = {c["name"]: c["value"] for c in cookies_list}
                all_cookie_names = list(cookies_dict.keys())
                self._log("info", f"Обнаружено cookies ({len(cookies_list)}): {all_cookie_names}")

                token_v2 = cookies_dict.get("token_v2") or captured_session.get("token_v2")
                notion_user_id = cookies_dict.get("notion_user_id") or captured_session.get("notion_user_id")

                # Проверяем альтернативные имена cookies с token_v2
                if not token_v2:
                    for k, v in cookies_dict.items():
                        if "token_v2" in k.lower():
                            token_v2 = v
                            break

                # Если token_v2 все еще не найден, синхронизируем через www.notion.so
                if not token_v2:
                    self._log("warning", "token_v2 не найден в app.notion.com, синхронизируем сессию через notion.so...")
                    try:
                        page.goto("https://www.notion.so/", wait_until="commit", timeout=15000)
                        page.wait_for_timeout(2500)
                        c_list_so = context.cookies(["https://www.notion.so", "https://notion.so"])
                        for c in c_list_so:
                            cookies_dict[c["name"]] = c["value"]
                        token_v2 = cookies_dict.get("token_v2") or captured_session.get("token_v2")
                        notion_user_id = cookies_dict.get("notion_user_id") or captured_session.get("notion_user_id")
                    except Exception as e:
                        self._log("warning", f"Попытка перехода на notion.so: {e}")

                # Если все еще не найден, проверяем localStorage
                if not token_v2:
                    try:
                        ls_token = page.evaluate("""() => {
                            try {
                                return localStorage.getItem('token_v2') || 
                                       localStorage.getItem('notion_token_v2') ||
                                       localStorage.getItem('token');
                            } catch(e) { return null; }
                        }""")
                        if ls_token:
                            token_v2 = ls_token
                    except Exception:
                        pass

                if not token_v2:
                    page.screenshot(path="debug_missing_token.png")
                    raise RuntimeError(
                        f"Не удалось получить cookie 'token_v2' из сессии браузера. "
                        f"Доступные cookies: {all_cookie_names}. "
                        f"Сетевой перехват: {list(captured_session.keys())}. "
                        f"URL: {page.url}"
                    )

                cookies_dict["token_v2"] = token_v2
                if notion_user_id:
                    cookies_dict["notion_user_id"] = notion_user_id

                space_info = self._extract_space_info(token_v2, notion_user_id, cookies_dict)

                space_id = space_info.get("space_id", "")
                space_name = space_info.get("space_name", "Business Workspace")
                user_name = space_info.get("user_name", "Notion User")
                space_view_id = space_info.get("space_view_id", "")

                file_path = save_account(
                    token_v2=token_v2,
                    user_id=notion_user_id or "",
                    space_id=space_id,
                    user_email=email,
                    user_name=user_name,
                    space_name=space_name,
                    space_view_id=space_view_id,
                    plan_type="business_trial",
                    cookies=cookies_dict,
                    extra={
                        "subscription_tier": space_info.get("subscription_tier", "business"),
                        "trial_status": "active",
                        "trial_duration_days": 30,
                        "preset_used": self.preset.get("id"),
                        "registered_at": time.strftime("%Y-%m-%d %H:%M:%S")
                    }
                )

                self._log("info", f"🎉 Аккаунт успешно создан и сохранен в пул: {file_path}", {
                    "step": 8,
                    "email": email,
                    "token_v2": token_v2[:15] + "...",
                    "space_id": space_id,
                    "plan": "Business Trial (30 дней)",
                    "file_path": file_path
                })

                if not self.headless:
                    self._log("success", "✔ Регистрация и активация завершены. Окно браузера открыто еще 4 секунды...")
                    page.wait_for_timeout(4000)

                return {
                    "success": True,
                    "email": email,
                    "token_v2": token_v2,
                    "user_id": notion_user_id,
                    "space_id": space_id,
                    "space_name": space_name,
                    "file_path": file_path,
                    "plan_type": "business_trial"
                }

            except Exception as e:
                self._log("error", f"Ошибка во время регистрации: {e}", {"error": str(e)})
                try:
                    page.screenshot(path="scratch/last_debug_screen.png")
                except Exception:
                    pass
                if not self.headless:
                    self._log("warning", "⚠️ Окно браузера оставлено открытым на 25 секунд для визуального осмотра...")
                    for sec in range(25, 0, -5):
                        self._log("info", f"Окно закроется через {sec} сек...")
                        page.wait_for_timeout(5000)
                raise
            finally:
                try:
                    if browser:
                        browser.close()
                    elif context:
                        context.close()
                except Exception:
                    pass
                try:
                    if chrome_proc:
                        chrome_proc.terminate()
                        chrome_proc.wait(timeout=3)
                except Exception:
                    try:
                        if chrome_proc:
                            chrome_proc.kill()
                    except Exception:
                        pass

    def _extract_space_info(self, token_v2: str, user_id: Optional[str], cookies: Dict[str, str]) -> Dict[str, str]:
        url = "https://www.notion.so/api/v3/loadUserContent"
        cookies_copy = dict(cookies)
        cookies_copy["token_v2"] = token_v2
        if user_id:
            cookies_copy["notion_user_id"] = user_id
        cookie_header = "; ".join([f"{k}={v}" for k, v in cookies_copy.items()])
        headers = {
            "Content-Type": "application/json",
            "Cookie": cookie_header,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "x-notion-active-user-header": user_id or ""
        }

        req = urllib.request.Request(url, data=b"{}", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                record_map = data.get("recordMap", {})
                spaces = record_map.get("space", {})
                space_id = ""
                space_name = "Business Workspace"
                plan = "business_trial"
                for sid, sval in spaces.items():
                    val = sval.get("value", {})
                    if isinstance(val, dict) and "value" in val:
                        val = val["value"]
                    space_id = sid
                    space_name = val.get("name") or "Business Workspace"
                    plan = val.get("plan_type") or val.get("subscription_tier") or "business_trial"
                    tier = val.get("subscription_tier") or "business"
                    break

                space_views = record_map.get("space_view", {})
                space_view_id = ""
                for svid in space_views:
                    space_view_id = svid
                    break

                user_name = "Notion User"
                users = record_map.get("notion_user", {})
                for uid, uval in users.items():
                    val = uval.get("value", {})
                    if isinstance(val, dict) and "value" in val:
                        val = val["value"]
                    if val.get("name"):
                        user_name = val["name"]
                        break
                    given = val.get("given_name", "")
                    family = val.get("family_name", "")
                    if given or family:
                        user_name = f"{given} {family}".strip()
                        break

                return {
                    "space_id": space_id,
                    "space_name": space_name,
                    "space_view_id": space_view_id,
                    "user_name": user_name,
                    "plan": plan,
                    "subscription_tier": tier
                }
        except Exception as e:
            self._log("warning", f"Не удалось получить данные через loadUserContent: {e}")
            return {}
