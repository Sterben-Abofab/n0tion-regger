import re
import urllib.parse
from typing import Optional, Dict, Any
import requests


def parse_proxy_string(proxy_str: str) -> Optional[Dict[str, str]]:
    """
    Преобразует строку прокси любого формата:
    - http://user:pass@ip:port
    - socks5://user:pass@ip:port
    - ip:port:user:pass
    - user:pass:ip:port
    - socks5://ip:port:user:pass
    - user:pass@ip:port
    - ip:port
    - socks5://ip:port
    в словарь параметров Playwright:
    {"server": "http://ip:port", "username": "...", "password": "..."}
    """
    if not proxy_str:
        return None
    s = proxy_str.strip()
    if not s:
        return None

    scheme = "http"
    if "://" in s:
        scheme, s = s.split("://", 1)
        scheme = scheme.lower()

    # Формат с 4 частями через двоеточие (ip:port:user:pass или user:pass:ip:port)
    if s.count(":") == 3 and "@" not in s:
        parts = s.split(":")
        if parts[1].isdigit():
            # ip:port:user:pass
            return {
                "server": f"{scheme}://{parts[0]}:{parts[1]}",
                "username": parts[2],
                "password": parts[3]
            }
        elif parts[3].isdigit():
            # user:pass:ip:port
            return {
                "server": f"{scheme}://{parts[2]}:{parts[3]}",
                "username": parts[0],
                "password": parts[1]
            }

    # Формат user:pass@host:port (без протокола или с ним)
    if "@" in s:
        user_pass, host_port = s.split("@", 1)
        if ":" in user_pass:
            u, p = user_pass.split(":", 1)
        else:
            u, p = user_pass, ""
        return {
            "server": f"{scheme}://{host_port}",
            "username": u,
            "password": p
        }

    # Простой host:port
    if s.count(":") == 1 and "@" not in s:
        return {"server": f"{scheme}://{s}"}

    parsed = urllib.parse.urlparse(f"{scheme}://{s}")
    server = f"{scheme}://{parsed.hostname}:{parsed.port}" if parsed.port else f"{scheme}://{parsed.hostname}"
    res = {"server": server}
    if parsed.username:
        res["username"] = parsed.username
    if parsed.password:
        res["password"] = parsed.password
    return res


def test_proxy(proxy_str: str, timeout: int = 8) -> Dict[str, Any]:
    """
    Тестирует работоспособность прокси, определяя реальный внешний IP и геолокацию.
    """
    parsed = parse_proxy_string(proxy_str)
    if not parsed:
        return {"success": False, "error": "Неверный формат прокси"}

    server = parsed["server"]
    scheme, host_port = server.split("://", 1)
    
    if parsed.get("username") and parsed.get("password"):
        proxy_url = f"{scheme}://{parsed['username']}:{parsed['password']}@{host_port}"
    else:
        proxy_url = f"{scheme}://{host_port}"

    proxies = {
        "http": proxy_url,
        "https": proxy_url
    }

    try:
        # Проверяем через ip-api.com или ipify для получения IP и страны
        resp = requests.get(
            "http://ip-api.com/json/?fields=status,message,country,countryCode,city,query",
            proxies=proxies,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        data = resp.json()
        if data.get("status") == "success":
            ip = data.get("query")
            country = data.get("country", "")
            city = data.get("city", "")
            geo = f"{country}, {city}".strip(", ")
            return {
                "success": True,
                "ip": ip,
                "geo": geo,
                "scheme": scheme,
                "message": f"Прокси рабочий! IP: {ip} ({geo})"
            }
        else:
            # Fallback к ipify
            resp2 = requests.get(
                "https://api.ipify.org?format=json",
                proxies=proxies,
                timeout=timeout
            )
            ip = resp2.json().get("ip")
            return {
                "success": True,
                "ip": ip,
                "scheme": scheme,
                "message": f"Прокси рабочий! Внешний IP: {ip}"
            }
    except Exception as e:
        return {
            "success": False,
            "error": f"Ошибка соединения через прокси: {str(e)}"
        }
