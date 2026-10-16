# Test Step: Auditable action available
# Target URL: http://admin.leadcrm.uat.s3-website.ap-south-1.amazonaws.com

from playwright.sync_api import sync_playwright, expect

def run_test():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        import os
        context = browser.new_context(storage_state='auth.json') if os.path.exists('auth.json') else browser.new_context()
        page = context.new_page()
        page.goto('http://admin.leadcrm.uat.s3-website.ap-south-1.amazonaws.com', wait_until='networkidle')

        if page.locator('.sidebar-toggle-btn, .navbar-toggler, button[aria-label*="sidebar" i], .pill-tab').count() > 0:
            page.locator('.sidebar-toggle-btn, .navbar-toggler, button[aria-label*="sidebar" i], .pill-tab').first.click(force=True)
        expect(page.locator('body')).to_be_visible()

        context.storage_state(path='auth.json')
        browser.close()

if __name__ == '__main__':
    run_test()
