import os
import re
from datetime import datetime


def capture_full_scroll_screenshot(page, file_path: str = None) -> bytes:
    """
    Manages complete page and internal container scrolling for web apps:
    1. Progressively scrolls the window and ALL nested scrollable containers (data tables,
       cards, modals, sidebars, overflow-y / overflow-x divs) to trigger lazy loading,
       fetch dynamic DOM nodes, and render virtualized lists.
    2. Un-constrains internal overflow and fixed height wrappers temporarily so that
       Playwright captures the entire height of inner SPA components without truncation.
    3. Resets scroll positions and captures the full-page screenshot.
    4. Restores all original styles and viewport state cleanly.
    5. Saves to file_path if provided, and returns raw bytes.
    """
    if not page:
        return None
    try:
        if page.is_closed():
            return None
    except Exception:
        return None

    orig_viewport = None
    try:
        orig_viewport = page.viewport_size or {"width": 1280, "height": 800}
    except Exception:
        return None

    # Step 1: Deep progressive scrolling across main document and all internal scrollable elements
    try:
        page.evaluate("""
        async () => {
            const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
            
            const scrollElementSmoothly = async (el) => {
                const totalH = el.scrollHeight || 0;
                const totalW = el.scrollWidth || 0;
                let currentH = 0;
                
                // Vertical progressive scroll (fast 800px increments)
                while (currentH < totalH) {
                    currentH += 800;
                    el.scrollTo(0, currentH);
                    await sleep(15);
                }
                
                // Horizontal progressive scroll if container is wide
                if (totalW > (el.clientWidth || 0) + 50) {
                    let currentW = 0;
                    while (currentW < totalW) {
                        currentW += 800;
                        el.scrollTo(currentW, currentH);
                        await sleep(15);
                    }
                    el.scrollTo(0, currentH);
                }
            };

            // 1. Scroll window and documentElement
            const docEl = document.documentElement;
            if (docEl) await scrollElementSmoothly(docEl);
            if (document.body && document.body !== docEl) await scrollElementSmoothly(document.body);
            
            // 2. Identify scrollable containers
            const scrollableContainers = [];
            const allElements = document.querySelectorAll('*');
            for (const el of allElements) {
                try {
                    const style = window.getComputedStyle(el);
                    const isScrollableY = (style.overflowY === 'auto' || style.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 20;
                    const isScrollableX = (style.overflowX === 'auto' || style.overflowX === 'scroll') && el.scrollWidth > el.clientWidth + 20;
                    if (isScrollableY || isScrollableX) {
                        scrollableContainers.push(el);
                    }
                } catch (e) {}
            }
            
            for (const container of scrollableContainers) {
                await scrollElementSmoothly(container);
            }
            
            // Wait brief moment for layout settle
            await sleep(50);
            
            // 3. Reset scroll positions back to top
            window.scrollTo(0, 0);
            if (document.documentElement) document.documentElement.scrollTo(0, 0);
            if (document.body) document.body.scrollTo(0, 0);
            for (const container of scrollableContainers) {
                container.scrollTo(0, 0);
            }
        }
        """)
        page.wait_for_timeout(60)
    except Exception as e:
        if "closed" in str(e).lower():
            return None
        print(f"Scroll management pre-pass note: {e}")

    try:
        if page.is_closed():
            return None
    except Exception:
        return None

    # Step 2: Un-constrain inner scroll containers and calculate total dimensions
    metrics = None
    try:
        metrics = page.evaluate("""
        () => {
            window.__antigravity_modified_styles = [];
            
            const candidates = document.querySelectorAll('html, body, #root, #__next, #app, main, [class*="layout"], [class*="content"], [class*="container"], [class*="wrapper"], [class*="table"], [class*="main"], [class*="body"], div, section');
            
            candidates.forEach(el => {
                try {
                    const style = window.getComputedStyle(el);
                    const isRestrictedHeight = (style.height.includes('vh') || style.maxHeight.includes('vh') || style.height.includes('100%') || style.maxHeight.includes('100%'));
                    const isScrollable = (style.overflowY === 'auto' || style.overflowY === 'scroll' || style.overflowY === 'hidden');
                    
                    if ((isRestrictedHeight && isScrollable) || (el.scrollHeight > el.clientHeight + 30 && isScrollable)) {
                        window.__antigravity_modified_styles.push({
                            element: el,
                            height: el.style.height,
                            maxHeight: el.style.maxHeight,
                            overflow: el.style.overflow,
                            overflowY: el.style.overflowY
                        });
                        el.style.setProperty('height', 'auto', 'important');
                        el.style.setProperty('max-height', 'none', 'important');
                        el.style.setProperty('overflow', 'visible', 'important');
                        el.style.setProperty('overflow-y', 'visible', 'important');
                    }
                } catch (err) {}
            });
            
            const body = document.body;
            const html = document.documentElement;
            const fullHeight = Math.max(
                body ? body.scrollHeight : 0,
                body ? body.offsetHeight : 0,
                html ? html.clientHeight : 0,
                html ? html.scrollHeight : 0,
                html ? html.offsetHeight : 0,
                window.innerHeight
            );
            
            const fullWidth = Math.max(
                body ? body.scrollWidth : 0,
                body ? body.offsetWidth : 0,
                html ? html.clientWidth : 0,
                html ? html.scrollWidth : 0,
                html ? html.offsetWidth : 0,
                window.innerWidth
            );
            
            return {
                width: Math.min(Math.max(fullWidth, 1280), 3840),
                height: Math.min(Math.max(fullHeight, 800), 16000)
            };
        }
        """)
    except Exception as e:
        if "closed" in str(e).lower():
            return None
        print(f"Dimension calculation note: {e}")

    try:
        if page.is_closed():
            return None
    except Exception:
        return None

    # Step 3: Capture full-page screenshot
    raw_bytes = None
    try:
        if file_path:
            raw_bytes = page.screenshot(path=file_path, full_page=True)
        else:
            raw_bytes = page.screenshot(full_page=True)
    except Exception as e:
        if "closed" in str(e).lower():
            return None
        print(f"Full-page capture fallback: {e}")
        try:
            if metrics and "height" in metrics and not page.is_closed():
                page.set_viewport_size({"width": metrics["width"], "height": metrics["height"]})
                page.wait_for_timeout(100)
            if not page.is_closed():
                if file_path:
                    raw_bytes = page.screenshot(path=file_path)
                else:
                    raw_bytes = page.screenshot()
        except Exception as e2:
            if "closed" in str(e2).lower():
                return None
            print(f"Viewport capture fallback: {e2}")
            try:
                if not page.is_closed():
                    if file_path:
                        raw_bytes = page.screenshot(path=file_path)
                    else:
                        raw_bytes = page.screenshot()
            except Exception:
                return None

    # Step 4: Restore modified styles and original viewport
    try:
        if not page.is_closed():
            page.evaluate("""
            () => {
                if (window.__antigravity_modified_styles) {
                    window.__antigravity_modified_styles.forEach(item => {
                        try {
                            item.element.style.height = item.height;
                            item.element.style.maxHeight = item.maxHeight;
                            item.element.style.overflow = item.overflow;
                            item.element.style.overflowY = item.overflowY;
                        } catch (err) {}
                    });
                    delete window.__antigravity_modified_styles;
                }
            }
            """)
            if orig_viewport:
                page.set_viewport_size(orig_viewport)
    except Exception:
        pass

    # Step 5: Compress image on disk if file_path was written
    if file_path and os.path.exists(file_path):
        compress_image_file(file_path)

    return raw_bytes


