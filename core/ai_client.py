import asyncio
import os
import re
import warnings
from concurrent.futures import ThreadPoolExecutor

from openai import OpenAI

warnings.filterwarnings("ignore")

import json
import requests
from requests.adapters import HTTPAdapter
import google.generativeai as genai

# Shared connection pool session to reuse TCP/TLS connections
_HTTP_SESSION = requests.Session()
_adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=1)
_HTTP_SESSION.mount("https://", _adapter)
_HTTP_SESSION.mount("http://", _adapter)


def _call_pollinations(prompt: str, system_message: str = "You are a helpful AI assistant.") -> str:
    """Ultra-fast, 100% free REST AI endpoint with zero keys, zero rate limits, and zero paywalls."""
    # Note: Specifying "openai" or "deepseek" explicitly returns HTTP 402; None/default routes to free anonymous tier
    model_configs = [None, "openai-fast"]
    url = "https://text.pollinations.ai/"
    for m in model_configs:
        try:
            payload = {
                "messages": [
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": prompt},
                ],
                "seed": 42,
            }
            if m:
                payload["model"] = m
            res = _HTTP_SESSION.post(url, json=payload, timeout=20)
            if res.status_code == 200 and res.text and len(res.text.strip()) > 0:
                raw = res.text.strip()
                try:
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        if "content" in data and data["content"]:
                            return str(data["content"])
                        elif "reasoning" in data and data["reasoning"]:
                            return str(data["reasoning"])
                        elif "choices" in data and len(data["choices"]) > 0:
                            msg = data["choices"][0].get("message", {})
                            return msg.get("content") or msg.get("reasoning") or raw
                except Exception:
                    pass
                return raw
        except Exception:
            continue
    return ""


def _run_g4f_sync(prompt: str, system_message: str) -> str:
    """Executes G4F in an isolated event loop to prevent 'Timeout context manager' asyncio errors on Windows."""
    def _worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            from g4f.client import Client as G4FClient

            client = G4FClient()
            models_to_try = [
                "gpt-4o-mini",
                "gpt-4o",
            ]
            for m in models_to_try:
                try:
                    response = client.chat.completions.create(
                        model=m,
                        messages=[
                            {"role": "system", "content": system_message},
                            {"role": "user", "content": prompt},
                        ],
                    )
                    content = response.choices[0].message.content
                    if content and len(content.strip()) > 0:
                        return content
                except Exception:
                    continue
            return ""
        finally:
            loop.close()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_worker)
            return future.result(timeout=25)
    except Exception:
        return ""


def _call_g4f(prompt: str, system_message: str = "You are a helpful AI assistant.") -> str:
    # 1. First attempt Pollinations AI (free, fast, zero paywall errors)
    poll_res = _call_pollinations(prompt, system_message)
    if poll_res and len(poll_res.strip()) > 0:
        return poll_res

    # 2. Fallback to G4F client
    res = _run_g4f_sync(prompt, system_message)
    if res and len(res.strip()) > 0:
        return res
    raise RuntimeError("All Free AI endpoints (Pollinations, G4F) failed. Please check network connection.")


