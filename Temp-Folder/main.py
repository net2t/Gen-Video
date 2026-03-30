"""
AutoMagicAI — Automated video generation from Google Sheets using MagicLight.AI
Author: net2t (net2tara@gmail.com)
Repo:   https://github.com/net2t/AutoMagicAi
"""

import os
import sys
import json
import time
import signal
import argparse
import requests
import gspread
from datetime import datetime
from oauth2client.service_account import ServiceAccountCredentials
from playwright.sync_api import sync_playwright, Download
from google.oauth2.service_account import Credentials as GACredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from dotenv import load_dotenv
try:
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials as OAuthCredentials
    _OAUTH_LIB_OK = True
except ImportError:
    _OAUTH_LIB_OK = False

# ── Load config ───────────────────────────────────────────────────────────────
load_dotenv()

SPREADSHEET_ID  = os.getenv("SPREADSHEET_ID", "")
ML_EMAIL        = os.getenv("ML_EMAIL", "")
ML_PASSWORD     = os.getenv("ML_PASSWORD", "")
STORIES_PER_RUN = int(os.getenv("STORIES_PER_RUN", "2"))
DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")
HEADLESS_MODE   = os.getenv("HEADLESS_MODE", "false").lower() == "true"

# ── Per-step timing controls (all configurable via .env) ──────────────────────
STEP1_WAIT           = int(os.getenv("STEP1_WAIT",           "60"))   # seconds to wait after Step 1 Next click
STEP2_WAIT           = int(os.getenv("STEP2_WAIT",           "20"))   # seconds to wait for Cast to generate
STEP3_WAIT           = int(os.getenv("STEP3_WAIT",           "180"))  # seconds to wait for Storyboard images
STEP4_RENDER_TIMEOUT = int(os.getenv("STEP4_RENDER_TIMEOUT", "900"))  # seconds to wait for video render (15 min)
STEP4_POLL_INTERVAL  = int(os.getenv("STEP4_POLL_INTERVAL",  "15"))   # how often to check render status
STEP4_MAX_NEXT       = int(os.getenv("STEP4_MAX_NEXT",       "10"))   # max Next clicks before reaching Generate

CREDS_FILE        = "credentials.json"
OAUTH_CREDS_FILE  = "oauth_credentials.json"
OAUTH_TOKEN_FILE  = "token.json"
COOKIES_FILE      = "cookies.json"
DOWNLOADS_DIR     = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# ── Google API scopes ─────────────────────────────────────────────────────────
SHEETS_SCOPES = ["https://spreadsheets.google.com/feeds",
                 "https://www.googleapis.com/auth/drive"]
DRIVE_SCOPES  = ["https://www.googleapis.com/auth/drive"]

# ── Sheet Names ───────────────────────────────────────────────────────────────
VIDEO_SHEET_NAME = "VideoData"  # Your main video sheet - will try first
ACCOUNTS_SHEET_NAME = "Accounts"  # Multi-account credentials sheet

# ── Accounts Sheet Columns ─────────────────────────────────────────────────────
ACCOL_EMAIL      = 1  # A - Email
ACCOL_PASSWORD   = 2  # B - Password
ACCOL_STATUS     = 3  # C - Status (Active/Cooldown/Banned)
ACCOL_DAILY_USE  = 4  # D - Daily Usage (0-5)
ACCOL_LAST_USED  = 5  # E - Last Used Timestamp
ACCOL_TOTAL_GEN  = 6  # F - Total Videos Generated

# ── Sheet column indices (1-based) ────────────────────────────────────────────
COL_TITLE       = 1  # A - Story Title
COL_STORY       = 2  # B - Story Text
COL_MORAL       = 3  # C - Moral
COL_HASHTAGS    = 4  # D - Hashtags
COL_STATUS      = 5  # E - Status (Generated/Processing/Done)
COL_THUMB_URL   = 6  # F - Thumbnail URL
COL_NOTES       = 7  # G - Notes
COL_PROJECT_URL = 8  # H - Project URL
COL_PROCESSED   = 9  # I - Processed Video URL

# ── Graceful shutdown ─────────────────────────────────────────────────────────
shutdown_requested = False
browser_instance   = None

def signal_handler(signum, frame):
    global shutdown_requested, browser_instance
    print("\n[SHUTDOWN] CTRL+C detected — finishing current step then stopping...")
    shutdown_requested = True
    if browser_instance:
        try:
            browser_instance.close()
        except Exception:
            pass
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)


# ── CLI ───────────────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description="AutoMagicAI — Kids Story video generator")
    p.add_argument("--maxstory", "-n", type=int, default=None,
                   help="Stories to process (overrides .env STORIES_PER_RUN)")
    p.add_argument("--headless", action="store_true", default=None,
                   help="Run browser headless (no window, overrides .env HEADLESS_MODE)")
    p.add_argument("--no-headless", action="store_true", default=None,
                   help="Run browser with window (overrides .env HEADLESS_MODE)")
    return p.parse_args()


# ── Google Sheets ─────────────────────────────────────────────────────────────
def get_sheet():
    try:
        creds  = ServiceAccountCredentials.from_json_keyfile_name(CREDS_FILE, SHEETS_SCOPES)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(SPREADSHEET_ID)
        
        # Try to find the video data sheet
        try:
            return spreadsheet.worksheet(VIDEO_SHEET_NAME)
        except gspread.exceptions.WorksheetNotFound:
            # Try common sheet names
            for name in ["Sheet1", "Video Data", "Videos", "Data", "Stories"]:
                try:
                    return spreadsheet.worksheet(name)
                except gspread.exceptions.WorksheetNotFound:
                    continue
            
            # If none found, use the first sheet
            return spreadsheet.get_worksheet(0)
            
    except Exception as e:
        print(f"[ERROR] Google Sheets: {e}")
        return None

def get_sheet_service():
    """Get sheet service for multi-account management"""
    try:
        creds  = ServiceAccountCredentials.from_json_keyfile_name(CREDS_FILE, SHEETS_SCOPES)
        client = gspread.authorize(creds)
        return client.open_by_key(SPREADSHEET_ID)
    except Exception as e:
        print(f"[ERROR] Google Sheets service: {e}")
        return None


# ── Google Drive ──────────────────────────────────────────────────────────────
def get_drive_service():
    """
    Build a Google Drive service.
    Priority:
      1. oauth_credentials.json   — OAuth2 user account (personal Drive)
         Token is cached in token.json after first browser login.
      2. credentials.json         — Service account (shared Drive)
    """
    # — Option 1: OAuth2 user credentials ——————————————————————
    if _OAUTH_LIB_OK and os.path.exists(OAUTH_CREDS_FILE):
        oauth_creds = None
        # Load cached token
        if os.path.exists(OAUTH_TOKEN_FILE):
            try:
                oauth_creds = OAuthCredentials.from_authorized_user_file(
                    OAUTH_TOKEN_FILE, DRIVE_SCOPES
                )
            except Exception:
                oauth_creds = None
        # Refresh or run new OAuth flow
        if not oauth_creds or not oauth_creds.valid:
            if oauth_creds and oauth_creds.expired and oauth_creds.refresh_token:
                oauth_creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    OAUTH_CREDS_FILE, DRIVE_SCOPES
                )
                oauth_creds = flow.run_local_server(port=0)
            # Cache the token
            with open(OAUTH_TOKEN_FILE, "w") as tf:
                tf.write(oauth_creds.to_json())
        print("[Drive] Using OAuth2 user credentials (oauth_credentials.json)")
        return build("drive", "v3", credentials=oauth_creds)

    # — Option 2: Service account ————————————————————————————
    print("[Drive] Using service account credentials (credentials.json)")
    creds = GACredentials.from_service_account_file(CREDS_FILE, scopes=DRIVE_SCOPES)
    return build("drive", "v3", credentials=creds)

