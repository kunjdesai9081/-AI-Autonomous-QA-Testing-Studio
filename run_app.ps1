# Start Streamlit via python -m streamlit to bypass Windows AppLocker/WDAC policy
Write-Host "Starting AI Testing Studio via Python Module..." -ForegroundColor Cyan

$pyPath = ".\venv\Scripts\python.exe"
if (Test-Path $pyPath) {
    & $pyPath -m streamlit run app.py
} else {
    python -m streamlit run app.py
}
