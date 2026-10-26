@echo off
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" --check
if errorlevel 1 pause
