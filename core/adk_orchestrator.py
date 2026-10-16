import json
import os
import re
import requests
from google.adk import Workflow, Agent
from pydantic import BaseModel
from google import genai
from google.genai import errors


class TestGenerationState(BaseModel):
    url: str = ""
    swagger_spec: str = ""
    user_story: str = ""
    generated_test_cases: str = ""
    status: str = "pending"


FALLBACK_MODELS = [
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-flash-lite-latest",
]


def _clean_ai_response(text: str) -> str:
    """Strips reasoning wrappers, thought tags, markdown fences, and conversational intro."""
    if not text:
        return ""
    cleaned = str(text).strip()

    # 1. Strip reasoning / think tags (e.g. DeepSeek-R1 / Qwen reasoning outputs)
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", cleaned, flags=re.DOTALL).strip()
    cleaned = re.sub(r"<reasoning>[\s\S]*?</reasoning>", "", cleaned, flags=re.DOTALL).strip()

    # 2. Check if text is wrapped in JSON payload
    try:
        wrapped = json.loads(cleaned)
        if isinstance(wrapped, dict):
            if "content" in wrapped and wrapped["content"]:
                inner = wrapped["content"]
                if isinstance(inner, (list, dict)):
                    return json.dumps(inner)
                cleaned = str(inner).strip()
            elif "reasoning" in wrapped and wrapped["reasoning"]:
                cleaned = str(wrapped["reasoning"]).strip()
            elif "choices" in wrapped and len(wrapped["choices"]) > 0:
                msg = wrapped["choices"][0].get("message", {})
                cleaned = str(msg.get("content") or msg.get("reasoning") or cleaned).strip()
    except Exception:
        pass

    # 3. Strip markdown fences
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE).strip()

    return cleaned


def _call_g4f_orchestrator(prompt: str, instruction: str) -> str:
    """Iterates through Pollinations AI free endpoints and isolated G4F with fast failover."""
    try:
        from core.ai_client import _call_g4f
        res = _call_g4f(prompt=prompt, system_message=instruction)
        cleaned = _clean_ai_response(res)
        if cleaned:
            print("[Free AI Agent] Successfully generated response via Free Mode!")
            return cleaned
    except Exception as e:
        print(f"[Free AI Agent] Free AI call note: {e}")
    return ""


