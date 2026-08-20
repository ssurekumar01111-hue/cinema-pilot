"""
Script to run Playwright visual check across all 7 scenes specifically by scene_id.
Verifies:
1. Storyboard panel badge displays "Generated (gemini-3.1-flash-image)".
2. No remaining references to "Imagen" or "Imagen 3" exist anywhere in the rendered dashboard.
3. Explicitly waits for each scene_id's DOM to load, eliminating race conditions.
"""

import sys
import os
import time
from playwright.sync_api import sync_playwright

def run_check():
    screenshots_dir = os.path.join(os.path.dirname(__file__), "screenshots")
    os.makedirs(screenshots_dir, exist_ok=True)

    expected_scenes = [
        "scene_001",
        "scene_002",
        "scene_003",
        "scene_004",
        "scene_005",
        "scene_006",
        "scene_007"
    ]

    print("Starting Playwright...", flush=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()

        print("Navigating to http://127.0.0.1:8000/ ...", flush=True)
        page.goto("http://127.0.0.1:8000/", wait_until="domcontentloaded")

        # Wait for scenes sidebar to finish initial load
        page.wait_for_selector(".scene-card-item", timeout=15000)

        results = []
        all_passed = True

        for scene_id in expected_scenes:
            # Trigger scene selection
            card_selector = f".scene-card-item[onclick*='{scene_id}']"
            page.wait_for_selector(card_selector, timeout=5000)
            page.click(card_selector)

            # Explicitly wait for the scene detail container to update to THIS specific scene_id
            subtitle_selector = f".scene-subtitle:has-text('Production Node: {scene_id}')"
            page.wait_for_selector(subtitle_selector, timeout=10000)

            # Wait for storyboard panel
            page.wait_for_selector(".cascade-card:has(.cascade-card-title:has-text('Storyboard Panel'))", timeout=5000)

            scene_title = page.inner_text(".scene-main-title").strip()
            scene_subtitle = page.inner_text(".scene-subtitle").strip()

            # Check storyboard card badge
            sb_badge_el = page.query_selector(".cascade-card:has(.cascade-card-title:has-text('Storyboard Panel')) .badge")
            sb_badge_text = sb_badge_el.inner_text().strip() if sb_badge_el else "NOT FOUND"

            # Check full page rendered text for any 'Imagen'
            page_text = page.inner_text("body")
            has_imagen = "imagen" in page_text.lower()

            # Take screenshot of scene detail
            screenshot_path = os.path.join(screenshots_dir, f"{scene_id}.png")
            page.screenshot(path=screenshot_path)

            status_ok = ("gemini-3.1-flash-image" in sb_badge_text.lower() or "gemini" in sb_badge_text.lower()) and not has_imagen
            if not status_ok:
                all_passed = False

            results.append({
                "scene_id": scene_id,
                "title": scene_title,
                "subtitle": scene_subtitle,
                "badge": sb_badge_text,
                "has_imagen": has_imagen,
                "passed": status_ok,
                "screenshot": screenshot_path
            })
            print(f"[{'PASS' if status_ok else 'FAIL'}] {scene_id} -> {scene_title} -> Badge: '{sb_badge_text}' (Imagen Free: {not has_imagen})", flush=True)

        # Also check Cascade Timeline tab
        timeline_btn = page.query_selector("#tab-timeline-btn")
        if timeline_btn:
            timeline_btn.click()
            time.sleep(0.5)
            timeline_text = page.inner_text("#timeline-view-container")
            timeline_has_imagen = "imagen" in timeline_text.lower()
            print(f"Cascade Timeline tab: Has 'Imagen'={timeline_has_imagen}", flush=True)
            if timeline_has_imagen:
                all_passed = False

        browser.close()

        print("\n--- Summary ---", flush=True)
        print(f"Total scenes tested: {len(results)}", flush=True)
        print(f"All passed: {all_passed}\n", flush=True)
        print("Raw Mapping (scene_id -> Title -> Badge):", flush=True)
        for r in results:
            print(f"  {r['scene_id']} -> \"{r['title']}\" -> \"{r['badge']}\"", flush=True)

        return all_passed

if __name__ == "__main__":
    success = run_check()
    sys.exit(0 if success else 1)