def compress_image_file(file_path: str, max_width: int = 1920, quality: int = 85) -> bool:
    """
    Optimizes and compresses an image file in-place using Pillow (PIL) to reduce disk and report payload size
    without sacrificing visual readability for QA inspection.
    """
    if not file_path or not os.path.exists(file_path):
        return False
    try:
        if os.path.getsize(file_path) < 100:
            return False
        from PIL import Image
        with Image.open(file_path) as img:
            img.verify()
        with Image.open(file_path) as img:
            orig_w, orig_h = img.size
            if orig_w > max_width:
                ratio = max_width / float(orig_w)
                new_h = max(1, int(orig_h * ratio))
                img = img.resize((max_width, new_h), Image.Resampling.LANCZOS)
            if file_path.lower().endswith(".png"):
                img.save(file_path, format="PNG", optimize=True, compress_level=6)
            elif file_path.lower().endswith((".jpg", ".jpeg")):
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                img.save(file_path, format="JPEG", optimize=True, quality=quality)
        return True
    except Exception:
        return False


def save_screenshot_with_prefix(page, prefix: str = "screenshot") -> str:
    """Helper to save a full-scroll managed screenshot into reports/screenshots/."""
    if not page:
        return None
    try:
        if page.is_closed():
            return None
    except Exception:
        return None
    reports_dir = os.path.join("reports", "screenshots")
    os.makedirs(reports_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_prefix = re.sub(r"[^a-zA-Z0-9]", "_", prefix)[:30].strip("_") or "screen"
    file_path = os.path.join(reports_dir, f"{safe_prefix}_{timestamp}.png")
    try:
        res = capture_full_scroll_screenshot(page, file_path)
        if res is not None and os.path.exists(file_path) and os.path.getsize(file_path) > 100:
            return file_path
        # Remove corrupted / 0-byte file if left behind
        if os.path.exists(file_path) and os.path.getsize(file_path) < 100:
            try:
                os.remove(file_path)
            except Exception:
                pass
        return None
    except Exception as e:
        if "closed" not in str(e).lower():
            print(f"Error saving screenshot: {e}")
        if os.path.exists(file_path) and os.path.getsize(file_path) < 100:
            try:
                os.remove(file_path)
            except Exception:
                pass
        return None