def create_drive_folder(service, name: str, parent_id: str) -> str:
    meta   = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    folder = service.files().create(body=meta, fields="id").execute()
    return folder["id"]

def upload_to_drive(service, local_path: str, folder_id: str) -> str:
    name   = os.path.basename(local_path)
    mime   = "video/mp4" if local_path.endswith(".mp4") else "image/jpeg"
    media  = MediaFileUpload(local_path, mimetype=mime, resumable=True)
    f_meta = {"name": name, "parents": [folder_id]}
    upl    = service.files().create(body=f_meta, media_body=media, fields="id,webViewLink").execute()
    service.permissions().create(fileId=upl["id"], body={"role": "reader", "type": "anyone"}).execute()
    return upl.get("webViewLink", "")


# ── Cookie helpers ────────────────────────────────────────────────────────────
def save_cookies(context):
    """Save browser cookies to cookies.json for reuse."""
    try:
        cookies = context.cookies()
        with open(COOKIES_FILE, "w") as f:
            json.dump(cookies, f, indent=2)
        print(f"[Cookies] ✓ Saved {len(cookies)} cookies to {COOKIES_FILE}")
    except Exception as e:
        print(f"[Cookies] Could not save: {e}")

def load_cookies(context) -> bool:
    """Load cookies from cookies.json into the browser context. Returns True if loaded."""
    if not os.path.exists(COOKIES_FILE):
        return False
    try:
        with open(COOKIES_FILE) as f:
            cookies = json.load(f)
        if not cookies:
            return False
        context.add_cookies(cookies)
        print(f"[Cookies] ✓ Loaded {len(cookies)} saved cookies")
        return True
    except Exception as e:
        print(f"[Cookies] Could not load: {e}")
        return False

def clear_cookies():
    """Delete saved cookies (used when login fails with saved cookies)."""
    if os.path.exists(COOKIES_FILE):
        os.remove(COOKIES_FILE)
        print("[Cookies] Cleared stale cookies.json")


# ── Login ─────────────────────────────────────────────────────────────────────
def login(page, email, password):
    print("[Login] Navigating to login page...")
    page.goto("https://magiclight.ai/login/", timeout=60000)
    page.wait_for_load_state("domcontentloaded")
    time.sleep(4)

    # Already logged in?
    if "login" not in page.url.lower():
        print("[Login] Already logged in — skipping.")
        return

    # Click "Sign in with Email" or "Log in with Email" (a <div class="entry-email">)
    print("[Login] Clicking 'Sign in with Email'...")
    email_entry = None
    deadline = time.time() + 15
    while time.time() < deadline:
        for sel in [
            "div.entry-email",
            "text='Sign in with Email'",
            "text='Log in with Email'",
            ".login-methods div"
        ]:
            try:
                el = page.locator(sel)
                if el.count() > 0 and el.first.is_visible():
                    email_entry = el.first
                    break
            except Exception:
                pass
        if email_entry:
            break
        time.sleep(1)
        
    if email_entry is None:
        raise Exception("Could not find 'Sign in with Email' option on login page.")
        
    try:
        email_entry.click()
    except Exception:
        email_entry.first.click()
    time.sleep(3)

    # Fill Email (input type="text" on this site)
    print("[Login] Filling email...")
    email_input = page.locator('input[type="text"], input[type="email"]')
    email_input.first.wait_for(state="visible", timeout=10000)
    email_input.first.click()
    email_input.first.fill(email)
    time.sleep(0.5)

    # Fill Password
    print("[Login] Filling password...")
    pwd_input = page.locator('input[type="password"]')
    pwd_input.first.wait_for(state="visible", timeout=10000)
    pwd_input.first.click()
    pwd_input.first.fill(password)
    time.sleep(0.5)

    # Click Continue — it's a <div class="signin-continue">, NOT a <button>
    print("[Login] Clicking Continue (div.signin-continue)...")
    continue_el = page.locator("div.signin-continue")
    if continue_el.count() == 0:
        # broad fallback
        continue_el = page.locator("text='Continue'")
    continue_el.first.wait_for(state="visible", timeout=10000)
    continue_el.first.click()

    # Wait for redirect away from login
    print("[Login] Waiting for dashboard...")
    try:
        page.wait_for_url("**/home**", timeout=30000)
    except Exception:
        time.sleep(8)

    if "login" in page.url.lower():
        raise Exception("Login failed — still on login page after clicking Continue.")
    print(f"[Login] ✓ Success! URL: {page.url}")


# ── Popup / tour helpers ──────────────────────────────────────────────────────
def dismiss_popups(page):
    for sel in [".arco-modal-close-btn", "button:has-text('OK')", "button:has-text('Got it')",
                "button:has-text('Close')", "[aria-label='Close']",
                ".sora2-modal .close", ".notice-popup-modal__close"]:
        try:
            btn = page.locator(sel)
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click()
                time.sleep(0.5)
        except Exception:
            pass

def _dismiss_tour(page):
    try:
        time.sleep(3)
        print("[Tour] Checking for tutorial overlays...")
        js_click = """() => {
            const texts = ["Skip","Got it","Got It","Close","Done"];
            document.querySelectorAll('button,span,div,a').forEach(el => {
                if (el.innerText && texts.includes(el.innerText.trim())) el.click();
            });
        }"""
        for _ in range(3):
            page.evaluate(js_click)
            time.sleep(1)
        page.evaluate("""() => {
            document.querySelectorAll('.diy-tour,.diy-tour__mask,[class*="tour-tooltip"],[class*="driver-"]')
                .forEach(el => { try { el.remove(); } catch(e){} });
        }""")
        time.sleep(1)
    except Exception as e:
        print(f"[Tour] {e}")

def _dismiss_animation_modal(page):
    """
    Dismiss the 'Animate All' modal that blocks the Generate button.
    Tries multiple strategies to ensure it's fully closed.
    """
    # Strategy 1: click arco-btn-secondary with text Next/Skip
    js1 = """() => {
        const btns = Array.from(document.querySelectorAll(
            'button.arco-btn-secondary, .arco-modal-footer button, .arco-modal button'
        ));
        for (const el of btns) {
            const t = (el.innerText || '').trim();
            const rect = el.getBoundingClientRect();
            if ((t === 'Next' || t === 'Skip' || t === 'Cancel' || t === 'No thanks')
                && rect.width > 0 && rect.height > 0) {
                el.click();
                return 'secondary: ' + t;
            }
        }
        return null;
    }"""
    # Strategy 2: close any arco-modal via X button
    js2 = """() => {
        const close = document.querySelector(
            '.arco-modal-close-btn, [aria-label="Close"], .modal-close, .animation-modal__close'
        );
        if (close) { close.click(); return 'modal X closed'; }
        return null;
    }"""
    # Strategy 3: force-remove the modal DOM element
    js3 = """() => {
        const modals = document.querySelectorAll(
            '.arco-modal-wrapper, .animation-modal, [class*="animation-modal"]'
        );
        let removed = 0;
        modals.forEach(el => { try { el.remove(); removed++; } catch(e){} });
        return removed > 0 ? 'removed ' + removed + ' modal(s)' : null;
    }"""
    for js in [js1, js2, js3]:
        try:
            result = page.evaluate(js)
            if result:
                print(f"[Modal] ✓ {result}")
                time.sleep(2)
                return
        except Exception:
            pass


