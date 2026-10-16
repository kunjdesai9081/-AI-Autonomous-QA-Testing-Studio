# 🚀 AI-Powered QA Testing Tool — Implementation Plan

This implementation plan outlines the architecture, directory structure, module design, and execution strategy for the AI-driven QA testing framework supporting **Web UI** (Playwright + Vision + Self-Healing) and **API** (Requests + Schema Validation) testing.

---

## 📋 Executive Summary

The **AI QA Testing Tool** unifies browser automation, API assertion, and Large Language Model intelligence (OpenAI, Google Gemini, Groq, and OpenRouter). Key features include:

- **Web UI Engine**: Playwright wrapper with safe-click self-healing selector recovery, Microsoft Edge / Chromium auto-fallback, and GPT-4 / Gemini Vision layout evaluation.
- **API Engine**: Automated execution against OpenAPI / Swagger specifications and REST endpoints with dynamic assertions.
- **Manual Sheet Execution**: Ingestion of Excel (`.xlsx`) / CSV test suites, running each case with live logging and status recording.
- **User Interfaces**:
  1. **CLI Engine** (`typer` + `rich`) for local and CI/CD automated pipeline usage.
  2. **Web Dashboard** (`streamlit`) for visual drag-and-drop test execution, live editing, real-time KPI metrics, screenshot gallery, and export.

---

## 📂 Project Directory Structure

```text
qa_ai_tool/
│
├── core/
│   ├── __init__.py
│   ├── adk_orchestrator.py    # Multi-agent LLM orchestrator & fallback graph
│   ├── ai_client.py           # Multi-provider AI client (OpenAI, Gemini, Groq, OpenRouter)
│   ├── self_healing.py        # DOM parser and self-healing locator recovery
│   ├── test_generator.py      # Automated web DOM scanner & OpenAPI test case builder
│   └── visual_inspector.py    # Vision AI visual UI layout inspector
│
├── runners/
│   ├── __init__.py
│   ├── api_runner.py          # Requests/HTTP execution engine
│   ├── web_runner.py          # Playwright automation engine with self-healing
│   └── sheet_runner.py        # CSV/Excel test suite runner with Rich reporting
│
├── tests/
│   ├── sample_openapi.json    # Sample OpenAPI specification
│   └── sample_test_cases.csv  # Sample manual test suite
│
├── reports/
│   └── screenshots/           # Captured UI inspection & failure screenshots
│
├── generated_scripts/         # Persisted standalone Python test scripts
├── app.py                     # Streamlit Web UI Dashboard
├── cli.py                     # Typer CLI Entrypoint
├── requirements.txt           # Python dependencies
└── README.md                  # Setup & usage documentation
```

---

## 🛠️ Implementation Architecture & Roadmap

### Phase 1: Multi-Provider AI & Self-Healing Automation Engines
- [x] **AI Client (`core/ai_client.py`)**: Multi-provider fallback across OpenAI (`gpt-4o-mini`, `gpt-4o`), Google Gemini (`gemini-2.0-flash`, `gemini-1.5-flash`), Groq (`llama-3.3-70b-versatile`), and OpenRouter.
- [x] **Self-Healing Recovery (`core/self_healing.py`)**: Intercept broken locators or timeouts, capture live DOM context, query AI for repaired selectors, and retry execution seamlessly.
- [x] **Visual Layout Inspector (`core/visual_inspector.py`)**: Capture page screenshots and analyze for visual overlap, broken styling, or responsive misalignment.
- [x] **Multi-Agent Orchestrator (`core/adk_orchestrator.py`)**: Specialized agents for OpenAPI schema decomposition and Web DOM element scanning.

### Phase 2: Execution Engines
- [x] **Web Runner (`runners/web_runner.py`)**: Headed/headless Playwright runner with session/cookie persistence (`auth.json`), Edge/Chromium auto-fallback, and screenshot capture.
- [x] **API Runner (`runners/api_runner.py`)**: Full HTTP method support, JSON payload validation, dynamic assertions, and OpenAPI spec ingestion.
- [x] **Sheet Suite Runner (`runners/sheet_runner.py`)**: Runs combined Web + API test suites with Rich CLI summary tables and output file export.

### Phase 3: Interactive Web Dashboard & CLI
- [x] **Streamlit Dashboard (`app.py`)**:
  - Drag-and-drop CSV/Excel test suite loading.
  - Interactive editable data grid.
  - KPI metric cards (Passed, Failed, Skipped, Success %).
  - Real-time execution logs & dual console capture.
  - One-click export to Excel / CSV.
  - Visual screenshot gallery for audits.
- [x] **Typer CLI (`cli.py`)**: Commands for `api`, `web`, `sheet`, and `generate`.