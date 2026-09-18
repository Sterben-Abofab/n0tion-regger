import os
import subprocess
import time
import signal
import urllib.request
import yaml
from pathlib import Path
from typing import Optional, Dict, Any

ROOT_DIR = Path(__file__).resolve().parent.parent
EXE_PATH = ROOT_DIR / "notion-manager.exe"
CONFIG_PATH = ROOT_DIR / "config.yaml"


class NotionManagerProcess:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(NotionManagerProcess, cls).__new__(cls)
            cls._instance.process = None
            cls._instance.started_at = None
        return cls._instance

    def get_config(self) -> Dict[str, Any]:
        if CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
            except Exception as e:
                print(f"Ошибка чтения config.yaml: {e}")
        return {
            "server": {
                "port": 8081,
                "api_key": "",
                "admin_password": ""
            }
        }

    def is_running(self) -> bool:
        # Сначала проверяем доступность по HTTP health endpoint
        cfg = self.get_config()
        port = cfg.get("server", {}).get("port", 8081)
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass

        if self.process is not None:
            if self.process.poll() is None:
                return True
            else:
                self.process = None
        return False

    def start(self) -> bool:
        if self.is_running():
            return True

        if not EXE_PATH.exists():
            print(f"Бинарник {EXE_PATH} не найден!")
            return False

        try:
            # Запускаем в фоне
            self.process = subprocess.Popen(
                [str(EXE_PATH)],
                cwd=str(ROOT_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            self.started_at = time.time()
            time.sleep(1.0)  # Даем секунду на старт
            return self.is_running()
        except Exception as e:
            print(f"Не удалось запустить notion-manager.exe: {e}")
            return False

    def stop(self) -> bool:
        if self.process is not None:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None
            return True
        return False

    def get_status(self) -> Dict[str, Any]:
        running = self.is_running()
        cfg = self.get_config()
        server_cfg = cfg.get("server", {})
        port = server_cfg.get("port", 8081)
        api_key = server_cfg.get("api_key", "")

        return {
            "running": running,
            "port": port,
            "api_key": api_key,
            "base_url": f"http://localhost:{port}",
            "dashboard_url": f"http://localhost:{port}/dashboard/",
            "reverse_proxy_url": f"http://localhost:{port}/ai",
            "openai_api_url": f"http://localhost:{port}/v1/chat/completions",
            "anthropic_api_url": f"http://localhost:{port}/v1/messages"
        }