# ── DOM helpers ───────────────────────────────────────────────────────────────
def _dom_click_text(page, texts: list, timeout: int = 120) -> bool:
    js = """(texts) => {
        const all = Array.from(document.querySelectorAll(
            'button,div[class*="btn"],span[class*="btn"],a,div[class*="vlog-btn"],div[class*="footer-btn"]'
        ));
        for (let i = all.length - 1; i >= 0; i--) {
            const el = all[i];
            let dt = '';
            el.childNodes.forEach(n => { if (n.nodeType === Node.TEXT_NODE) dt += n.textContent; });
            const t = dt.trim() || (el.innerText || '').trim();
            if (texts.includes(t)) {
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) { el.click(); return t; }
            }
        }
        return null;
    }"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = page.evaluate(js, texts)
        if result:
            print(f"[DOM] ✓ Clicked '{result}'")
            return True
        time.sleep(3)
    return False

def _dom_click_class(page, css_class: str, timeout: int = 30) -> bool:
    js = f"""() => {{
        const all = Array.from(document.querySelectorAll('[class*="{css_class}"]'));
        for (let i = all.length - 1; i >= 0; i--) {{
            const el = all[i];
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) {{ el.click(); return el.className; }}
        }}
        return null;
    }}"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = page.evaluate(js)
        if result:
            print(f"[DOM] ✓ Clicked class ~'{css_class}'")
            return True
        time.sleep(3)
    return False

def _dom_debug_buttons(page):
    js = """() => {
        const all = Array.from(document.querySelectorAll(
            'button,div[class*="btn"],span[class*="btn"],a,div[class*="vlog-btn"]'
        ));
        const res = [];
        all.forEach(el => {
            const t = (el.innerText || '').trim().substring(0, 60);
            const r = el.getBoundingClientRect();
            if (t && r.width > 0 && r.height > 0)
                res.push(el.tagName + '.' + (el.className||'').substring(0,40) + ' | ' + t);
        });
        return res;
    }"""
    try:
        items = page.evaluate(js)
        print(f"[DEBUG] URL: {page.url}")
        print("[DEBUG] Visible buttons:")
        for item in (items or []):
            print(f"  {item}")
    except Exception as e:
        print(f"[DEBUG] {e}")

def _try_click_in_context(ctx, selectors: list) -> bool:
    for sel in selectors:
        try:
            loc = ctx.locator(sel)
            if loc.count() > 0:
                target = loc.last
                if target.is_visible():
                    target.scroll_into_view_if_needed(timeout=2000)
                    target.click(timeout=5000)
                    return True
        except Exception:
            pass
    return False

