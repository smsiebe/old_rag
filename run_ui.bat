@echo off
set PYTHONPATH=%~dp0src
"%~dp0..\.venv\Scripts\python.exe" -m old_rag.cli ui
