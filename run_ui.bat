@echo off
cd /d "%~dp0"
set PYTHONPATH=%~dp0src
start "" "%~dp0..\.venv\Scripts\pythonw.exe" -m old_rag.cli ui