class AIClient:
    def __init__(self):
        def _clean_key(key_str):
            if not key_str:
                return None
            k = str(key_str).strip()
            # Ignore placeholder strings or obvious login passwords (e.g., contains @, spaces, or too short)
            if len(k) < 15 or " " in k or "@" in k or k.lower() in ["none", "null", "false"]:
                return None
            return k

        self.openai_api_key = _clean_key(os.getenv("OPENAI_API_KEY", None))
        self.gemini_api_key = _clean_key(os.getenv("GEMINI_API_KEY", None))
        self.openrouter_api_key = _clean_key(os.getenv("OPENROUTER_API_KEY", None))
        self.groq_api_key = _clean_key(os.getenv("GROQ_API_KEY", None))

        self.openai_client = None
        self.groq_client = None
        self.openrouter_client = None

        if self.openai_api_key:
            try:
                self.openai_client = OpenAI(api_key=self.openai_api_key)
            except Exception:
                self.openai_api_key = None
        if self.groq_api_key:
            try:
                self.groq_client = OpenAI(
                    base_url="https://api.groq.com/openai/v1", api_key=self.groq_api_key
                )
            except Exception:
                self.groq_api_key = None
        if self.openrouter_api_key:
            try:
                self.openrouter_client = OpenAI(
                    base_url="https://openrouter.ai/api/v1", api_key=self.openrouter_api_key
                )
            except Exception:
                self.openrouter_api_key = None
        if self.gemini_api_key:
            try:
                genai.configure(api_key=self.gemini_api_key)
            except Exception:
                self.gemini_api_key = None

    def _get_valid_gemini_models(self):
        """Discovers valid Gemini models and excludes audio/TTS/embed and deprecated endpoints."""
        if not self.gemini_api_key:
            return ["gemini-flash-lite-latest", "gemini-3.8-flash", "gemini-2.0-flash"]
        try:
            available = [
                m.name.replace("models/", "")
                for m in genai.list_models()
                if "generateContent" in m.supported_generation_methods
            ]
            invalid_keywords = [
                "tts", "audio", "voice", "live", "realtime", "embed", "imagen", "veo", "gemma",
                "gemini-2.5-flash", "gemini-2.5-pro"
            ]
            filtered = [m for m in available if not any(k in m.lower() for k in invalid_keywords)]
            # Sort lite and flash models to the top for speed and quota efficiency
            prioritized = sorted(
                filtered,
                key=lambda x: (0 if "lite" in x.lower() else (1 if "flash" in x.lower() else 2))
            )
            return prioritized if prioritized else ["gemini-flash-lite-latest", "gemini-3.8-flash", "gemini-2.0-flash"]
        except Exception:
            return ["gemini-flash-lite-latest", "gemini-3.8-flash", "gemini-2.0-flash"]

    def generate_response(
        self,
        prompt: str,
        system_message: str = "You are a helpful AI assistant.",
        model="gpt-4o-mini",
    ) -> str:
        """Standard text generation with multi-provider fallback."""
        errors = []

        # 1. Force G4F Mode
        if os.getenv("FORCE_G4F") == "true":
            try:
                print("Forcing G4F (Free Mode)...")
                return _call_g4f(prompt=prompt, system_message=system_message)
            except Exception as e:
                print(f"Forced G4F failed: {e}. Attempting available API providers...")
                errors.append(f"G4F: {e}")

        # 2. OpenAI Provider
        try:
            if self.openai_api_key and self.openai_client:
                print("Attempting OpenAI (gpt-4o-mini)...")
                response = self.openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": system_message},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.2,
                )
                content = response.choices[0].message.content
                if content:
                    return content
        except Exception as e:
            err_msg = str(e)
            if "401" in err_msg or "invalid_api_key" in err_msg or "Incorrect API key" in err_msg:
                print(f"[AIClient] OpenAI API key rejected (401). Disabling OpenAI for this session.")
                self.openai_api_key = None
            errors.append(f"OpenAI: {e}")
            print(f"OpenAI failed: {e}")

        # 3. Gemini Provider
        try:
            if self.gemini_api_key:
                models = self._get_valid_gemini_models()
                for gem_model in models:
                    try:
                        print(f"Attempting Gemini ({gem_model})...")
                        genai_model = genai.GenerativeModel(
                            gem_model, system_instruction=system_message
                        )
                        response = genai_model.generate_content(
                            prompt, generation_config={"temperature": 0.2}
                        )
                        content = response.text
                        if content:
                            return content
                    except Exception as inner_e:
                        print(f"Gemini {gem_model} failed: {inner_e}")
                        continue
        except Exception as e:
            err_msg = str(e)
            if "401" in err_msg or "API_KEY_INVALID" in err_msg:
                self.gemini_api_key = None
            errors.append(f"Gemini: {e}")
            print(f"Gemini failed: {e}")

        # 4. OpenRouter Provider
        try:
            if self.openrouter_api_key and self.openrouter_client:
                print("Attempting OpenRouter (Auto-Select)...")
                response = self.openrouter_client.chat.completions.create(
                    model="openrouter/auto",
                    messages=[
                        {"role": "system", "content": system_message},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.2,
                )
                content = response.choices[0].message.content
                if content:
                    return content
        except Exception as e:
            errors.append(f"OpenRouter: {e}")
            print(f"OpenRouter failed: {e}")

        # 5. Groq Provider
        try:
            if self.groq_api_key and self.groq_client:
                print("Attempting Groq (llama-3.3-70b-versatile)...")
                response = self.groq_client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[
                        {"role": "system", "content": system_message},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.2,
                )
                content = response.choices[0].message.content
                if content:
                    return content
        except Exception as e:
            errors.append(f"Groq: {e}")
            print(f"Groq failed: {e}")

        # 6. Pollinations Fast Free Fallback
        try:
            print("Attempting Pollinations Fast Free Fallback...")
            pol_resp = _call_pollinations(prompt=prompt, system_message=system_message)
            if pol_resp and len(pol_resp.strip()) > 0:
                return pol_resp
        except Exception as e:
            errors.append(f"Pollinations: {e}")

        # 7. G4F Fallback (if not forced earlier)
        if os.getenv("FORCE_G4F") != "true":
            try:
                print("Attempting G4F Free Fallback...")
                return _call_g4f(prompt=prompt, system_message=system_message)
            except Exception as e:
                errors.append(f"G4F: {e}")

        raise RuntimeError(
            "All AI providers (OpenAI, Gemini, Groq, OpenRouter, G4F) failed.\nDetails:\n"
            + "\n".join(errors)
        )

    def analyze_image(self, base64_image: str, prompt: str, model="gpt-4o", max_tokens: int = 2048) -> str:
        """Vision model capability with multi-provider fallback and robust image parsing."""
        if not base64_image:
            return ""

        import base64

        raw_bytes = None
        b64_clean = ""

        # Handle file paths vs raw base64 string
        img_str = str(base64_image).strip()
        if os.path.exists(img_str) and (img_str.lower().endswith(".png") or img_str.lower().endswith(".jpg") or img_str.lower().endswith(".jpeg")):
            try:
                with open(img_str, "rb") as f:
                    raw_bytes = f.read()
                b64_clean = base64.b64encode(raw_bytes).decode("utf-8")
            except Exception as read_err:
                print(f"[AIClient] Could not read image file {img_str}: {read_err}")
        else:
            if "base64," in img_str:
                b64_clean = img_str.split("base64,")[1]
            else:
                b64_clean = img_str
            b64_clean = re.sub(r"\s+", "", b64_clean)
            pad_len = len(b64_clean) % 4
            if pad_len:
                b64_clean += "=" * (4 - pad_len)
            try:
                raw_bytes = base64.b64decode(b64_clean)
            except Exception:
                raw_bytes = None

        if not b64_clean or not raw_bytes:
            print("[AIClient] Invalid base64 image data. Skipping Vision API call.")
            return ""

        # 0. Force G4F (Free Mode)
        if os.getenv("FORCE_G4F") == "true":
            try:
                print("Forcing G4F (Free Mode) for Vision/Layout Analysis...")
                free_res = _call_g4f(
                    prompt=prompt,
                    system_message="You are an expert QA Automation Engineer and UI Visual Inspector analyzing a web UI layout."
                )
                if free_res and len(free_res.strip()) > 0:
                    return free_res
            except Exception as e:
                print(f"Forced G4F Vision failed: {e}. Attempting available API providers...")

        # 1. OpenAI Vision
        try:
            if self.openai_api_key and self.openai_client:
                print("Attempting OpenAI Vision (gpt-4o)...")
                response = self.openai_client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/png;base64,{b64_clean}",
                                        "detail": "high",
                                    },
                                },
                            ],
                        }
                    ],
                    max_tokens=max_tokens,
                )
                return response.choices[0].message.content
        except Exception as e:
            err_msg = str(e)
            if "401" in err_msg or "invalid_api_key" in err_msg or "Incorrect API key" in err_msg:
                print(f"[AIClient] OpenAI API key rejected (401). Disabling OpenAI for this session.")
                self.openai_api_key = None
            print(f"OpenAI Vision failed: {e}")

        # 2. Gemini Vision
        try:
            if self.gemini_api_key and raw_bytes:
                image_parts = [{"mime_type": "image/png", "data": raw_bytes}]
                models = self._get_valid_gemini_models()
                vision_candidates = [m for m in models if "flash" in m.lower() or "pro" in m.lower()]
                if not vision_candidates:
                    vision_candidates = ["gemini-flash-lite-latest", "gemini-2.0-flash", "gemini-1.5-flash"]

                for gem_model in vision_candidates:
                    try:
                        print(f"Attempting Gemini Vision ({gem_model})...")
                        genai_model = genai.GenerativeModel(gem_model)
                        response = genai_model.generate_content([prompt, image_parts[0]])
                        if response.text:
                            return response.text
                    except Exception as inner_e:
                        print(f"Gemini Vision ({gem_model}) failed: {inner_e}")
                        continue
        except Exception as e:
            print(f"Gemini Vision failed: {e}")

        # 3. OpenRouter Vision
        try:
            if self.openrouter_api_key and self.openrouter_client:
                print("Attempting OpenRouter Vision (openai/gpt-4o)...")
                response = self.openrouter_client.chat.completions.create(
                    model="openai/gpt-4o",
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/png;base64,{b64_clean}",
                                        "detail": "auto",
                                    },
                                },
                            ],
                        }
                    ],
                    max_tokens=max_tokens,
                )
                return response.choices[0].message.content
        except Exception as e:
            print(f"OpenRouter Vision failed: {e}")

        # 4. G4F / Pollinations Free Fallback
        try:
            print("Attempting Free G4F/Pollinations fallback for Vision prompt...")
            free_res = _call_g4f(
                prompt=prompt,
                system_message="You are an expert QA Automation Engineer and UI Visual Inspector analyzing a web UI layout."
            )
            if free_res and len(free_res.strip()) > 0:
                return free_res
        except Exception as e:
            print(f"G4F Vision fallback failed: {e}")

        return ""

    def describe_image(self, base64_image: str, prompt: str = "Describe this image", max_tokens: int = 2048) -> str:
        """Alias for analyze_image."""
        return self.analyze_image(base64_image, prompt, max_tokens=max_tokens)

    def analyze_image_for_test_generation(
        self, base64_image: str, target_url: str = "https://example.com", user_instructions: str = ""
    ) -> str:
        """Analyzes a UI screenshot/mockup and returns a raw JSON array of 15 to 25 UI test cases."""
        prompt = f"""
        You are an expert QA Automation Engineer with computer vision capabilities.
        Analyze this UI screenshot / mockup of a web application.
        Identified Target URL: {target_url}
        User Instructions / Focus: {user_instructions or 'Full comprehensive test suite across all visible components'}

        Carefully examine all visible UI elements in the image:
        - Input fields, labels, placeholders, dropdowns, dates, password fields, checkboxes
        - Action buttons (Submit, Login, Signup, Reset, Save, Cancel, Filter)
        - Navigation bars, sidebars, tabs, links, breadcrumbs, logos
        - Data tables, cards, summary metrics, modals, error banners, notifications

        Generate a comprehensive, production-grade test suite of 15 to 25 detailed test cases based on this UI layout.
        Cover:
        1. Positive / Happy path interactions (valid data input and expected successful navigation/submission).
        2. Field validations (empty required fields, invalid email format, boundary lengths, password rules).
        3. Security & edge cases (special characters, SQLi/XSS prevention, rapid clicks).
        4. UI layout & element presence assertions (verify title, logo, buttons, labels exist and are visible).
        5. State persistence & navigation (switching tabs, clicking links, error dismissals).

        CRITICAL OUTPUT FORMAT:
        Output ONLY a raw, valid JSON array of objects without markdown backticks, commentary, or wrapper text.
        Each object must have exactly these keys:
        [
          {{
            "Type": "Web",
            "Target": "{target_url}",
            "Test_Step_Description": "<Detailed test step with exact button/input names from screenshot>",
            "Expected_Result": "<Expected UI result or assertion>"
          }}
        ]
        """
        return self.analyze_image(base64_image, prompt, max_tokens=3500)
