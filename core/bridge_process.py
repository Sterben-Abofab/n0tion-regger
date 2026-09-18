import os
import sys
import subprocess
import time
import urllib.request
import json
from pathlib import Path
from typing import Optional, Dict, Any

ROOT_DIR = Path(__file__).resolve().parent.parent
BRIDGE_DIR = ROOT_DIR / "bridge"
RUNTIME_DIR = ROOT_DIR / "runtime"


class NotionBridgeProcess:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(NotionBridgeProcess, cls).__new__(cls)
            cls._instance.runtime_process = None
            cls._instance.bridge_process = None
            cls._instance.started_at = None
        return cls._instance

    def is_runtime_running(self) -> bool:
        try:
            req = urllib.request.Request("http://127.0.0.1:8787")
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status in (200, 404, 400):
                    return True
        except Exception:
            pass

        if self.runtime_process is not None:
            if self.runtime_process.poll() is None:
                return True
            self.runtime_process = None
        return False

    def is_bridge_running(self) -> bool:
        try:
            req = urllib.request.Request("http://127.0.0.1:8765/healthz")
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass

        if self.bridge_process is not None:
            if self.bridge_process.poll() is None:
                return True
            self.bridge_process = None
        return False

    def is_running(self) -> bool:
        # Мост является основным сервисом (инструменты автономно работают и без Node.js)
        return self.is_bridge_running()

    def start_runtime(self) -> bool:
        if self.is_runtime_running():
            return True

        import shutil
        if not shutil.which("node"):
            print("[BRIDGE] Node.js не установлен — автономные инструменты будут работать через нативный Python-движок.")
            return False

        server_js = RUNTIME_DIR / "server.js"
        if not server_js.exists():
            print(f"[BRIDGE] server.js не найден в {RUNTIME_DIR}")
            return False

        env = os.environ.copy()
        env_file = RUNTIME_DIR / ".env"
        if env_file.exists():
            try:
                for line in env_file.read_text(encoding="utf-8-sig").splitlines():
                    if line.lstrip().startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip("'\"")
            except Exception as e:
                print(f"[BRIDGE] Ошибка чтения runtime/.env: {e}")

        raw_root = env.get("CODE_ROOT", "")
        if not raw_root or not os.path.exists(raw_root):
            env["CODE_ROOT"] = str(Path.home())

        node_cmd = "node"
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

        try:
            self.runtime_process = subprocess.Popen(
                [node_cmd, "server.js"],
                cwd=str(RUNTIME_DIR),
                env=env,
                creationflags=creationflags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            time.sleep(0.5)
            return True
        except Exception as e:
            print(f"[BRIDGE] Ошибка запуска Node MCP Runtime: {e}")
            return False

    def start_bridge(self) -> bool:
        if self.is_bridge_running():
            return True

        server_py = BRIDGE_DIR / "server.py"
        if not server_py.exists():
            print(f"[BRIDGE] server.py не найден в {BRIDGE_DIR}")
            return False

        portable_py = ROOT_DIR / "runtime" / "python" / "python.exe"
        if portable_py.exists():
            python_exe = str(portable_py)
        else:
            python_exe = sys.executable

        env = os.environ.copy()
        env["PYTHONPATH"] = f"{str(BRIDGE_DIR)};{env.get('PYTHONPATH', '')}"

        creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

        try:
            self.bridge_process = subprocess.Popen(
                [python_exe, "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", "8765"],
                cwd=str(BRIDGE_DIR),
                env=env,
                creationflags=creationflags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            time.sleep(1.0)
            return True
        except Exception as e:
            print(f"[BRIDGE] Ошибка запуска FastAPI Bridge: {e}")
            return False

    def start(self) -> bool:
        self.start_runtime()
        time.sleep(0.3)
        b_ok = self.start_bridge()
        if b_ok:
            self.started_at = time.time()
        return b_ok

    def stop(self) -> bool:
        stopped = True
        for proc_attr in ("bridge_process", "runtime_process"):
            p = getattr(self, proc_attr, None)
            if p is not None:
                try:
                    p.terminate()
                    p.wait(timeout=2)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass
                setattr(self, proc_attr, None)
        self.started_at = None
        return stopped

    def reload_account(self) -> Dict[str, Any]:
        """Отправляет сигнал мосту перезагрузить активный аккаунт."""
        if not self.is_bridge_running():
            return {"ok": False, "error": "Bridge is not running"}
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:8765/reload_account",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_status(self) -> Dict[str, Any]:
        bridge_alive = self.is_bridge_running()
        runtime_alive = self.is_runtime_running()
        active_account = None

        if bridge_alive:
            try:
                req = urllib.request.Request("http://127.0.0.1:8765/healthz")
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    active_account = data.get("account")
            except Exception:
                pass

        return {
            "is_running": bridge_alive,
            "bridge_online": bridge_alive,
            "runtime_online": runtime_alive,
            "native_tools": not runtime_alive,
            "bridge_port": 8765,
            "runtime_port": 8787 if runtime_alive else None,
            "provider_id": "abofab",
            "active_account": active_account,
            "started_at": self.started_at
        }
