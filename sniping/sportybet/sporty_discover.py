"""
SportyBet API Discovery Script

Automatically:
1. Opens visible browser
2. Logs in (pre-fills credentials, clicks login)
3. Navigates to a match
4. Adds selection to betslip
5. Generates booking code
6. Captures ALL API endpoints and saves them

Usage:
    python3 sporty_discover.py
"""

import asyncio
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

API_LOG_FILE = os.path.join(os.path.dirname(__file__), "sporty_api_map.json")
SESSION_FILE = os.path.join(os.path.dirname(__file__), ".sportybet_session.json")


async def main():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("ERROR: pip install playwright && playwright install chromium")
        return

    from dotenv import load_dotenv
    load_dotenv()

    phone = os.getenv("SPORTYBET_PHONE", "")
    password = os.getenv("SPORTYBET_PASSWORD", "")

    discovered = {
        "timestamp": datetime.now().isoformat(),
        "endpoints": {},
        "auth_headers": {},
        "booking_flow": [],
    }

    request_bodies = {}

    def save_results():
        """Save whatever we have so far."""
        try:
            with open(API_LOG_FILE, "w") as f:
                json.dump(discovered, f, indent=2, default=str)
            print(f"\n  [SAVED] API map -> {API_LOG_FILE}")
        except Exception as e:
            print(f"  [ERROR] Could not save: {e}")

    def on_request(request):
        url = request.url
        if "sportybet.com" not in url:
            return
        if any(ext in url for ext in [".js", ".css", ".png", ".jpg", ".svg", ".woff", ".ico", ".gif"]):
            return

        entry = {
            "method": request.method,
            "url": url,
            "headers": dict(request.headers),
            "timestamp": datetime.now().isoformat(),
        }

        if request.method in ("POST", "PUT"):
            try:
                body = request.post_data
                if body:
                    entry["post_data"] = body
                    try:
                        entry["post_json"] = json.loads(body)
                    except (json.JSONDecodeError, TypeError):
                        pass
            except Exception:
                pass

        request_bodies[url] = entry

        if "/api/" in url:
            short = url.split("sportybet.com")[-1].split("?")[0]
            print(f"  [REQ] {request.method:4s} {short}")

    async def on_response(response):
        url = response.url
        if "sportybet.com" not in url:
            return
        if any(ext in url for ext in [".js", ".css", ".png", ".jpg", ".svg", ".woff", ".ico", ".gif"]):
            return
        if "/api/" not in url:
            return

        entry = request_bodies.get(url, {"url": url, "method": "?"})
        entry["status"] = response.status
        entry["response_headers"] = dict(response.headers)

        try:
            body = await response.body()
            text = body.decode("utf-8", errors="replace")
            try:
                entry["response_json"] = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                entry["response_text"] = text[:2000]
        except Exception as e:
            entry["response_error"] = str(e)

        # Capture auth headers
        for h in ["authorization", "x-auth-token", "cookie", "x-session-id", "token"]:
            if h in entry.get("headers", {}):
                discovered["auth_headers"][h] = entry["headers"][h]

        # Also capture token from response
        resp_json = entry.get("response_json", {})
        if isinstance(resp_json, dict):
            for key in ["token", "accessToken", "access_token", "authToken"]:
                if key in resp_json:
                    discovered["auth_headers"][f"response_{key}"] = resp_json[key]
            # Check nested
            data = resp_json.get("data", {})
            if isinstance(data, dict):
                for key in ["token", "accessToken", "access_token", "authToken"]:
                    if key in data:
                        discovered["auth_headers"][f"response_data_{key}"] = data[key]

        short = url.split("sportybet.com")[-1]
        # Use path without query as key (dedup)
        path_key = short.split("?")[0]
        discovered["endpoints"][path_key] = entry
        discovered["booking_flow"].append({
            "order": len(discovered["booking_flow"]) + 1,
            "method": entry.get("method", "?"),
            "path": short,
            "status": response.status,
        })

        print(f"  [RES] {response.status} {short.split('?')[0]}")

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False)
    context = await browser.new_context(
        viewport={"width": 430, "height": 932},
        user_agent=(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.0 Mobile/15E148 Safari/604.1"
        ),
    )
    page = await context.new_page()
    page.on("request", on_request)
    page.on("response", on_response)

    print("=" * 60)
    print("SportyBet API Discovery (Automated)")
    print("=" * 60)

    # ── Step 1: Navigate to site ──
    print("\n[Step 1] Opening SportyBet...")
    try:
        await page.goto("https://www.sportybet.com/ng/m", timeout=60000)
    except Exception:
        pass
    await asyncio.sleep(5)

    # ── Step 2: Login ──
    print("\n[Step 2] Logging in...")
    try:
        # Click login button
        for selector in ['div.m-btn-login', '[data-op="nav-login"]']:
            btn = await page.query_selector(selector)
            if btn:
                await btn.click()
                await asyncio.sleep(3)
                break

        # Wait for login form
        for _ in range(10):
            await asyncio.sleep(2)
            inner = await page.evaluate("""() => {
                const el = document.querySelector('#popupLogin');
                if (!el) return 'missing';
                const html = el.innerHTML.trim();
                return (html === '' || html === '<!---->') ? 'empty' : 'ready';
            }""")
            if inner == "ready":
                break

        # Fill credentials
        p = phone[1:] if phone.startswith("0") else phone
        phone_input = await page.query_selector('[data-op="login-phone"] input[type="tel"]')
        if phone_input:
            await phone_input.fill(p)
            print(f"  Filled phone: ...{p[-4:]}")

        pw_input = await page.query_selector('[data-op="login-pswd"] input[type="password"]')
        if pw_input:
            await pw_input.fill(password)
            print("  Filled password")

        await asyncio.sleep(1)

        # Dismiss any overlay modals that might block clicks
        await page.evaluate("""() => {
            // Remove mask overlays
            document.querySelectorAll('.layout.mask, .es-dialog-mask').forEach(el => {
                el.style.display = 'none';
                el.style.pointerEvents = 'none';
            });
            // Enable and click login button via JS (bypasses overlay interception)
            const btn = document.querySelector('button[data-op="login-btn"]');
            if (btn) {
                btn.disabled = false;
                btn.classList.remove('is-disabled');
                btn.click();
            }
        }""")
        print("  Clicked login button (JS click)")

        # Wait for login to complete
        for _ in range(15):
            await asyncio.sleep(2)
            # Check if login modal closed
            modal_vis = await page.evaluate("""() => {
                const d = document.querySelector('#esDialog0');
                return d && d.style.visibility !== 'hidden' && d.offsetWidth > 0;
            }""")
            if not modal_vis:
                print("  Login successful!")
                break

            # Check for any user-specific elements
            has_user = await page.evaluate("""() => {
                return document.querySelector('[class*="user-avatar"]') !== null ||
                       document.querySelector('[class*="account"]') !== null;
            }""")
            if has_user:
                print("  Login successful! (user avatar detected)")
                break
        else:
            print("  Login may have failed or CAPTCHA required")
            print("  >> If browser shows CAPTCHA, solve it manually")
            print("  >> Waiting 60 seconds for manual intervention...")
            await asyncio.sleep(60)

    except Exception as e:
        print(f"  Login error: {e}")
        print("  Continuing anyway...")

    # Save session cookies only if login succeeded (accessToken present)
    try:
        storage = await context.storage_state()
        has_token = any(
            c.get("name") == "accessToken" and c.get("value")
            for c in storage.get("cookies", [])
        )
        if has_token:
            with open(SESSION_FILE, "w") as f:
                json.dump(storage, f)
            print(f"  Session saved to {SESSION_FILE}")
        else:
            print("  No accessToken found — session NOT saved (login may have failed)")
            print("  Existing session file preserved.")
    except Exception as e:
        print(f"  Error saving session: {e}")

    save_results()

    # ── Step 3: Navigate to football/sports page ──
    print("\n[Step 3] Navigating to sports page...")
    try:
        await page.goto("https://www.sportybet.com/ng/m/sports", timeout=30000)
    except Exception:
        pass
    await asyncio.sleep(5)

    save_results()

    # ── Step 4: Find a match and click on it ──
    print("\n[Step 4] Looking for a match to click...")
    try:
        # Try to find any match row
        match_rows = await page.query_selector_all('div.m-table-row')
        if not match_rows:
            match_rows = await page.query_selector_all('[class*="match-row"]')
        if not match_rows:
            match_rows = await page.query_selector_all('[class*="event-row"]')

        print(f"  Found {len(match_rows)} match elements")

        if match_rows:
            # Click on the first match's odds (first market cell child)
            first_row = match_rows[0]
            row_text = await first_row.inner_text()
            print(f"  First match text: {row_text[:80]}")

            # Find odds buttons within this row
            odds_btns = await first_row.query_selector_all('[class*="market-cell"] > div')
            if not odds_btns:
                odds_btns = await first_row.query_selector_all('[class*="odds"]')
            if not odds_btns:
                odds_btns = await first_row.query_selector_all('[class*="market"] div')

            print(f"  Found {len(odds_btns)} odds elements")

            if odds_btns:
                # Click the first odds (usually home team)
                await odds_btns[0].click()
                print("  Clicked first odds button (home team)")
                await asyncio.sleep(3)
            else:
                # Try clicking the row itself to open match detail
                await first_row.click()
                print("  Clicked match row")
                await asyncio.sleep(3)

                # On match detail page, look for odds buttons
                detail_odds = await page.query_selector_all('[class*="odds-btn"]')
                if not detail_odds:
                    detail_odds = await page.query_selector_all('[class*="market"] [class*="cell"]')
                if detail_odds:
                    await detail_odds[0].click()
                    print(f"  Clicked odds on detail page ({len(detail_odds)} found)")
                    await asyncio.sleep(2)
    except Exception as e:
        print(f"  Error finding match: {e}")

    save_results()

    # ── Step 5: Open betslip and generate booking code ──
    print("\n[Step 5] Opening betslip and generating booking code...")
    try:
        await asyncio.sleep(2)

        # Look for betslip button/indicator
        for sel in ['[class*="betslip"]', '[data-op*="betslip"]',
                    '[class*="bet-slip"]', '[class*="cart"]',
                    '[class*="BETSLIP"]', 'div.m-betslip-btn']:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                print(f"  Opened betslip ({sel})")
                await asyncio.sleep(3)
                break

        # Look for Share / Book / Booking Code button
        for sel in ['[data-op*="share"]', '[data-op*="book"]',
                    'button:has-text("Share")', 'button:has-text("Book")',
                    'button:has-text("Booking")', 'button:has-text("Code")',
                    '[class*="share"]', '[class*="booking"]',
                    'button:has-text("Get Code")', 'a:has-text("Share")']:
            btn = await page.query_selector(sel)
            if btn:
                is_visible = await btn.is_visible()
                if is_visible:
                    await btn.click()
                    print(f"  Clicked share/book button ({sel})")
                    await asyncio.sleep(5)
                    break

        # Try to extract booking code
        for sel in ['[class*="booking-code"]', '[class*="share-code"]',
                    '[class*="code-value"]', 'input[readonly]',
                    '[data-op*="code"]', '[class*="code"]']:
            elem = await page.query_selector(sel)
            if elem:
                text = await elem.inner_text()
                if not text:
                    text = await elem.get_attribute("value")
                if text and len(text.strip()) >= 4:
                    print(f"  BOOKING CODE: {text.strip()}")
                    break

    except Exception as e:
        print(f"  Betslip error: {e}")

    save_results()

    # ── Step 6: Extra wait for any remaining API calls ──
    print("\n[Step 6] Waiting for remaining API calls...")
    await asyncio.sleep(5)

    # Save final results
    save_results()

    # ── Summary ──
    print("\n" + "=" * 60)
    print("DISCOVERY SUMMARY")
    print("=" * 60)
    print(f"\nEndpoints captured: {len(discovered['endpoints'])}")
    print(f"Auth headers found: {list(discovered['auth_headers'].keys())}")

    # Print key endpoints
    print("\n--- KEY ENDPOINTS ---")
    keywords = ["login", "auth", "book", "betslip", "share", "search",
                "match", "market", "order", "bet", "code", "patron",
                "accessToken", "cipher", "wallet", "facts"]
    for path, info in discovered["endpoints"].items():
        if any(kw in path.lower() for kw in keywords):
            method = info.get("method", "?")
            status = info.get("status", "?")
            print(f"\n  [{method}] {path}  -> {status}")
            if "post_json" in info:
                pj = json.dumps(info["post_json"], indent=2)
                if len(pj) > 300:
                    pj = pj[:300] + "..."
                print(f"    Request body: {pj}")
            rj = info.get("response_json", {})
            if isinstance(rj, dict):
                # Show structure
                print(f"    Response keys: {list(rj.keys())[:10]}")
                if "data" in rj and isinstance(rj["data"], dict):
                    print(f"    data keys: {list(rj['data'].keys())[:10]}")
                elif "data" in rj and isinstance(rj["data"], list):
                    print(f"    data: list of {len(rj['data'])} items")

    await browser.close()
    await pw.stop()

    print(f"\n\nFull API map saved to: {API_LOG_FILE}")
    print("Session cookies saved to: {SESSION_FILE}")
    print("\nNext: Use these endpoints to build direct API booking.")


if __name__ == "__main__":
    asyncio.run(main())
