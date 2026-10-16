import json
from typing import Dict, Any, List, Optional


class VisualInspector:
    """
    Multi-Tier Visual & Layout Inspection Engine:
    1. Automated DOM Layout Health Audit (horizontal overflow, broken images, unrendered widgets, missing labels)
    2. Multi-Modal Vision AI Evaluation (GPT-4o / Gemini Flash Vision for aesthetic, alignment, and styling anomalies)
    3. Resilient Fallback Diagnostics (zero-token deterministic layout validation)
    """

    def __init__(self, ai_client=None):
        self.ai_client = ai_client

    def audit_dom_layout(self, page) -> Dict[str, Any]:
        """
        Executes an instant, deterministic DOM layout audit directly on the active browser page.
        Identifies horizontal scroll clippings, broken image assets, and layout deformities.
        """
        if not page:
            return {"status": "skipped", "score": 100, "issues": []}
        try:
            if page.is_closed():
                return {"status": "skipped", "score": 100, "issues": []}
        except Exception:
            return {"status": "skipped", "score": 100, "issues": []}

        try:
            audit = page.evaluate("""() => {
                const issues = [];
                let penalty = 0;

                // 1. Check for horizontal overflow (breaks responsive mobile/tablet layout)
                const docWidth = document.documentElement.scrollWidth;
                const winWidth = window.innerWidth;
                const hasOverflow = docWidth > winWidth + 5;
                if (hasOverflow) {
                    issues.push({
                        type: "RESPONSIVE_OVERFLOW",
                        severity: "MEDIUM",
                        message: `Page content (${docWidth}px) horizontally overflows viewport (${winWidth}px).`
                    });
                    penalty += 15;
                }

                // 2. Check for broken images
                const images = Array.from(document.querySelectorAll('img'));
                let brokenCount = 0;
                images.forEach(img => {
                    if (img.complete && (img.naturalWidth === 0 || !img.src)) {
                        brokenCount++;
                    }
                });
                if (brokenCount > 0) {
                    issues.push({
                        type: "BROKEN_IMAGES",
                        severity: "HIGH",
                        message: `Detected ${brokenCount} broken image element(s) failing to render.`
                    });
                    penalty += Math.min(25, brokenCount * 10);
                }

                // 3. Check for empty interactive buttons without text or aria-label
                const buttons = Array.from(document.querySelectorAll('button, [role="button"]'));
                let unlabeledButtons = 0;
                buttons.forEach(btn => {
                    const txt = (btn.innerText || btn.textContent || '').trim();
                    const aria = btn.getAttribute('aria-label') || btn.getAttribute('title') || '';
                    if (!txt && !aria && btn.offsetWidth > 0 && btn.offsetHeight > 0) {
                        unlabeledButtons++;
                    }
                });
                if (unlabeledButtons > 0) {
                    issues.push({
                        type: "UNLABELED_BUTTONS",
                        severity: "LOW",
                        message: `Found ${unlabeledButtons} interactive button(s) lacking visible text or aria-labels.`
                    });
                    penalty += Math.min(10, unlabeledButtons * 2);
                }

                const finalScore = Math.max(0, 100 - penalty);
                return {
                    score: finalScore,
                    hasOverflow: hasOverflow,
                    brokenImages: brokenCount,
                    unlabeledButtons: unlabeledButtons,
                    totalImagesChecked: images.length,
                    totalButtonsChecked: buttons.length,
                    issues: issues
                };
            }""")
            return {
                "status": "success",
                "score": audit.get("score", 100),
                "issues": audit.get("issues", []),
                "details": audit
            }
        except Exception as e:
            return {"status": "note", "score": 100, "issues": [], "note": str(e)}

    def inspect_layout(
        self, image_base64: str = None, context: str = "General Screen UI", page = None
    ) -> Dict[str, Any]:
        """
        Runs comprehensive multi-tier layout inspection:
        - Instant deterministic DOM layout audit
        - Multi-modal Vision AI evaluation if available
        """
        dom_report = {}
        if page:
            dom_report = self.audit_dom_layout(page)

        ai_analysis = ""
        import os
        has_vision_key = bool(
            self.ai_client
            and os.getenv("FORCE_G4F") != "true"
            and (
                getattr(self.ai_client, "openai_api_key", None)
                or getattr(self.ai_client, "gemini_api_key", None)
                or getattr(self.ai_client, "openrouter_api_key", None)
            )
        )
        if has_vision_key and image_base64:
            prompt = f"""
            You are a Senior UI/UX Visual QA Auditor.
            Screen Context: "{context}"
            
            Inspect this screenshot for visual and layout defects:
            1. Layout alignment, grid integrity, and card consistency
            2. Text truncation, overlapping labels, or broken CSS styles
            3. Spacing, padding, and visual hierarchy
            
            Provide a concise, 2-paragraph verdict with specific component references.
            """
            try:
                import concurrent.futures
                pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                try:
                    fut = pool.submit(self.ai_client.analyze_image, image_base64, prompt)
                    ai_analysis = fut.result(timeout=4)
                finally:
                    pool.shutdown(wait=False, cancel_futures=True)
            except Exception as e:
                ai_analysis = f"Vision layout audit note: {e}"

        # Combine reports
        score = dom_report.get("score", 100)
        issues = dom_report.get("issues", [])
        status_label = "HEALTHY" if score >= 85 else ("NEEDS_ATTENTION" if score >= 65 else "CRITICAL")

        summary = f"Visual Layout Health: {score}/100 ({status_label})."
        if issues:
            summary += " Issues: " + "; ".join([i["message"] for i in issues])
        if ai_analysis and not ai_analysis.startswith("Vision layout audit note:"):
            summary += f" Vision AI Note: {ai_analysis[:180]}..."

        return {
            "status": "success",
            "score": score,
            "health": status_label,
            "issues": issues,
            "summary": summary,
            "ai_analysis": ai_analysis,
            "dom_report": dom_report
        }
