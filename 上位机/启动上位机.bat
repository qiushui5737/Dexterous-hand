@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" hand_gui.py
) else (
    python hand_gui.py
)
if errorlevel 1 pause