class APIScraperAgent(Agent):
    """Agent specialized in reading Swagger specs and generating resilient API tests."""

    def __init__(self):
        super().__init__(
            name="APIScraperAgent",
            model="gemini-2.0-flash",
            instruction="""You are an expert Backend QA Automation Engineer.
Your task is to analyze OpenAPI/Swagger specifications and generate an extensive, comprehensive test suite (15 to 25 detailed test cases).
Cover positive happy paths, invalid authentication/headers, required/missing payload parameters, invalid data types, boundary testing, and response status codes.

CRITICAL TEST STEP QUALITY RULES:
1. Every `Test_Step_Description` must specify clear, numbered sequential steps (e.g. 'Step 1: Set Authorization headers -> Step 2: Send POST request with JSON payload -> Step 3: Verify HTTP 200/201 response').
2. `Expected_Result` must specify expected status code and response schema validation criteria.
3. You must output ONLY a raw, valid JSON array of objects without markdown formatting, commentary, or code fences.
4. Each object must have exactly these keys: "Type", "Target", "Test_Step_Description", "Expected_Result".
5. "Type" must always be "API".
""",
        )

    def process(
        self,
        state: TestGenerationState,
        client: genai.Client,
        ai_client,
        base_url: str,
    ) -> TestGenerationState:
        story_context = f"\nUSER STORY & BUSINESS FLOW:\n{state.user_story}\n" if state.user_story else ""
        prompt = f"""
        Analyze this OpenAPI/Swagger specification snippet.
        {story_context}
        OpenAPI Spec Snippet:
        {state.swagger_spec[:20000]}

        Generate 15 to 25 comprehensive, high-coverage API test cases.
        {'Ensure you align test cases with the USER STORY and business flow requirements specified above.' if state.user_story else ''}
        For "Target", use the full URL by prepending {base_url} to the endpoint path.
        Ensure "Test_Step_Description" includes the specific HTTP method required (e.g., "Step 1: Send POST request to...").
        """

        response_text = ""

        # 1. If Force G4F Free Mode is requested
        if os.getenv("FORCE_G4F") == "true":
            print("[API Agent] Forcing G4F Free Mode...")
            response_text = _call_g4f_orchestrator(prompt, self.instruction)
            if not response_text:
                print("[API Agent] G4F free mode failed. Attempting configured API key fallbacks...")

        # 2. Gemini GenAI Client
        if not response_text and client:
            for model_name in FALLBACK_MODELS:
                try:
                    print(f"[API Agent] Attempting generation with model: {model_name}...")
                    response = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=genai.types.GenerateContentConfig(
                            system_instruction=self.instruction
                        ),
                    )
                    if response and response.text:
                        response_text = _clean_ai_response(response.text)
                        print(f"[API Agent] Success using {model_name}!")
                        break
                except errors.ClientError as e:
                    print(f"[API Agent] Model {model_name} failed: {getattr(e, 'message', e)}. Falling back...")
                    continue
                except Exception as e:
                    print(f"[API Agent] Notice with {model_name}: {e}")
                    continue

        # 3. Fallback to OpenAI if configured
        if not response_text and ai_client and getattr(ai_client, "openai_client", None):
            try:
                print("[API Agent] Attempting OpenAI fallback...")
                openai_res = ai_client.openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": self.instruction},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.2,
                )
                response_text = _clean_ai_response(openai_res.choices[0].message.content)
                print("[API Agent] Success using OpenAI!")
            except Exception as e:
                print(f"[API Agent] OpenAI fallback failed: {e}")

        # 4. Fallback to Groq if configured
        if not response_text and ai_client and getattr(ai_client, "groq_client", None):
            try:
                print("[API Agent] Engaging Groq fallback...")
                groq_response = ai_client.groq_client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[
                        {"role": "system", "content": self.instruction},
                        {"role": "user", "content": prompt},
                    ],
                )
                response_text = _clean_ai_response(groq_response.choices[0].message.content)
                print("[API Agent] Success using Groq!")
            except Exception as e:
                print(f"[API Agent] Groq fallback failed: {e}")

        # 5. Fallback to OpenRouter if configured
        if not response_text and ai_client and getattr(ai_client, "openrouter_client", None):
            try:
                print("[API Agent] Engaging OpenRouter fallback...")
                for or_model in ["deepseek/deepseek-chat", "meta-llama/llama-3.3-70b-instruct", "openai/gpt-4o-mini"]:
                    try:
                        or_res = ai_client.openrouter_client.chat.completions.create(
                            model=or_model,
                            messages=[
                                {"role": "system", "content": self.instruction},
                                {"role": "user", "content": prompt},
                            ],
                            temperature=0.2,
                        )
                        response_text = _clean_ai_response(or_res.choices[0].message.content)
                        if response_text:
                            print(f"[API Agent] Success using OpenRouter ({or_model})!")
                            break
                    except Exception:
                        continue
            except Exception as e:
                print(f"[API Agent] OpenRouter fallback failed: {e}")

        # 6. Fallback to G4F if not already tried
        if not response_text and os.getenv("FORCE_G4F") != "true":
            print("[API Agent] Engaging Free AI Fallback...")
            response_text = _call_g4f_orchestrator(prompt, self.instruction)

        if not response_text:
            raise RuntimeError(
                "All AI models (Gemini, OpenAI, Groq, OpenRouter, Free AI) failed to generate API test cases. "
                "Please check your network connection or API keys."
            )

        state.generated_test_cases = response_text
        state.status = "api_success"
        return state


