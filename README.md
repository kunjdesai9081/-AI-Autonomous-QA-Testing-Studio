# 🚀 QA Automation Studio & E2E Test Suite

This project contains the AI-powered QA Testing Studio and autonomous end-to-end tests built for **[tester-army/e2e](https://github.com/tester-army/e2e)** and Playwright.

---

## 🖥️ Running the QA Studio App

To bypass Windows AppLocker / WDAC Application Control policies that block local `.exe` binaries like `streamlit.exe`, run Streamlit directly via the Python module:

### Option 1: Using PowerShell / Terminal
```powershell
.\venv\Scripts\python.exe -m streamlit run app.py
```
*(Or if your virtual environment is activated: `python -m streamlit run app.py`)*

### Option 2: 1-Click Launchers
* **Windows CMD / File Explorer:** Double-click [`run_app.bat`](file:///C:/Projects/New%20folder%20(6)/run_app.bat)
* **PowerShell:** Run [`.\run_app.ps1`](file:///C:/Projects/New%20folder%20(6)/run_app.ps1)
* **npm:** Run `npm start`

---

## 📦 E2E Test Suite Quick Start

### 1. Install Dependencies
```bash
npm install
```

### 2. Run All Tests
```bash
npm test
```

### 3. Run in Visible Headed Browser
```bash
npm run test:headed
```

### 4. Run Interactive UI Mode
```bash
npm run test:ui
```
