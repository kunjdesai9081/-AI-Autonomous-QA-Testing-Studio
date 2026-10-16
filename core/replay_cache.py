import hashlib
import json
import os
import time
from typing import Any, Dict, Optional


class ReplayCache:
    """
    Self-Compiling Action Recording & Replay Cache.
    Stores verified, deterministic Playwright action sequences for natural-language goals.
    Enables sub-100ms test executions with zero LLM token consumption on repeated runs.
    """

    def __init__(self, cache_file: str = ".e2e_replay_cache.json"):
        self.cache_file = cache_file
        self.cache: Dict[str, Dict[str, Any]] = self._load_cache()

    def _load_cache(self) -> Dict[str, Dict[str, Any]]:
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_cache(self) -> None:
        try:
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, indent=2)
        except Exception as e:
            print(f"[ReplayCache] Warning: Failed to persist cache: {e}")

    def _get_key(self, route_or_url: str, goal: str) -> str:
        norm_url = (route_or_url or "").strip().lower().split("?")[0].rstrip("/")
        norm_goal = (goal or "").strip().lower()
        content = f"{norm_url}::{norm_goal}"
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    def get_cached_action(self, route_or_url: str, goal: str) -> Optional[str]:
        """Retrieves a cached deterministic Playwright script for the given goal and route with sub-millisecond latency."""
        key = self._get_key(route_or_url, goal)
        entry = self.cache.get(key)
        if entry and entry.get("script"):
            entry["hit_count"] = entry.get("hit_count", 0) + 1
            entry["last_hit_time"] = time.time()
            return entry["script"]
        return None

    def save_cached_action(
        self,
        route_or_url: str,
        goal: str,
        playwright_code: str,
        execution_latency_ms: float = 0.0,
    ) -> None:
        """Saves or updates a verified Playwright action sequence into the replay cache."""
        if not playwright_code or len(playwright_code.strip()) == 0:
            return

        key = self._get_key(route_or_url, goal)
        norm_url = (route_or_url or "").strip().split("?")[0].rstrip("/")
        self.cache[key] = {
            "key": key,
            "route": norm_url,
            "goal": goal.strip(),
            "script": playwright_code.strip(),
            "latency_ms": round(execution_latency_ms, 2),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "hit_count": self.cache.get(key, {}).get("hit_count", 0) + 1,
            "last_hit_time": time.time(),
        }
        self._save_cache()

    def invalidate(self, route_or_url: Optional[str] = None, goal: Optional[str] = None) -> int:
        """Removes entries for a given route or goal from the replay cache."""
        if route_or_url and goal:
            key = self._get_key(route_or_url, goal)
            if key in self.cache:
                del self.cache[key]
                self._save_cache()
                return 1
            return 0

        if route_or_url:
            norm_url = route_or_url.strip().split("?")[0].rstrip("/")
            keys_to_del = [k for k, v in self.cache.items() if v.get("route") == norm_url]
            for k in keys_to_del:
                del self.cache[k]
            if keys_to_del:
                self._save_cache()
            return len(keys_to_del)

        # Invalidate all
        count = len(self.cache)
        self.cache = {}
        self._save_cache()
        return count

    def get_stats(self) -> Dict[str, Any]:
        """Returns statistical overview of the replay cache."""
        total_entries = len(self.cache)
        total_hits = sum(v.get("hit_count", 0) for v in self.cache.values())
        routes = list({v.get("route", "") for v in self.cache.values() if v.get("route")})
        return {
            "total_cached_actions": total_entries,
            "total_replay_hits": total_hits,
            "unique_routes": len(routes),
            "routes": routes,
        }