class WebUIAgent(Agent):
    """Agent specialized in generating Playwright UI tests from DOM layouts."""

    def __init__(self):
        super().__init__(
            name="WebUIAgent",
            model="gemini-2.0-flash",
            instruction="""You are an expert QA Automation Lead and Playwright Specialist.
Your mission is to generate a comprehensive, highly reliable test suite (15 to 25 test cases) covering all interactive elements found in the DOM structure.

CRITICAL TEST STEP QUALITY & EXECUTION RULES:
1. ATOMIC & STRUCTURED ACTIONS: Each `Test_Step_Description` must specify clear, numbered sequential steps on elements present in the DOM:
   - "Step 1: Navigate to page -> Step 2: Verify page header & layout containers render visibly."
   - "Step 1: Locate search input -> Step 2: Fill search input with 'test query' -> Step 3: Press Enter to execute search."
   - "Step 1: Click 'DEO Data Entry' navigation link -> Step 2: Verify data table view loads with candidate records."
   - "Step 1: Click on first 'Enter Data' button in table row -> Step 2: Verify modal dialog opens with form inputs."
   - "Step 1: Select 'Assigned' option from status dropdown -> Step 2: Verify table filters records accordingly."
   - "Step 1: Leave mandatory inputs blank -> Step 2: Click Submit button -> Step 3: Verify required field validation error appears."

2. RESILIENT PLAYWRIGHT CODE & ASSERTIONS:
   - For page title & load verification: Use `assert len((page.title() or '').strip()) > 0` and `expect(page.locator('body')).to_be_visible()`.
   - DO NOT generate rigid exact equality assertions on `page.url == '...'` that break on redirects (such as unauthenticated redirects to /login) or trailing slashes. Instead, check `assert any(k in page.url for k in ['dashboard', 'leadcrm', 'login'])` or `expect(page.locator('body')).to_be_visible()`.
   - Always check element count or use `.first` before clicking or interacting: `if page.locator('...').count() > 0: page.locator('...').first.click(force=True)`.

3. STRICT JSON OUTPUT FORMAT:
   You must output ONLY a valid JSON array of objects. No markdown code fences, reasoning text, or conversational intro.
   Each object must have exactly:
   {
     "Type": "Web",
     "Target": "<exact target URL>",
     "Test_Step_Description": "<concrete, actionable numbered test step description>",
     "Expected_Result": "<clear, verifiable expected outcome>",
     "Playwright_Code": "<synchronous Python Playwright statements using page object>"
   }
""",
        )

    def process(
        self, state: TestGenerationState, client: genai.Client, ai_client
    ) -> TestGenerationState:
        story_section = f"\nUSER STORY / BUSINESS GOAL / SCENARIO CONTEXT:\n{state.user_story}\n" if state.user_story else ""
        dom_content = state.swagger_spec[:25000] if state.swagger_spec else ""
        prompt = f"""
        Analyze the following Multi-Zone DOM structure extracted from {state.url}:
        {story_section}
        DOM Structure:
        {dom_content}
        
        Generate 20 to 30 comprehensive test cases with clear sequential steps.
        {'Ensure you align test workflows directly with the USER STORY provided above, covering all specified business actions, user personas, and acceptance criteria in addition to full DOM element coverage.' if state.user_story else 'Cover EVERY zone and element listed above (Navigation links, Search & Filters, Table rows & Row action buttons, Pagination controls, Forms/Inputs, and Security/Resilience).'}
        For "Target", use the exact URL: {state.url}.
        """

        response_text = ""

        # 1. If Force G4F Free Mode is requested
        if os.getenv("FORCE_G4F") == "true":
            print("[Web Agent] Forcing G4F Free Mode...")
            response_text = _call_g4f_orchestrator(prompt, self.instruction)
            if not response_text:
                print("[Web Agent] G4F free mode failed. Attempting configured API key fallbacks...")

        # 2. Gemini GenAI Client
        if not response_text and client:
            for model_name in FALLBACK_MODELS:
                try:
                    print(f"[Web Agent] Attempting generation with model: {model_name}...")
                    response = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=genai.types.GenerateContentConfig(
                            system_instruction=self.instruction
                        ),
                    )
                    if response and response.text:
                        response_text = _clean_ai_response(response.text)
                        print(f"[Web Agent] Success using {model_name}!")
                        break
                except errors.ClientError as e:
                    print(f"[Web Agent] Model {model_name} failed: {getattr(e, 'message', e)}. Falling back...")
                    continue
                except Exception as e:
                    print(f"[Web Agent] Notice with {model_name}: {e}")
                    continue

        # 3. Fallback to OpenAI if configured
        if not response_text and ai_client and getattr(ai_client, "openai_client", None):
            try:
                print("[Web Agent] Attempting OpenAI fallback...")
                openai_res = ai_client.openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": self.instruction},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.2,
                )
                response_text = _clean_ai_response(openai_res.choices[0].message.content)
                print("[Web Agent] Success using OpenAI!")
            except Exception as e:
                print(f"[Web Agent] OpenAI fallback failed: {e}")

        # 4. Fallback to Groq if configured
        if not response_text and ai_client and getattr(ai_client, "groq_client", None):
            try:
                print("[Web Agent] Engaging Groq fallback...")
                groq_response = ai_client.groq_client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[
                        {"role": "system", "content": self.instruction},
                        {"role": "user", "content": prompt},
                    ],
                )
                response_text = _clean_ai_response(groq_response.choices[0].message.content)
                print("[Web Agent] Success using Groq!")
            except Exception as e:
                print(f"[Web Agent] Groq fallback failed: {e}")

        # 5. Fallback to OpenRouter if configured
        if not response_text and ai_client and getattr(ai_client, "openrouter_client", None):
            try:
                print("[Web Agent] Engaging OpenRouter fallback...")
                for or_model in ["deepseek/deepseek-chat", "meta-llama/llama-3.3-70b-instruct", "openai/gpt-4o-mini"]:
                    try:
                        or_res = ai_client.openrouter_client.chat.completions.create(
                            model=or_model,
                            messages=[
                                {"role": "system", "content": self.instruction},
                                {"role": "user", "content": prompt},
                            ],
                            temperature=0.2,
                        )
                        response_text = _clean_ai_response(or_res.choices[0].message.content)
                        if response_text:
                            print(f"[Web Agent] Success using OpenRouter ({or_model})!")
                            break
                    except Exception:
                        continue
            except Exception as e:
                print(f"[Web Agent] OpenRouter fallback failed: {e}")

        # 6. Fallback to G4F if not already tried
        if not response_text and os.getenv("FORCE_G4F") != "true":
            print("[Web Agent] Engaging Free AI Fallback...")
            response_text = _call_g4f_orchestrator(prompt, self.instruction)

        if not response_text:
            raise RuntimeError(
                "All AI models (Gemini, OpenAI, Groq, OpenRouter, Free AI) failed to generate Web test cases. "
                "Please check your network connection or API keys."
            )

        state.generated_test_cases = response_text
        state.status = "web_success"
        return state