def _click_next_step1(page, timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    selectors = [
        "button.arco-btn-primary:has-text('Next')",
        "button:has-text('Next')",
        "[role='button']:has-text('Next')",
        "div:has-text('Next')",
        "span:has-text('Next')",
        "div[class*='btn']:has-text('Next')",
    ]
    while time.time() < deadline:
        try:
            dismiss_popups(page)
            _dismiss_tour(page)
        except Exception:
            pass

        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass

        if _try_click_in_context(page, selectors):
            return True

        try:
            for fr in page.frames:
                if fr == page.main_frame:
                    continue
                if _try_click_in_context(fr, selectors):
                    return True
        except Exception:
            pass

        try:
            if _dom_click_text(page, ["Next", "Next Step", "Continue"], timeout=3):
                return True
        except Exception:
            pass

        time.sleep(1.5)
    return False

def _click_next_header(page):
    """Click the header-shiny-action__btn Next div and dismiss any animation modal."""
    js = """() => {
        const divs = Array.from(document.querySelectorAll('[class*="header-shiny-action__btn"]'));
        for (const el of divs) {
            const t = (el.innerText || '').trim();
            const r = el.getBoundingClientRect();
            if (t === 'Next' && r.width > 0 && r.height > 0) { el.click(); return 'header Next'; }
        }
        const btns = Array.from(document.querySelectorAll('button.arco-btn-primary, button'));
        for (const el of btns) {
            const t = (el.innerText || '').trim();
            const r = el.getBoundingClientRect();
            if (t === 'Next' && r.width > 0 && r.height > 0) { el.click(); return 'button Next'; }
        }
        return null;
    }"""
    result = page.evaluate(js)
    if result:
        print(f"[Step 4] ✓ {result}")
    return result


# ── Step 1: Content ───────────────────────────────────────────────────────────
def _open_dropdown_and_select(page, label_text: str, option_text: str) -> bool:
    """
    For Arco-Design / custom select components:
    1. Finds the dropdown trigger next to a label whose text matches label_text.
    2. Clicks the trigger to open the popup.
    3. Waits for the option list to appear and clicks the matching item.
    """
    # Step A — open the dropdown by clicking the trigger near the label
    js_open = """(label) => {
        // Find all text nodes that match the label
        const allEls = Array.from(document.querySelectorAll('label,div,span,p'));
        for (const el of allEls) {
            const own = Array.from(el.childNodes)
                .filter(n => n.nodeType === Node.TEXT_NODE)
                .map(n => n.textContent.trim()).join('');
            if (own !== label && (el.innerText || '').trim() !== label) continue;

            // Walk up to find a row/form-item container
            let container = el.parentElement;
            for (let i = 0; i < 6; i++) {
                if (!container) break;
                // Look for arco select trigger or any trigger inside this container
                const trigger = container.querySelector(
                    '.arco-select-view, .arco-select-view-input, '
                    + '[class*="select-view"], [class*="select-trigger"], '
                    + '[class*="arco-select"]'
                );
                if (trigger) {
                    const r = trigger.getBoundingClientRect();
                    if (r.width > 0) { trigger.click(); return 'opened:' + label; }
                }
                container = container.parentElement;
            }
        }
        return null;
    }"""
    # Step B — pick the option from the open popup
    js_pick = """(option) => {
        // Arco select popup OR any visible list popup
        const popup = document.querySelector(
            '.arco-select-popup .arco-select-option, '
            + '[class*="select-popup"] [class*="option"], '
            + '[class*="dropdown"] li, [class*="select-list"] li'
        );
        if (!popup) return null;
        const items = Array.from(document.querySelectorAll(
            '.arco-select-option, [class*="select-option"], [class*="option-item"]'
        ));
        const visible = items.filter(el => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        });
        for (const el of visible) {
            const t = (el.innerText || '').trim();
            if (t === option) { el.click(); return 'selected:' + option; }
        }
        // Fallback: any visible li / div matching text
        const all = Array.from(document.querySelectorAll('li,div'));
        for (const el of all) {
            if ((el.innerText || '').trim() !== option) continue;
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) { el.click(); return 'fallback:' + option; }
        }
        return null;
    }"""
    try:
        opened = page.evaluate(js_open, label_text)
        if not opened:
            print(f"[Step 1] ⚠ Could not open dropdown for '{label_text}'")
            return False
        time.sleep(1)  # wait for popup to animate open
        picked = page.evaluate(js_pick, option_text)
        if picked:
            print(f"[Step 1] ✓ {label_text} → '{option_text}' ({picked})")
            time.sleep(0.5)
            return True
        else:
            print(f"[Step 1] ⚠ Option '{option_text}' not found in '{label_text}' dropdown")
            # Close the popup by pressing Escape
            page.keyboard.press("Escape")
            return False
    except Exception as e:
        print(f"[Step 1] Dropdown select error ({label_text}): {e}")
        return False


def step1_content(page, story_text: str):
    print("[Step 1] Navigating to Kids Story page...")
    page.goto("https://magiclight.ai/kids-story/", timeout=60000)
    page.wait_for_load_state("domcontentloaded")
    time.sleep(6)
    dismiss_popups(page)
    _dismiss_tour(page)

    print("[Step 1] Pasting story text...")
    textarea = page.locator("textarea[placeholder*='original story']")
    textarea.wait_for(state="visible", timeout=20000)
    textarea.first.evaluate(
        f"el => {{ el.value = {repr(story_text)}; "
        f"el.dispatchEvent(new Event('input', {{bubbles:true}})); }}"
    )
    time.sleep(1)

    print("[Step 1] Selecting Pixar 2.0 style...")
    try:
        pixar = page.locator("text='Pixar 2.0'")
        if pixar.count() > 0 and pixar.first.is_visible():
            pixar.first.click()
            time.sleep(1)
    except Exception:
        print("[Step 1] Pixar 2.0 not found — skipping")

    try:
        r169 = page.locator("text='16:9'")
        if r169.count() > 0 and r169.first.is_visible():
            r169.first.click()
            time.sleep(0.5)
    except Exception:
        pass

    # Scroll down so the Voiceover / Music dropdowns are visible
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    time.sleep(1)

    # ── Voiceover: Sophia ─────────────────────────────────────────────────
    print("[Step 1] Setting voiceover to Sophia...")
    _open_dropdown_and_select(page, "Voiceover", "Sophia")
    time.sleep(0.5)

    # ── Background music: Silica ────────────────────────────────────────────
    print("[Step 1] Setting background music to Silica...")
    _open_dropdown_and_select(page, "Background Music", "Silica")
    time.sleep(0.5)

    print(f"[Step 1] Clicking Next (will wait {STEP1_WAIT}s after)...")
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    time.sleep(1)

    if _click_next_step1(page, timeout=30):
        time.sleep(STEP1_WAIT)
        return

    _dom_debug_buttons(page)
    raise Exception("[Step 1] Could not find Next button")


# ── Step 2: Cast ──────────────────────────────────────────────────────────────
def step2_cast(page):
    print(f"[Step 2] Cast — waiting {STEP2_WAIT}s for characters to generate...")
    time.sleep(STEP2_WAIT)
    dismiss_popups(page)

    print("[Step 2] Clicking Next Step...")
    if _dom_click_class(page, "step2-footer-btn-left", timeout=120):
        print("[Step 2] ✓ Done.")
    elif _dom_click_text(page, ["Next Step", "Animate All", "Create now"], timeout=30):
        print("[Step 2] ✓ Done (fallback).")
    else:
        _dom_debug_buttons(page)
        print("[Step 2] Next Step not found — may have auto-skipped.")

    time.sleep(4)
    _dismiss_animation_modal(page)
    time.sleep(4)


# ── Step 3: Storyboard ────────────────────────────────────────────────────────
def step3_storyboard(page):
    print(f"[Step 3] Storyboard — waiting up to {STEP3_WAIT}s for images...")
    dismiss_popups(page)

    js_count = """() => {
        const imgs = document.querySelectorAll(
            '[class*="role-card"] img,[class*="scene"] img,[class*="storyboard"] img,' +
            '[class*="story-board"] img,[class*="video-scene"] img,[class*="frame"] img'
        );
        return imgs.length;
    }"""
    deadline = time.time() + STEP3_WAIT
    while time.time() < deadline:
        count = page.evaluate(js_count)
        if count >= 2:
            print(f"[Step 3] ✓ Storyboard images ready ({count} found)")
            break
        time.sleep(5)
        print(f"[Step 3] Waiting for images... ({int(deadline - time.time())}s left)")
    else:
        print("[Step 3] Timeout — proceeding anyway")

    time.sleep(3)

    print("[Step 3] Clicking Next Step...")
    if _dom_click_class(page, "step2-footer-btn-left", timeout=20):
        print("[Step 3] ✓ Done.")
    elif _dom_click_text(page, ["Next", "Next Step", "Create now"], timeout=15):
        print("[Step 3] ✓ Done (fallback).")
    else:
        _dom_debug_buttons(page)
        print("[Step 3] Next not found — proceeding to Step 4.")

    time.sleep(4)
    _dismiss_animation_modal(page)
    time.sleep(4)


# ── Step 3b: Edit page — Subtitle settings ─────────────────────────────────────
def step3b_edit_settings(page):
    """
    On the Edit page (Content->Cast->Storyboard->Edit) set:
      Subtitle Settings tab -> 10th style
    """
    print("[Step 3b] Configuring Edit settings (Subtitle style)...")
    dismiss_popups(page)
    time.sleep(2)

    # Click 'Subtitle Settings' tab
    clicked_tab = False
    for tab_text in ["Subtitle Settings", "Subtitle", "Caption"]:
        try:
            tab = page.locator(f"text='{tab_text}'")
            if tab.count() > 0 and tab.first.is_visible():
                tab.first.click()
                print(f"[Step 3b] \u2713 Clicked tab: {tab_text}")
                clicked_tab = True
                time.sleep(1.5)
                break
        except Exception:
            pass
    if not clicked_tab:
        print("[Step 3b] Subtitle Settings tab not found \u2014 trying JS...")
        page.evaluate("""() => {
            const els = Array.from(document.querySelectorAll('div,span,li,a'));
            for (const el of els) {
                const t = (el.innerText || '').trim();
                if ((t === 'Subtitle Settings' || t === 'Subtitle')
                        && el.getBoundingClientRect().width > 0) {
                    el.click(); return t;
                }
            }
        }""")
        time.sleep(1.5)

    # Select the 10th subtitle style card from the visible grid.
    # Real MagicLight class confirmed: .coverFontList-item (17 items total)
    # index 0 = 'No Subtitle', index 9 = 10th card
    result = page.evaluate("""() => {
        // Try the confirmed class first
        let candidates = Array.from(document.querySelectorAll('.coverFontList-item'));
        // Broad fallback selectors if class changes
        if (candidates.length === 0) {
            candidates = Array.from(document.querySelectorAll(
                '[class*="coverFont"] [class*="item"], [class*="coverFont"] li,'
                + '[class*="subtitle-item"], [class*="subtitle-style"] > div,'
                + '[class*="subtitle"] [class*="item"], [class*="subtitle"] [class*="card"],'
                + '[class*="caption-style"] > div, [class*="caption"] [class*="item"]'
            ));
        }
        const visible = candidates.filter(el => {
            const r = el.getBoundingClientRect();
            return r.width > 5 && r.height > 5;
        });
        if (visible.length >= 10) {
            visible[9].click();
            return 'clicked index 10 of ' + visible.length;
        }
        return 'only ' + visible.length + ' items found';
    }""")
    print(f"[Step 3b] Subtitle style: {result}")
    time.sleep(1)


# ── Step 4: Edit → Generate → Wait → Download ─────────────────────────────────
def step4_generate_and_download(page, row_label: str, safe_title: str) -> dict:
    print("[Step 4] Navigating sub-steps to reach Generate screen...")
    dismiss_popups(page)
    time.sleep(3)

    generate_texts = ["Generate", "Create Video", "Export", "Create now", "Render"]

    # ── Navigate to Generate button ──────────────────────────────────────────
    # KEY FIX: Dismiss animation modal FIRST on every attempt before checking
    for attempt in range(STEP4_MAX_NEXT):

        # Always dismiss animation modal at top of each attempt
        _dismiss_animation_modal(page)
        time.sleep(2)
        dismiss_popups(page)

        # Check if Generate is visible now
        js_has_generate = """(texts) => {
            const all = Array.from(document.querySelectorAll(
                'button,div[class*="btn"],span[class*="btn"],div[class*="footer-btn"]'
            ));
            for (let i = all.length - 1; i >= 0; i--) {
                const el = all[i];
                let dt = '';
                el.childNodes.forEach(n => { if (n.nodeType === Node.TEXT_NODE) dt += n.textContent; });
                const t = dt.trim() || (el.innerText || '').trim();
                if (texts.includes(t)) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) return t;
                }
            }
            return null;
        }"""
        found = page.evaluate(js_has_generate, generate_texts)
        if found:
            print(f"[Step 4] ✓ Found '{found}' button after {attempt} Next clicks!")
            break

        print(f"[Step 4] Generate not visible (attempt {attempt+1}/{STEP4_MAX_NEXT}) — clicking Next...")
        result = _click_next_header(page)
        if not result:
            print("[Step 4] No Next button found at all")
            _dom_debug_buttons(page)

        time.sleep(4)   # Wait a bit longer after each Next click
        _dismiss_animation_modal(page)
        time.sleep(3)
        dismiss_popups(page)

    else:
        _dom_debug_buttons(page)
        raise Exception(f"[Step 4] Could not reach Generate after {STEP4_MAX_NEXT} attempts")

    # ── Click Generate ────────────────────────────────────────────────────────
    print("[Step 4] Clicking Generate...")
    if not _dom_click_text(page, generate_texts, timeout=20):
        _dom_debug_buttons(page)
        raise Exception("[Step 4] Generate button click failed")
    time.sleep(3)

    # ── Confirm export popup ──────────────────────────────────────────────────
    print("[Step 4] Confirming export popup (OK)...")
    _dom_click_text(page, ["OK", "Ok", "Confirm"], timeout=10)
    time.sleep(3)
    dismiss_popups(page)

    # ── Wait for render ───────────────────────────────────────────────────────
    PROGRESS_EVERY  = 30
    RELOAD_EVERY    = 120  # reload page every 2 min to pick up state changes
    print(f"[Step 4] ⏳ Waiting for render (up to {STEP4_RENDER_TIMEOUT // 60} min)...")
    print("[Step 4]    MagicLight usually takes 5–10 minutes — please be patient...")

    start             = time.time()
    last_progress_log = start
    last_reload       = start
    render_done       = False

    # JS snippet: true when a visible Download button / video / complete text is present
    js_render_ready = """() => {
        // Check for completion text
        const bodyText = (document.body && document.body.innerText) || '';
        const doneKw = ['video has been generated','Video generated','generation complete',
                        'successfully generated','video is ready','Your video is ready',
                        'Export completed','Export success'];
        for (const kw of doneKw) {
            if (bodyText.includes(kw)) return 'text:' + kw;
        }
        // Check for a real mp4 in a <video> tag
        const vid = document.querySelector('video[src*=".mp4"], video source[src*=".mp4"]');
        if (vid && vid.src) return 'video:' + vid.src.substring(0, 80);
        // Check for a download anchor
        const dlA = document.querySelector('a[href*=".mp4"], a[download]');
        if (dlA && dlA.offsetWidth > 0) return 'anchor:' + (dlA.href || 'no-href').substring(0, 80);
        // Check for visible download button by text
        const allBtns = Array.from(document.querySelectorAll('button,a,div[class*="btn"],span[class*="btn"]'));
        for (const el of allBtns) {
            const t = (el.innerText || '').trim();
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.height > 0 &&
                (t === 'Download' || t === 'Download video' || t === 'Download Video' ||
                 t === 'Save video' || t === 'Export')) {
                return 'btn:' + t;
            }
        }
        // Check for [class*=download] button
        const dlBtn = document.querySelector('[class*="download-btn"],[class*="download_btn"],[class*="export-btn"]');
        if (dlBtn && dlBtn.offsetWidth > 0) return 'dlclass:' + dlBtn.className.substring(0, 60);
        return null;
    }"""

    while time.time() - start < STEP4_RENDER_TIMEOUT:
        elapsed = int(time.time() - start)

        # Reload periodically so the page reflects latest render state
        if time.time() - last_reload >= RELOAD_EVERY:
            try:
                print("[Step 4] 🔄 Reloading page to check render status...")
                page.reload(timeout=30000, wait_until="domcontentloaded")
                time.sleep(5)
                dismiss_popups(page)
            except Exception as reload_err:
                print(f"[Step 4] Reload skipped: {reload_err}")
            last_reload = time.time()

        try:
            ready_signal = page.evaluate(js_render_ready)
            if ready_signal:
                print(f"[Step 4] ✓ Render ready ({elapsed}s) — signal: {ready_signal}")
                render_done = True
                break
        except Exception:
            pass

        # Progress log
        if time.time() - last_progress_log >= PROGRESS_EVERY:
            mins = elapsed // 60
            secs = elapsed % 60
            rem  = STEP4_RENDER_TIMEOUT - elapsed
            print(f"[Step 4] ⏳ {mins}m {secs}s elapsed | {rem // 60}m {rem % 60}s remaining...")
            last_progress_log = time.time()

        time.sleep(STEP4_POLL_INTERVAL)

    if not render_done:
        print(f"[Step 4] ⚠️  Render timeout ({STEP4_RENDER_TIMEOUT // 60} min) — trying to download anyway...")
        _dom_debug_buttons(page)

    time.sleep(5)  # Settle buffer

    # ── Dismiss success popup ─────────────────────────────────────────────────
    for sel in [".arco-modal-close-btn", "button:has-text('×')",
                "[aria-label='Close']", ".popup-close"]:
        try:
            btn = page.locator(sel)
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click()
        except Exception:
            pass
    time.sleep(2)

    # ── Extract metadata from the final result page ───────────────────────────
    video_id = page.url.strip("/").split("/")[-1]
    if not video_id or len(video_id) < 3:
        video_id = f"gen_{int(time.time())}"

    # Label-walking JS: scans page for a label whose visible text matches,
    # then looks inside the same container for an input/textarea/div with content.
    meta = page.evaluate("""() => {
        function getValueByLabel(labelText) {
            // Walk every element, find one whose OWN text (direct text nodes) equals labelText
            const all = Array.from(document.querySelectorAll('div,span,label,p,h3,h4,h5'));
            for (const el of all) {
                // Build text from direct child text nodes only
                const own = Array.from(el.childNodes)
                    .filter(n => n.nodeType === 3)
                    .map(n => n.textContent.trim())
                    .join('');
                if (own !== labelText && (el.innerText || '').trim() !== labelText) continue;
                // Found label element - look for value in parent/sibling containers
                const r = el.getBoundingClientRect();
                if (r.width === 0) continue;  // skip hidden labels

                // Walk up to 5 ancestors looking for an input/textarea
                let container = el.parentElement;
                for (let i = 0; i < 5; i++) {
                    if (!container) break;
                    const inputs = Array.from(container.querySelectorAll(
                        'input, textarea, [contenteditable="true"]'
                    ));
                    for (const inp of inputs) {
                        const v = (inp.value || inp.innerText || inp.textContent || '').trim();
                        if (v && v.length > 2) return v;
                    }
                    container = container.parentElement;
                }
                // Also try next siblings of the label's parent
                let sib = el.parentElement && el.parentElement.nextElementSibling;
                while (sib) {
                    const inp = sib.querySelector('input, textarea, [contenteditable]');
                    if (inp) {
                        const v = (inp.value || inp.innerText || '').trim();
                        if (v && v.length > 2) return v;
                    }
                    const v = (sib.innerText || '').trim();
                    if (v && v.length > 5 && v !== labelText) return v;
                    sib = sib.nextElementSibling;
                }
            }
            return '';
        }

        const title    = getValueByLabel('Title');
        const summary  = getValueByLabel('Summary');
        const hashtags = getValueByLabel('Hashtags');
        return { title, summary, hashtags };
    }""")

    gen_title = (meta or {}).get("title", "").strip()
    summary   = (meta or {}).get("summary", "").strip()
    hashtags  = (meta or {}).get("hashtags", "").strip()
    print(f"[Meta] Title    : {gen_title[:80] if gen_title else 'NOT FOUND'}")
    print(f"[Meta] Summary  : {summary[:80] if summary else 'NOT FOUND'}")
    print(f"[Meta] Hashtags : {hashtags[:80] if hashtags else 'NOT FOUND'}")

    # ── Build local folder ────────────────────────────────────────────────────
    local_folder = os.path.join(DOWNLOADS_DIR, safe_title)
    os.makedirs(local_folder, exist_ok=True)
    print(f"[Download] Local folder: {local_folder}")

    # ── Collect browser cookies for authenticated requests ────────────────────
    def _cookies_dict(pg):
        try:
            return {c["name"]: c["value"] for c in pg.context.cookies()}
        except Exception:
            return {}

    # ── Download Magic Thumbnail ──────────────────────────────────────────────
    # The Magic Thumbnail card is identified by the "Magic Thumbnail" heading text
    # on the page. We locate the img inside that section, and click its Download button.
    thumb_local = ""
    thumb_web   = ""
    try:
        dest = os.path.join(local_folder, f"{safe_title}_thumbnail.jpg")

        # Step 1: get the img URL from the Magic Thumbnail section
        # Walk up from any element containing "Magic Thumbnail" text to find the card,
        # then grab the img inside it.
        thumb_web = page.evaluate("""() => {
            // Find the element whose text includes 'Magic Thumbnail'
            const allEls = Array.from(document.querySelectorAll('div,span,section,article'));
            for (const el of allEls) {
                if (!(el.innerText || '').includes('Magic Thumbnail')) continue;
                // Walk up to find a container wide enough to be the card
                let card = el;
                for (let i = 0; i < 6; i++) {
                    if (!card) break;
                    const img = card.querySelector('img[src]');
                    if (img && img.src && img.src.startsWith('http')
                            && img.naturalWidth >= 100) {
                        return img.src;
                    }
                    card = card.parentElement;
                }
            }
            // Fallback: largest non-logo img on page
            const imgs = Array.from(document.querySelectorAll('img[src]'))
                .filter(img => img.src.startsWith('http')
                    && !img.src.includes('logo')
                    && !img.src.includes('avatar')
                    && !img.src.includes('icon')
                    && img.naturalWidth >= 300)
                .sort((a, b) => (b.naturalWidth * b.naturalHeight) - (a.naturalWidth * a.naturalHeight));
            return imgs.length ? imgs[0].src : null;
        }""") or ""

        # Step 2: click the Download button/link inside the Magic Thumbnail section
        # Specifically look for a link with text "Download" that is NOT "Download video"
        thumb_downloaded = False

        # JS click approach: find visible Download link in the thumbnail section
        js_thumb_dl = """() => {
            const allEls = Array.from(document.querySelectorAll('div,span,section'));
            for (const el of allEls) {
                if (!(el.innerText || '').includes('Magic Thumbnail')) continue;
                // Find 'Download' link/button inside this section
                let card = el;
                for (let i = 0; i < 6; i++) {
                    if (!card) break;
                    const dlBtns = Array.from(card.querySelectorAll('a,button,span,div'));
                    for (const btn of dlBtns) {
                        const t = (btn.innerText || btn.textContent || '').trim();
                        const r = btn.getBoundingClientRect();
                        // Match 'Download' but NOT 'Download video'
                        if ((t === 'Download' || t === '\u2193 Download' || t.startsWith('Download')
                                && !t.toLowerCase().includes('video'))
                                && r.width > 0 && r.height > 0) {
                            btn.click();
                            return 'clicked:' + t;
                        }
                    }
                    card = card.parentElement;
                }
            }
            return null;
        }"""
        try:
            with page.expect_download(timeout=20000) as dl_info:
                result = page.evaluate(js_thumb_dl)
            if result:
                dl = dl_info.value
                dl.save_as(dest)
                thumb_local = dest
                print(f"[Download] ✓ Thumbnail (native DL via JS click) → {dest}")
                thumb_downloaded = True
        except Exception as te:
            print(f"[Download] Thumbnail native click failed: {te}")

        # Step 3: if JS click didn't trigger a download, try requests with the img URL
        if not thumb_downloaded and thumb_web:
            try:
                resp = requests.get(thumb_web, timeout=30, cookies=_cookies_dict(page),
                                    headers={"Referer": page.url,
                                             "User-Agent": "Mozilla/5.0"})
                if resp.status_code == 200 and len(resp.content) > 5000:
                    with open(dest, "wb") as f:
                        f.write(resp.content)
                    thumb_local = dest
                    print(f"[Download] ✓ Thumbnail (requests) → {dest} ({len(resp.content)//1024} KB)")
                else:
                    print(f"[Download] Thumbnail HTTP {resp.status_code} / {len(resp.content)} bytes")
            except Exception as re:
                print(f"[Download] Thumbnail requests failed: {re}")

    except Exception as e:
        print(f"[Download] Thumbnail outer error: {e}")

    # ── Download Video ────────────────────────────────────────────────────────
    video_local = ""
    try:
        # Strategy 1: extract direct mp4 URL and download with session cookies
        js_video_url = """() => {
            // Direct <video src>
            const vid = document.querySelector('video');
            if (vid && vid.src && vid.src.includes('.mp4')) return vid.src;
            // <video><source src>
            const src = document.querySelector('video source');
            if (src && src.src && src.src.includes('.mp4')) return src.src;
            // Any <a href .mp4>
            const a = document.querySelector('a[href*=".mp4"]');
            if (a && a.href) return a.href;
            // Check data-src attributes
            const dsrc = document.querySelector('[data-src*=".mp4"]');
            if (dsrc) return dsrc.getAttribute('data-src');
            // Scan all <source> tags
            for (const s of document.querySelectorAll('source[src]')) {
                if (s.src && s.src.includes('.mp4')) return s.src;
            }
            return null;
        }"""

        video_url = page.evaluate(js_video_url)
        print(f"[Download] Video URL from DOM: {str(video_url)[:120] if video_url else 'NOT FOUND'}")

        if not video_url:
            # Strategy 2: click the Download button via Playwright native download
            print("[Download] Attempting native Playwright download via button click...")
            dl_selectors = [
                "a[download]",
                "a:has-text('Download')",
                "button:has-text('Download')",
                "[class*='download-btn']",
                "[class*='download_btn']",
                "[class*='export-btn']",
                "a[href*='.mp4']",
            ]
            clicked_download = False
            for sel in dl_selectors:
                try:
                    loc = page.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        dest = os.path.join(local_folder, f"{safe_title}.mp4")
                        with page.expect_download(timeout=120000) as dl_info:
                            loc.first.click()
                        download = dl_info.value
                        suggested = download.suggested_filename
                        print(f"[Download] ✓ Native download started: {suggested}")
                        if suggested and not suggested.lower().endswith('.mp4'):
                            dest = os.path.join(local_folder, suggested)
                        download.save_as(dest)
                        video_local = dest
                        print(f"[Download] ✓ Video saved → {dest}")
                        clicked_download = True
                        break
                except Exception as dl_err:
                    print(f"[Download] Native download via '{sel}' failed: {dl_err}")

            if not clicked_download:
                print("[Download] ❌ Could not trigger native download")
                _dom_debug_buttons(page)

        else:
            dest = os.path.join(local_folder, f"{safe_title}.mp4")
            print(f"[Download] Downloading via requests: {str(video_url)[:100]}")
            hdrs = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": page.url,
            }
            r = requests.get(video_url, stream=True, timeout=120,
                             cookies=_cookies_dict(page), headers=hdrs)
            r.raise_for_status()
            total = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        total += len(chunk)
            if total > 10_000:  # at least 10 KB
                video_local = dest
                print(f"[Download] ✓ Video saved → {dest} ({total // 1024} KB)")
            else:
                print(f"[Download] ❌ Downloaded only {total} bytes — file likely invalid")
                os.remove(dest)

    except Exception as e:
        print(f"[Download] Video failed: {e}")

    return {
        "video_id":    video_id,
        "gen_title":   gen_title,
        "summary":     summary,
        "hashtags":    hashtags,
        "thumb_local": thumb_local,
        "thumb_web":   thumb_web,
        "video_local": video_local,
        "local_folder": local_folder,
    }


