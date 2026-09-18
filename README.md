# ⚡ n0tion-regger | Abofab Community

<div align="center">

**Автономная студия автоматической регистрации Notion Business Trial и локальный OpenAI-совместимый API прокси для AI-агентов (Cursor, OpenCode CLI, Cline, Claude Code).**

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688.svg)](https://fastapi.tiangolo.com/)
[![Playwright](https://img.shields.io/badge/Playwright-Automated-orange.svg)](https://playwright.dev/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Telegram Channel](https://img.shields.io/badge/Telegram-Канал-2CA5E0?logo=telegram)](https://t.me/+GRmYZVzzL9EwZWRi)
[![Telegram Bot](https://img.shields.io/badge/Telegram-Бот-2CA5E0?logo=telegram)](https://t.me/AbofabBot)

</div>

---

## 🚀 Возможности

- 🤖 **Автоматическая регистрация Business Trial (30 дней)** в 1 клик через Playwright CDP.
- 📬 **Любые типы почт**: поддержка собственных Catch-all доменов и IMAP (Gmail).
- 🔌 **Локальный OpenAI-совместимый API Мост (`:8765/v1`)**:
  - Прямое подключение к **Cursor**, **OpenCode CLI**, **Cline**, **Roo Code**, **Aider**.
- 🔄 **Пул аккаунтов и ротация**: переключение между активными аккаунтами без прерывания сессий.
- 🛡 **Антифрод защита**: стерильный режим браузера, изоляция отпечатков, автоматическое закрытие нежелательных вкладок, поддержка прокси.
- 📦 **Zero-install Portable сборка**: работает на любом ПК с Windows без предварительной установки Python, Node.js или зависимостей.

---

## 📥 Скачать готовую сборку (Zero-Install)

Для обычных пользователей доступен готовый архив, где всё уже настроено и собрано:

👉 **[Скачать Abofab_Community.zip из Releases](https://github.com/Sterben-Abofab/n0tion-regger/releases/latest)**

1. Распакуйте архив в любую папку.
2. Запустите **`Abofab_Community.exe`** (или `Запустить_Abofab.bat`).
3. В браузере автоматически откроется панель управления `http://localhost:8000`.

---

## 💻 Запуск из исходного кода (Для разработчиков, macOS / Linux / Windows)

### 1. Клонирование репозитория
```bash
git clone https://github.com/Sterben-Abofab/n0tion-regger.git
cd n0tion-regger
```

### 2. Установка зависимостей
```bash
python -m venv venv

# Windows:
venv\Scripts\activate
# macOS / Linux:
source venv/bin/activate

pip install -r requirements.txt
playwright install chromium
```

### 3. Запуск сервера
```bash
python app.py
```
После запуска откройте в браузере: `http://localhost:8000`

---

## 🛠 Подключение к редакторам и CLI

Перейдите во вкладку **«API ключ & Endpoint»** в веб-интерфейсе:

- **Base URL (Endpoint)**: `http://localhost:8765/v1`
- **API Key**: `abofab`
- **Поддерживаемые модели**:
  - `opus-5` (Claude Opus 5)
  - `sonnet-5` (Claude Sonnet 5)
  - `sonnet-4.6` (Claude Sonnet 4.6)
  - `gpt-5.6-sol` (GPT-5.6 Sol)
  - `gpt-5.5` (GPT 5.5)
  - `gemini-3.1-pro` (Gemini 3.1 Pro)
  - `deepseek-v4-pro` (DeepSeek V4 Pro)
  - `grok-4.6` (Grok 4.6)

### Пример для OpenCode CLI / Cursor:
```json
{
  "endpoint": "http://localhost:8765/v1",
  "apiKey": "abofab",
  "model": "sonnet-5"
}
```

---

## 💬 Сообщество и поддержка

- 📢 **Telegram-канал**: [Abofab Community](https://t.me/+GRmYZVzzL9EwZWRi)
- 🤖 **Telegram-бот**: [@AbofabBot](https://t.me/AbofabBot)

---

## 📄 Лицензия
Распространяется под лицензией MIT. Подробности в файле [LICENSE](LICENSE).
