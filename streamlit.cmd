@echo off
if exist "%~dp0venv\Scripts\python.exe" (
    "%~dp0venv\Scripts\python.exe" -m streamlit %*
) else (
    python -m streamlit %*
)
