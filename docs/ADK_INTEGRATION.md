# Google Agent Development Kit (ADK) Integration Plan

## Overview
Based on our project analysis, the current AI integration (using simple prompt chaining via `openai` and `google-generativeai` in `core/ai_client.py`) is starting to reach its limits in terms of scalability and complex reasoning. 

By integrating **Google's Agent Development Kit (ADK)**, we can transform our monolithic AI calls into a robust, multi-agent orchestrated system.

## Why Google ADK?
Google ADK allows us to build a graph-based execution engine. Instead of a single AI trying to write code, parse swagger, and inspect layouts simultaneously, we can split responsibilities:

1. **Modular Agents:** We can define specific, containerized agents:
   - `WebUIAgent`: Specialized in parsing HTML/Playwright layouts.
   - `APIAgent`: Specialized in parsing OpenAPI/Swagger JSON.
   - `SelfHealingAgent`: Dedicated to fixing broken selectors on the fly.
2. **Built-in Orchestration:** The ADK provides a routing mechanism so that when a user uploads a mixed suite (API and UI), the ADK orchestrator routes the tasks to the correct sub-agent in parallel, massively **improving performance and reducing timeout errors**.
3. **Model Agnostic but Gemini Optimized:** While ADK natively excels with Gemini's high context window (great for massive OpenAPI specs), it still allows us to plug in OpenAI for specific agents if needed.

## Proposed Architecture Updates

### 1. Replace `core/ai_client.py`
We will replace our raw API clients with an ADK `Agent` class wrapper:

```python
# Future state using Google ADK
from google_adk import Agent, Workflow

test_generator_agent = Agent(
    name="TestGenerator",
    model="gemini-1.5-pro",
    tools=[playwright_scraper_tool, swagger_parser_tool],
    instructions="You are an expert QA engineer. Use tools to analyze the environment and write test cases."
)
```

### 2. Implement ADK Workflows
We will define an ADK graph workflow for our test suite execution, allowing tests to run in parallel and fallback to the `SelfHealingAgent` seamlessly if a step fails.

## Next Steps for Implementation
1. **Dependency update:** `pip install google-adk`
2. **Refactor `ai_client.py`:** Transition from raw `genai.GenerativeModel` to ADK's `Agent` framework.
3. **Tool Definition:** Convert our existing Python functions (like `TestGenerator.generate_api_cases_from_spec`) into official ADK Tools using the `@tool` decorator.

---
*This integration will drastically improve the output quality of generated tests by giving the AI structured access to tools rather than relying purely on zero-shot prompting.*