class TestOrchestrator:
    """Manages the graph of ADK agents for the QA testing system."""

    def __init__(self, ai_client=None):
        self.ai_client = ai_client
        self._gemini_client = None
        self.api_agent = APIScraperAgent()
        self.web_agent = WebUIAgent()

    @property
    def client(self) -> genai.Client:
        """Dynamically provides active Gemini GenAI client."""
        gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
        if gemini_key:
            if self._gemini_client is None or getattr(self, "_current_key", "") != gemini_key:
                try:
                    self._gemini_client = genai.Client(api_key=gemini_key)
                    self._current_key = gemini_key
                except Exception as e:
                    print(f"Warning: Failed to create Gemini client: {e}")
                    self._gemini_client = None
        else:
            self._gemini_client = None
        return self._gemini_client

    def _ensure_ai_client(self):
        """Lazily creates an AIClient instance if none was provided."""
        if self.ai_client is None:
            try:
                from core.ai_client import AIClient
                self.ai_client = AIClient()
            except Exception:
                pass

    def generate_api_tests(self, swagger_spec: str, base_url: str, user_story: str = "") -> str:
        self._ensure_ai_client()
        state = TestGenerationState(swagger_spec=swagger_spec, user_story=user_story)
        final_state = self.api_agent.process(
            state, self.client, self.ai_client, base_url
        )
        return final_state.generated_test_cases

    def generate_web_tests(self, url: str, dom_snapshot: str, user_story: str = "") -> str:
        self._ensure_ai_client()
        state = TestGenerationState(url=url, swagger_spec=dom_snapshot, user_story=user_story)
        final_state = self.web_agent.process(state, self.client, self.ai_client)
        return final_state.generated_test_cases
