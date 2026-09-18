import re
import time
import json
import random
import string
import urllib.request
import urllib.error
import imaplib
import email
import threading
from typing import Optional, Tuple
from pathlib import Path

_email_gen_lock = threading.Lock()


class MailProvider:
    """Базовый класс почтового провайдера"""
    def get_email(self) -> str:
        raise NotImplementedError

    def wait_for_notion_code(self, timeout_seconds: int = 120, check_interval: int = 3) -> Optional[str]:
        raise NotImplementedError


class MailTmProvider(MailProvider):
    """
    Автоматический провайдер временной почты на базе Mail.tm.
    Генерирует чистый почтовый ящик и автоматически извлекает код подтверждения от Notion.
    """
    BASE_URL = "https://api.mail.tm"

    def __init__(self):
        self.email: Optional[str] = None
        self.password: str = "P@ssw0rdNotion2026!"
        self.token: Optional[str] = None
        self.account_id: Optional[str] = None
        self._init_account()

    def _make_request(self, endpoint: str, data: Optional[dict] = None, headers: Optional[dict] = None) -> dict:
        url = f"{self.BASE_URL}{endpoint}"
        req_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        }
        if headers:
            req_headers.update(headers)

        payload = None
        if data is not None:
            req_headers["Content-Type"] = "application/json"
            payload = json.dumps(data).encode("utf-8")

        req = urllib.request.Request(url, data=payload, headers=req_headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _init_account(self):
        # 1. Получаем доступные домены
        domains_resp = self._make_request("/domains")
        if isinstance(domains_resp, list):
            domains_list = domains_resp
        elif isinstance(domains_resp, dict):
            domains_list = domains_resp.get("hydra:member", [])
        else:
            domains_list = []

        domains = [d["domain"] for d in domains_list if isinstance(d, dict) and d.get("isActive", True)]
        if not domains:
            raise RuntimeError("Не удалось получить активные домены от Mail.tm")

        chosen_domain = random.choice(domains)
        prefix = "ntn_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        self.email = f"{prefix}@{chosen_domain}"

        # 2. Регистрируем аккаунт
        reg_data = {"address": self.email, "password": self.password}
        acc_resp = self._make_request("/accounts", data=reg_data)
        if isinstance(acc_resp, dict):
            self.account_id = acc_resp.get("id")

        # 3. Получаем токен авторизации
        token_resp = self._make_request("/token", data=reg_data)
        if isinstance(token_resp, dict):
            self.token = token_resp.get("token")
        else:
            raise RuntimeError("Не удалось авторизоваться в Mail.tm")

    def get_email(self) -> str:
        return self.email

    def wait_for_notion_code(self, timeout_seconds: int = 120, check_interval: int = 3) -> Optional[str]:
        """Ожидает входящее письмо от Notion и извлекает проверочный код (6 цифр)"""
        if not self.token:
            raise RuntimeError("Mail.tm токен отсутствует")

        headers = {"Authorization": f"Bearer {self.token}"}
        start_time = time.time()

        while time.time() - start_time < timeout_seconds:
            try:
                msgs_resp = self._make_request("/messages", headers=headers)
                if isinstance(msgs_resp, list):
                    messages = msgs_resp
                elif isinstance(msgs_resp, dict):
                    messages = msgs_resp.get("hydra:member", [])
                else:
                    messages = []

                for msg in messages:
                    if not isinstance(msg, dict):
                        continue
                    msg_id = msg.get("id")
                    subject = msg.get("subject", "")
                    sender = msg.get("from", {}).get("address", "").lower() if isinstance(msg.get("from"), dict) else ""

                    # Проверяем, что письмо связано с кодом авторизации / Notion
                    if "notion" in sender or "code" in subject.lower() or "notion" in subject.lower() or "login" in subject.lower() or True:
                        msg_detail = self._make_request(f"/messages/{msg_id}", headers=headers)
                        if isinstance(msg_detail, dict):
                            content = (msg_detail.get("text") or "") + " " + (msg_detail.get("intro") or "") + " " + subject

                            # Ищем 6-значный код
                            matches = re.findall(r"\b(\d{6})\b", content)
                            if matches:
                                return matches[0]

                            # Альтернативный поиск кода из HTML
                            html_content = msg_detail.get("html", "")
                            if html_content:
                                matches_html = re.findall(r"\b(\d{6})\b", html_content)
                                if matches_html:
                                    return matches_html[0]

            except Exception:
                pass

            time.sleep(check_interval)

        return None


class ManualMailProvider(MailProvider):
    """
    Провайдер для ручного ввода email и кода подтверждения.
    """
    def __init__(self, email: str, code_getter_callback=None):
        self.email = email.strip()
        self.code_getter_callback = code_getter_callback

    def get_email(self) -> str:
        return self.email

    def wait_for_notion_code(self, timeout_seconds: int = 180, check_interval: int = 2) -> Optional[str]:
        if self.code_getter_callback:
            return self.code_getter_callback(timeout_seconds)
        return None


class ImapMailProvider(MailProvider):
    """
    Автоматический провайдер для чтения кодов Notion по IMAP (Gmail, Outlook, Firstmail, Rambler и др.)
    """
    def __init__(
        self,
        target_email: str,
        imap_user: str,
        imap_password: str,
        imap_server: str = "imap.gmail.com",
        imap_port: int = 993,
        log_callback=None
    ):
        self.target_email = target_email.strip()
        self.imap_user = imap_user.strip()
        # Очищаем пароль приложения от возможных случайных пробелов (Google выдает 'xxxx xxxx xxxx xxxx')
        self.imap_password = imap_password.replace(" ", "").strip()
        self.imap_server = imap_server.strip() or "imap.gmail.com"
        self.imap_port = int(imap_port)
        self.log_callback = log_callback or (lambda msg: None)

    def get_email(self) -> str:
        return self.target_email

    def _extract_body_text(self, msg) -> str:
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition"))
                if content_type in ["text/plain", "text/html"] and "attachment" not in content_disposition:
                    payload = part.get_payload(decode=True)
                    if payload:
                        body += " " + payload.decode(errors="ignore")
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode(errors="ignore")
        return body

    def wait_for_notion_code(self, timeout_seconds: int = 180, check_interval: float = 1.0) -> Optional[str]:
        from email.utils import parsedate_to_datetime
        start_time = time.time()
        self.log_callback(f"Подключение к IMAP {self.imap_server}:{self.imap_port} (пользователь: {self.imap_user})...")

        known_stale_codes = set()
        target_clean = self.target_email.lower().strip()

        def _connect():
            conn = imaplib.IMAP4_SSL(self.imap_server, self.imap_port, timeout=15)
            conn.login(self.imap_user, self.imap_password)
            return conn

        mail = None
        try:
            mail = _connect()
            self.log_callback(f"✔ IMAP соединение активно. Ожидаем письмо для '{target_clean}'...")
        except Exception as e:
            self.log_callback(f"Ошибка первичного подключения к IMAP: {e}")

        last_spam_check = 0

        try:
            while time.time() - start_time < timeout_seconds:
                if mail is None:
                    try:
                        mail = _connect()
                    except Exception as e:
                        self.log_callback(f"Переподключение к IMAP: {e}")
                        time.sleep(check_interval)
                        continue

                folders_to_check = ["INBOX"]
                now = time.time()
                # Если прошло больше 12 сек, периодически также проверяем Spam
                if (now - start_time > 12) and (now - last_spam_check > 10):
                    last_spam_check = now
                    if "gmail" in self.imap_server.lower():
                        folders_to_check.append('"[Gmail]/Spam"')
                    else:
                        folders_to_check.append("Spam")

                for folder in folders_to_check:
                    try:
                        sel_st, _ = mail.select(folder, readonly=True)
                        if sel_st != "OK":
                            continue
                    except Exception:
                        try:
                            mail.close()
                            mail.logout()
                        except Exception:
                            pass
                        mail = None
                        break

                    # 1. Быстрый целенаправленный серверный поиск писем от Notion
                    mids = []
                    try:
                        st, s_data = mail.search(None, '(OR (FROM "Notion") (SUBJECT "Notion"))')
                        if st == "OK" and s_data[0]:
                            mids = s_data[0].split()
                    except Exception:
                        try:
                            st, s_data = mail.search(None, 'FROM "Notion"')
                            if st == "OK" and s_data[0]:
                                mids = s_data[0].split()
                        except Exception:
                            pass

                    if not mids:
                        # Запасной вариант: 5 последних сообщений
                        try:
                            st_all, s_data_all = mail.search(None, 'ALL')
                            if st_all == "OK" and s_data_all[0]:
                                mids = s_data_all[0].split()[-5:]
                        except Exception:
                            pass

                    # Проверяем строго от самых новых к старым (максимум последние 6 писем)
                    for mid in reversed(mids[-6:]):
                        try:
                            status, msg_data = mail.fetch(mid, "(RFC822)")
                            if status != "OK" or not msg_data:
                                continue

                            raw_email = msg_data[0][1]
                            if not isinstance(raw_email, bytes):
                                continue

                            parsed_msg = email.message_from_bytes(raw_email)
                            sender = str(parsed_msg.get("From", "")).lower()
                            subject = str(parsed_msg.get("Subject", "")).lower()
                            date_hdr = str(parsed_msg.get("Date", ""))
                            to_hdr = str(parsed_msg.get("To", "")).lower()
                            delivered_to = str(parsed_msg.get("Delivered-To", "")).lower()
                            x_forwarded = str(parsed_msg.get("X-Forwarded-To", "")).lower()
                            x_original = str(parsed_msg.get("X-Original-To", "")).lower()

                            if "notion" in sender or "notion" in subject or "login" in subject or "code" in subject or "verification" in subject or "temporary" in subject:
                                body_text = self._extract_body_text(parsed_msg) + " " + subject

                                if target_clean:
                                    recipient_headers = f"{to_hdr} {delivered_to} {x_forwarded} {x_original}"
                                    if target_clean not in recipient_headers and target_clean not in body_text.lower():
                                        continue

                                matches = re.findall(r"\b(\d{6})\b", body_text)
                                if matches:
                                    candidate_code = matches[0]
                                    if candidate_code in known_stale_codes:
                                        continue

                                    if date_hdr:
                                        try:
                                            msg_dt = parsedate_to_datetime(date_hdr)
                                            msg_ts = msg_dt.timestamp()
                                            if msg_ts < (start_time - 35):
                                                known_stale_codes.add(candidate_code)
                                                continue
                                        except Exception:
                                            pass

                                    self.log_callback(f"📬 Получен код от Notion: {candidate_code} (письмо в {folder} от {date_hdr}, адресат: {to_hdr or self.target_email})")
                                    return candidate_code
                        except Exception:
                            continue

                time.sleep(check_interval)

        finally:
            if mail:
                try:
                    mail.close()
                    mail.logout()
                except Exception:
                    pass

        return None


def generate_next_dot_email(base_email: str, existing_emails: list[str]) -> str:
    """
    Генерирует следующую уникальную комбинацию точек для base_email,
    которой еще нет в списке existing_emails и .used_emails.json.
    """
    if "@" not in base_email:
        return base_email
    local_part, domain = base_email.split("@", 1)
    clean_local = local_part.replace(".", "")
    n = len(clean_local)
    if n <= 1:
        return base_email

    with _email_gen_lock:
        existing_set = set(e.lower().strip() for e in existing_emails)

        # Загружаем также историю из accounts/.used_emails.json
        used_file = Path(__file__).resolve().parent.parent / "accounts" / ".used_emails.json"
        if used_file.exists():
            try:
                with open(used_file, "r", encoding="utf-8") as f:
                    used_list = json.load(f)
                    for u in used_list:
                        existing_set.add(u.lower().strip())
            except Exception:
                pass

        total_combinations = 1 << (n - 1)
        for mask in range(total_combinations):
            chars = []
            for i in range(n - 1):
                chars.append(clean_local[i])
                if (mask >> i) & 1:
                    chars.append(".")
            chars.append(clean_local[-1])
            candidate = f"{''.join(chars)}@{domain}".lower()
            if candidate not in existing_set:
                try:
                    used_file.parent.mkdir(parents=True, exist_ok=True)
                    existing_used = []
                    if used_file.exists():
                        with open(used_file, "r", encoding="utf-8") as f:
                            existing_used = json.load(f)
                    if candidate not in existing_used:
                        existing_used.append(candidate)
                        with open(used_file, "w", encoding="utf-8") as f:
                            json.dump(existing_used, f, indent=2)
                except Exception:
                    pass
                return candidate

        return f"{clean_local}@{domain}"


FIRST_NAMES = [
    "alex", "david", "michael", "james", "robert", "john", "william", "richard", "thomas", "charles",
    "daniel", "matthew", "anthony", "mark", "paul", "steven", "andrew", "joshua", "kevin", "brian",
    "george", "edward", "timothy", "jason", "jeffrey", "ryan", "jacob", "gary", "nicholas", "eric",
    "jonathan", "stephen", "larry", "justin", "scott", "brandon", "benjamin", "samuel", "gregory", "frank",
    "elena", "sarah", "olivia", "emma", "anna", "maria", "clara", "sophie", "laura", "julia"
]

LAST_NAMES = [
    "smith", "johnson", "williams", "brown", "jones", "garcia", "miller", "davis", "rodriguez", "martinez",
    "hernandez", "lopez", "gonzalez", "wilson", "anderson", "thomas", "taylor", "moore", "jackson", "martin",
    "lee", "perez", "thompson", "white", "harris", "sanchez", "clark", "ramirez", "lewis", "robinson",
    "walker", "young", "allen", "king", "wright", "scott", "torres", "hill", "flores", "green",
    "adams", "nelson", "baker", "hall", "rivera", "campbell", "mitchell", "carter", "roberts", "turner"
]


def generate_next_catchall_email(domain: str, style: str = "corporate", existing_emails: Optional[list] = None, save: bool = True) -> str:
    """
    Генерирует следующий уникальный корпоративный email под Catch-All домен.
    Стили:
    - 'corporate': alex.smith42@domain.com
    - 'short': ntn_a9b2@domain.com
    - 'tech': dev_lead8@domain.com
    """
    clean_domain = domain.strip().lower().lstrip("@")
    if not clean_domain:
        clean_domain = "company-notion.xyz"

    with _email_gen_lock:
        existing_set = set(e.lower().strip() for e in (existing_emails or []))

        used_file = Path(__file__).resolve().parent.parent / "accounts" / ".used_catchall.json"
        if used_file.exists():
            try:
                with open(used_file, "r", encoding="utf-8") as f:
                    for u in json.load(f):
                        existing_set.add(u.lower().strip())
            except Exception:
                pass

        for _ in range(1000):
            if style == "short":
                rand_suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
                candidate = f"ntn_{rand_suffix}@{clean_domain}"
            elif style == "tech":
                prefixes = ["dev", "lead", "eng", "ops", "pm", "qa", "arch", "sec", "data", "cloud"]
                p = random.choice(prefixes)
                num = random.randint(10, 999)
                candidate = f"{p}_{num}@{clean_domain}"
            else:  # corporate
                fn = random.choice(FIRST_NAMES)
                ln = random.choice(LAST_NAMES)
                num = random.randint(1, 99) if random.random() > 0.3 else ""
                sep = random.choice([".", "_", ""])
                candidate = f"{fn}{sep}{ln}{num}@{clean_domain}"

            if candidate not in existing_set:
                if save:
                    try:
                        used_file.parent.mkdir(parents=True, exist_ok=True)
                        existing_used = []
                        if used_file.exists():
                            with open(used_file, "r", encoding="utf-8") as f:
                                existing_used = json.load(f)
                        if candidate not in existing_used:
                            existing_used.append(candidate)
                            with open(used_file, "w", encoding="utf-8") as f:
                                json.dump(existing_used, f, indent=2)
                    except Exception:
                        pass
                return candidate

        rand_str = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        return f"notion_{rand_str}@{clean_domain}"


class CatchAllMailProvider(ImapMailProvider):
    """
    Провайдер для работы с собственным Catch-All доменом через пересылку в IMAP (Cloudflare / Gmail / Postfix).
    Отправляет в Notion уникальный корпоративный адрес (например: alex.smith42@mycorp.xyz),
    а входящий OTP-код считывает из целевого ящика IMAP.
    """
    def __init__(
        self,
        domain: str,
        imap_user: str,
        imap_password: str,
        style: str = "corporate",
        imap_server: str = "imap.gmail.com",
        imap_port: int = 993,
        log_callback=None
    ):
        self.domain = domain.strip().lower().lstrip("@")
        self.style = style
        generated_email = generate_next_catchall_email(self.domain, style=self.style)

        super().__init__(
            target_email=generated_email,
            imap_user=imap_user,
            imap_password=imap_password,
            imap_server=imap_server,
            imap_port=imap_port,
            log_callback=log_callback
        )