# ── Multi-Account Management ─────────────────────────────────────────────────────
def get_next_account(sheet_service, spreadsheet_id):
    """Get the best account to use based on usage and status"""
    try:
        # Get accounts sheet
        try:
            accounts_sheet = sheet_service.worksheet(ACCOUNTS_SHEET_NAME)
            print(f"[Accounts] Found '{ACCOUNTS_SHEET_NAME}' sheet")
        except gspread.exceptions.WorksheetNotFound:
            print(f"[ERROR] Accounts sheet '{ACCOUNTS_SHEET_NAME}' not found!")
            print("[ERROR] Please create a sheet named 'Accounts' with columns:")
            print("  A: Email, B: Password, C: Status, D: Daily Usage, E: Last Used, F: Total Generated")
            return None
            
        accounts = accounts_sheet.get_all_records()
        print(f"[Accounts] Found {len(accounts)} accounts")
        
        current_date = datetime.now().strftime("%Y-%m-%d")
        best_account = None
        lowest_usage = 999
        
        for i, account in enumerate(accounts):
            # Try different column name variations
            status = account.get('Status', '').strip() or account.get('status', '').strip() or account.get('Active', '').strip()
            daily_use = int(account.get('Daily Usage', 0) or account.get('Daily Usage', 0) or account.get('Usage', 0) or 0)
            last_used = account.get('Last Used', '').strip() or account.get('Last Used', '').strip() or account.get('Date', '').strip()
            email = account.get('Email', '').strip() or account.get('email', '').strip() or account.get('Account', '').strip()
            
            # If status is empty, assume it's active (for initial setup)
            if not status:
                status = 'active'
                print(f"[Accounts] Assuming {email} is active (status was empty)")
            
            print(f"[Accounts] Account {i+1}: {email} | Status: {status} | Usage: {daily_use}")
            
            # Skip if not active
            if status.lower() != 'active':
                print(f"[Accounts] Skipping {email} - status not active")
                continue
                
            # Reset daily usage if it's a new day
            if last_used != current_date:
                daily_use = 0
                print(f"[Accounts] Reset usage for {email} - new day")
                
            # Find account with lowest usage
            if daily_use < lowest_usage and daily_use < 5:
                lowest_usage = daily_use
                best_account = account
                print(f"[Accounts] Best account so far: {email} with usage {daily_use}")
        
        if best_account:
            # Update usage for selected account
            row_num = accounts.index(best_account) + 2  # +2 for header + 1-based
            accounts_sheet.update_cell(row_num, ACCOL_DAILY_USE, lowest_usage + 1)
            accounts_sheet.update_cell(row_num, ACCOL_LAST_USED, current_date)
            
            print(f"[Accounts] Selected: {best_account.get('Email')} (usage: {lowest_usage}→{lowest_usage+1})")
            
            return {
                'email': best_account.get('Email', ''),
                'password': best_account.get('Password', ''),
                'row': row_num
            }
        else:
            print("[ERROR] No available accounts found! All accounts have reached daily limit.")
            return None
            
    except Exception as e:
        print(f"[ERROR] Failed to get account: {e}")
        import traceback
        traceback.print_exc()
        return None

