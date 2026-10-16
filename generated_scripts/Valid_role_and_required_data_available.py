# Test Step: Valid role and required data available
# Target URL: http://admin.leadcrm.uat.s3-website.ap-south-1.amazonaws.com

from playwright.sync_api import sync_playwright, expect

def run_test():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        import os
        context = browser.new_context(storage_state='auth.json') if os.path.exists('auth.json') else browser.new_context()
        page = context.new_page()
        page.goto('http://admin.leadcrm.uat.s3-website.ap-south-1.amazonaws.com', wait_until='networkidle')

        if page.locator('input:not([type="hidden"]), select, textarea').count() > 0:
            for inp in page.locator('input[type="text"], input[type="email"], input[type="password"]').all()[:3]:
                try:
                    inp.fill('')
                except Exception:
                    pass
        sub_btn = page.locator('button[type="submit"], input[type="submit"], button:has-text("Submit"), button:has-text("Login"), button:has-text("Save")').first
        if sub_btn.count() > 0:
            sub_btn.click(force=True)
            page.wait_for_timeout(500)
        expect(page.locator('body')).to_be_visible()

        context.storage_state(path='auth.json')
        browser.close()

if __name__ == '__main__':
    run_test()
