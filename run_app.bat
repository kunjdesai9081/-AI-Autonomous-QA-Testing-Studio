@echo off
echo ========================================================
echo  Starting AI Testing Studio via Python Module
echo ========================================================
if exist "venv\Scripts\python.exe" (
    venv\Scripts\python.exe -m streamlit run app.py
) else (
    python -m streamlit run app.py
)
pause