def update_account_success(sheet_service, spreadsheet_id, account_row, success=True):
    """Update account statistics after video generation"""
    try:
        accounts_sheet = sheet_service.worksheet(ACCOUNTS_SHEET_NAME)
        
        if success:
            # Increment total generated
            current_total = int(accounts_sheet.cell(account_row, ACCOL_TOTAL_GEN).value or 0)
            accounts_sheet.update_cell(account_row, ACCOL_TOTAL_GEN, current_total + 1)
        else:
            # Mark as cooldown if failed
            accounts_sheet.update_cell(account_row, ACCOL_STATUS, 'Cooldown')
            
    except Exception as e:
        print(f"[ERROR] Failed to update account stats: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    global browser_instance

    args  = parse_args()

    print("=" * 60)
    print("  AutoMagicAI — Video Generator")
    print("  1: Generate videos (MagicLight.AI automation)")
    print("=" * 60)
    
    choice = input("Select option 1 to continue: ").strip()
    if choice != "1":
        print("[ERROR] Invalid choice. Exiting.")
        return

    raw_limit = input("How many rows to proceed? (Leave blank for default): ").strip()
    limit = int(raw_limit) if raw_limit.isdigit() else (args.maxstory if args.maxstory is not None else STORIES_PER_RUN)

    # Determine headless mode: CLI args override .env setting
    if args.headless is not None:
        headless_mode = args.headless
    elif args.no_headless is not None:
        headless_mode = False
    else:
        headless_mode = HEADLESS_MODE

    print("=" * 60)
    print(f"  AutoMagicAI — MagicLight.AI Automation")
    print(f"  Stories this run : {limit} | Headless: {headless_mode}")
    print(f"  Timing  → Step1:{STEP1_WAIT}s  Step2:{STEP2_WAIT}s  "
          f"Step3:{STEP3_WAIT}s  Render:{STEP4_RENDER_TIMEOUT}s")
    print("=" * 60)

    if not SPREADSHEET_ID:
        print("[ERROR] SPREADSHEET_ID not set in .env"); return
    if not ML_EMAIL or not ML_PASSWORD:
        print("[ERROR] ML_EMAIL / ML_PASSWORD not set in .env"); return

    print("[Setup] Connecting to Google Sheets...")
    sheet = get_sheet()
    if not sheet:
        return
    # Get all data and handle duplicate headers manually
    all_data = sheet.get_all_values()
    if len(all_data) < 2:
        print("[ERROR] Sheet is empty or has no data rows")
        return
    
    headers = all_data[0]  # First row is headers
    records = []
    
    for row in all_data[1:]:  # Skip header row
        if len(row) >= len(headers):
            record = {}
            for i, header in enumerate(headers):
                if i < len(row):
                    record[header] = row[i]
                else:
                    record[header] = ""
            records.append(record)
    print(f"[Setup] Found {len(records)} rows in sheet.")

    # Get sheet service for multi-account management
    sheet_service = get_sheet_service()
    if not sheet_service:
        print("[ERROR] Could not connect to Google Sheets")
        return

    # Get next available account
    account = get_next_account(sheet_service, SPREADSHEET_ID)
    if not account:
        print("[ERROR] No available accounts. All accounts have reached daily limit.")
        return

    print(f"[Account] Using: {account['email']}")
    current_email = account['email']
    current_password = account['password']
    account_row = account['row']

    drive_service = None
    if DRIVE_FOLDER_ID:
        try:
            drive_service = get_drive_service()
            print("[Setup] ✓ Google Drive connected.")
        except Exception as e:
            print(f"[Setup] Drive error (upload disabled): {e}")
    else:
        print("[Setup] GOOGLE_DRIVE_FOLDER_ID not set — Drive upload disabled.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless_mode, args=["--start-maximized"])
        browser_instance = browser
        context = browser.new_context(accept_downloads=True, no_viewport=True)
        page    = context.new_page()

        try:
            login(page, current_email, current_password)
            print("[Login] ✓ Account login successful")
        except Exception as e:
            print(f"[FATAL] Login failed for {current_email}: {e}")
            # Mark account as cooldown on failure
            update_account_success(sheet_service, SPREADSHEET_ID, account_row, False)
            browser.close()
            return

        processed = 0

        for idx, row in enumerate(records, start=2):
            if shutdown_requested:
                print(f"\n[SHUTDOWN] Stopping after {processed} stories.")
                break
            if processed >= limit:
                print(f"\n[LIMIT] Reached limit of {limit} stories.")
                break
            
            # Get status from the row using multiple possible column names
            status = ""
            if 'Status' in row:
                status = row['Status'].strip().lower()
            elif 'status' in row:
                status = row['status'].strip().lower()
            elif 'E' in row:  # Column E
                status = row['E'].strip().lower()
            
            print(f"[Row {idx}] Status: '{status}'")
            print(f"[Row {idx}] Full row data: {dict(row)}")
            
            # ── Only process stories with "Generated" status ────────────────────────────
            if status != "generated":
                print(f"[Row {idx}] Skipping - status not 'generated'")
                continue

            # ── Check for pending retry (has a Project URL saved) ─────────────
            project_url = str(row.get("Project URL", "") or "").strip()
            story       = row.get("Story Text", "").strip() or row.get("Story", "").strip()
            
            print(f"[Row {idx}] Story: '{story[:50]}...' | Project URL: '{project_url}'")
            
            if not story:
                print(f"[Row {idx}] Skipping - no story content")
                continue

            title_hint = (row.get("Story Title", "") or row.get("Title", "") or f"Row_{idx}").strip() or f"Row_{idx}"
            moral      = row.get("Moral", "").strip()
            hashtags   = row.get("Hashtags", "").strip()

            # Build a filesystem-safe folder name
            safe_title = f"Row_{idx}_{title_hint[:40]}".replace(" ", "_") \
                           .replace("/", "_").replace("\\", "_") \
                           .replace(":", "_").replace("*", "_") \
                           .replace("?", "_").replace('"', "_") \
                           .replace("<", "_").replace(">", "_") \
                           .replace("|", "_")

            prompt = story
            if moral:
                prompt += f"\n\nMoral of the story: {moral}"

            print(f"\n{'='*60}")
            print(f"[Processing] Row {idx}: {title_hint}")
            if project_url:
                print(f"[Processing] Retry mode — using saved Project URL: {project_url}")
            print(f"{'='*60}")

            try:
                max_attempts = int(os.getenv("ROW_MAX_ATTEMPTS", "2"))
                last_err = None
                result = None
                for attempt in range(1, max_attempts + 1):
                    try:
                        if attempt > 1:
                            print(f"[Retry] Attempt {attempt}/{max_attempts}...")

                        # ── If we have a saved project URL, reopen it and continue ──
                        if project_url and "magiclight.ai/project/edit/" in project_url:
                            print(f"[Retry] Navigating to saved project: {project_url}")
                            page.goto(project_url, timeout=60000)
                            page.wait_for_load_state("domcontentloaded")
                            time.sleep(6)
                            dismiss_popups(page)
                            _dismiss_tour(page)

                            # Resume from Storyboard/Generate path
                            step3_storyboard(page)
                            step3b_edit_settings(page)
                            result = step4_generate_and_download(page, safe_title, safe_title)

                        else:
                            # ── Full pipeline ─────────────────────────────────────────
                            step1_content(page, prompt)

                            # ── Save Project URL immediately after Step 1 ─────────────
                            # The URL changes to /project/edit/<id> after clicking Next on Step 1
                            time.sleep(3)
                            current_url = page.url
                            if "project/edit" in current_url:
                                project_url = current_url
                                try:
                                    sheet.update_cell(idx, COL_PROJECT_URL, current_url)
                                    sheet.update_cell(idx, COL_STATUS, "Pending")
                                    print(f"[Sheet] ✓ Project URL saved: {current_url}")
                                except Exception as e:
                                    print(f"[Sheet] Could not save Project URL: {e}")

                            step2_cast(page)
                            step3_storyboard(page)
                            step3b_edit_settings(page)
                            result = step4_generate_and_download(page, safe_title, safe_title)

                        last_err = None
                        break

                    except Exception as e:
                        last_err = e
                        print(f"[ERROR] Row {idx} attempt {attempt} failed: {e}")

                        # Save Project URL on any error so the next attempt can reopen it
                        try:
                            current_url = page.url
                            if current_url and "project/edit" in current_url:
                                project_url = current_url
                                sheet.update_cell(idx, COL_PROJECT_URL, current_url)
                                sheet.update_cell(idx, COL_STATUS, "Pending")
                                sheet.update_cell(idx, COL_NOTES, f"Attempt {attempt} failed: {str(e)[:450]}")
                                print(f"[Sheet] ✓ Project URL saved for retry: {current_url}")
                        except Exception as se:
                            print(f"[Sheet] Could not save Project URL for retry: {se}")

                        time.sleep(3)

                if last_err is not None:
                    raise last_err

                # ── Drive Upload ──────────────────────────────────────────────
                drive_video_url = ""
                drive_thumb_url = ""
                if drive_service and DRIVE_FOLDER_ID:
                    try:
                        # Create Drive folder with same name as local folder
                        drive_subfolder_id = create_drive_folder(
                            drive_service, safe_title, DRIVE_FOLDER_ID
                        )
                        print(f"[Drive] ✓ Folder created: {safe_title}")

                        if result["video_local"] and os.path.exists(result["video_local"]):
                            drive_video_url = upload_to_drive(
                                drive_service, result["video_local"], drive_subfolder_id
                            )
                            print(f"[Drive] ✓ Video → {drive_video_url}")

                        if result["thumb_local"] and os.path.exists(result["thumb_local"]):
                            drive_thumb_url = upload_to_drive(
                                drive_service, result["thumb_local"], drive_subfolder_id
                            )
                            print(f"[Drive] ✓ Thumbnail → {drive_thumb_url}")

                    except Exception as e:
                        print(f"[Drive] Upload error: {e}")

                # ── Update Sheet with all results ─────────────────────────────
                final_title = result["gen_title"] or title_hint
                video_ok    = bool(result["video_local"] and os.path.exists(result["video_local"]))
                new_status  = "Done" if video_ok else "Failed"
                notes       = f"{'Video OK' if video_ok else 'No video'} | local: {result['local_folder']}"

                try:
                    sheet.update_cell(idx, COL_STATUS,      new_status)
                    sheet.update_cell(idx, COL_THUMB_URL,   drive_thumb_url or result["thumb_web"])
                    sheet.update_cell(idx, COL_VIDEO_ID,    result["video_id"])
                    sheet.update_cell(idx, COL_GEN_TITLE,   final_title)
                    sheet.update_cell(idx, COL_SUMMARY,     result["summary"])
                    sheet.update_cell(idx, COL_GEN_HASH,    result["hashtags"])
                    sheet.update_cell(idx, COL_NOTES,       notes)
                    sheet.update_cell(idx, COL_PROJECT_URL, page.url)
                    print(f"[Sheet] ✓ Row {idx} updated → Status: {new_status}")
                except Exception as sheet_err:
                    print(f"[Sheet] ❌ Failed to update row {idx}: {sheet_err}")

            except Exception as e:
                print(f"[ERROR] Row {idx} failed: {e}")
                try:
                    sheet.update_cell(idx, COL_STATUS, "Error")
                    sheet.update_cell(idx, COL_NOTES,  str(e)[:500])
                except Exception:
                    pass
            
            # Unconditionally increment processed count after attempting the row
            processed += 1

        print(f"\n{'='*60}")
        print(f"  Done! Processed {processed}/{limit} stories.")
        print(f"{'='*60}")

        print("[Done] Closing browser automatically...")
        time.sleep(3)

        browser.close()
        browser_instance = None


if __name__ == "__main__":
    main()
