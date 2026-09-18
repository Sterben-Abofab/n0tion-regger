@echo off
REM Windows-аналог bridge/start.sh — запуск notion-fable-proxy.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
