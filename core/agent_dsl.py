import time
from typing import Any, Dict, Optional
from core.replay_cache import ReplayCache
from core.semantic_assert import SemanticAssertEngine


class Screen:
    """Provides high-reliability accessibility and DOM locators for the active page."""

    def __init__(self, page):
        self.page = page

    def get_by_role(self, role: str, name: Optional[str] = None, **kwargs):
        if name:
            return self.page.get_by_role(role, name=name, **kwargs)
        return self.page.get_by_role(role, **kwargs)

    def get_by_text(self, text: str, exact: bool = False, **kwargs):
        return self.page.get_by_text(text, exact=exact, **kwargs)

    def get_by_placeholder(self, text: str, exact: bool = False, **kwargs):
        return self.page.get_by_placeholder(text, exact=exact, **kwargs)

    def get_by_label(self, text: str, exact: bool = False, **kwargs):
        return self.page.get_by_label(text, exact=exact, **kwargs)

    def get_by_test_id(self, test_id: str):
        return self.page.get_by_test_id(test_id)

    def locator(self, selector: str):
        return self.page.locator(selector)


class App:
    """Manages browser navigation and page lifecycle."""

    def __init__(self, page, base_url: str = ""):
        self.page = page
        self.base_url = base_url.rstrip("/")

    def open(self, path_or_url: str, timeout: int = 30000):
        url = path_or_url
        if self.base_url and not path_or_url.startswith("http://") and not path_or_url.startswith("https://"):
            url = f"{self.base_url}/{path_or_url.lstrip('/')}"
        return self.page.goto(url, timeout=timeout, wait_until="domcontentloaded")

    def wait_for_idle(self, timeout_ms: int = 500):
        self.page.wait_for_timeout(timeout_ms)

    @property
    def url(self) -> str:
        return self.page.url

    @property
    def title(self) -> str:
        return self.page.title()


class Agent:
    """
    Goal-driven autonomous agent that drives the UI using natural-language actions and assertions.
    Features self-compiling instant replay cache and self-healing resilience.
    """

    def __init__(self, page, replay_cache: Optional[ReplayCache] = None, semantic_engine: Optional[SemanticAssertEngine] = None):
        self.page = page
        self.replay_cache = replay_cache or ReplayCache()
        self.semantic_engine = semantic_engine or SemanticAssertEngine()
        self.last_action_script = ""
        self.last_latency_ms = 0.0

    def act(self, goal: str, timeout_sec: int = 15) -> str:
        """
        Drives the application to fulfill a natural-language goal.
        1. Checks ReplayCache for instant execution (<50ms).
        2. If miss or failure, engages autonomous DOM discovery & self-healing, then updates cache.
        """
        current_route = self.page.url
        t_start = time.time()

        # 1. Fast Path: Replay Cache
        cached_script = self.replay_cache.get_cached_action(current_route, goal)
        if cached_script:
            try:
                # Execute cached script directly in the page context
                local_scope = {"page": self.page, "time": time}
                exec(cached_script, globals(), local_scope)
                self.last_latency_ms = round((time.time() - t_start) * 1000, 2)
                self.last_action_script = cached_script
                return cached_script
            except Exception as replay_err:
                print(f"[Agent] Replay failed on DOM shift: {replay_err}. Engaging Self-Healing...")

        # 2. Adaptive Path: Autonomous Agent Generation
        from core.ai_client import AIClient
        from core.test_generator import TestGenerator

        generator = TestGenerator(AIClient())
        synthesized_code = generator.generate_playwright_script_for_step(
            url=current_route,
            step_description=goal,
            expected_result="Action executed cleanly.",
            page=self.page,
        )

        if not synthesized_code:
            raise RuntimeError(f"Agent failed to discover interactive element for goal: '{goal}'")

        # Execute synthesized code
        local_scope = {"page": self.page, "time": time}
        exec(synthesized_code, globals(), local_scope)

        self.last_latency_ms = round((time.time() - t_start) * 1000, 2)
        self.last_action_script = synthesized_code

        # Save to Replay Cache for subsequent sub-50ms replays
        self.replay_cache.save_cached_action(
            route_or_url=current_route,
            goal=goal,
            playwright_code=synthesized_code,
            execution_latency_ms=self.last_latency_ms,
        )

        return synthesized_code

    def assert_that(self, claim: str, screenshot_path: Optional[str] = None) -> bool:
        """
        Semantically asserts a natural-language claim against the current visual & DOM state.
        Alias for assert() which is a Python keyword.
        """
        passed, explanation = self.semantic_engine.evaluate_claim(
            page=self.page,
            claim=claim,
            screenshot_path=screenshot_path,
        )
        if not passed:
            raise AssertionError(f"Agent assertion failed: '{claim}'. Reason: {explanation}")
        return True


class AgentContext:
    """Container grouping (app, agent, screen) for clean dependency injection."""

    def __init__(self, page, base_url: str = ""):
        self.app = App(page, base_url=base_url)
        self.replay_cache = ReplayCache()
        self.semantic_engine = SemanticAssertEngine()
        self.agent = Agent(page, replay_cache=self.replay_cache, semantic_engine=self.semantic_engine)
        self.screen = Screen(page)
