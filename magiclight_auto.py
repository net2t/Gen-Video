"""
MagicLight Auto — Kids Story Video Generator
=============================================
Version : 1.0.0
Released: 2026-04-01
Repo    : https://github.com/<your-username>/VideoAutomation

CSV: stories.csv  ->  output/row{N}_{title}/

Usage:
    python magiclight_auto.py              # Process all Pending rows
    python magiclight_auto.py --max 2      # Process max 2 stories
    python magiclight_auto.py --headless   # No browser window

Multi-account (.env):
    ACCOUNTS=user1@x.com:pass1,user2@x.com:pass2
    EMAIL/PASSWORD are fallback if ACCOUNTS is not set.

──────────────────────────────────────────────────────────────────────────────
  ⚠️  SECURITY LOCK — READ BEFORE EDITING  ⚠️
──────────────────────────────────────────────────────────────────────────────
The following core functions are STABLE and TESTED. Do NOT refactor, rename,
or "improve" them without a full regression test (single + multi-story run):

  login()                   — .entry-email tab click + input[type=text] fill
  _dismiss_animation_modal()— uses .arco-modal-mask as real-dialog signal
  step4() / js_header_next  — ONLY clicks header-shiny-action__btn for Next
  _dismiss_all()            — generic banner/popup killer, NOT dialog-aware

Rules for AI-assisted edits:
  1. Never change JS selector strings without DOM verification.
  2. Never replace sleep_log() / _wait_dismissing() with plain time.sleep().
  3. Never add handle_generation_dialog() back into the step4 loop.
  4. Bump the Version string above for every change.
  5. Add an entry to CHANGELOG.md.
──────────────────────────────────────────────────────────────────────────────
"""

__version__ = "1.0.0"

import re
import os
import csv
import time
import signal
import argparse
import requests
from datetime import datetime
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

EMAIL    = os.getenv("EMAIL", "")
PASSWORD = os.getenv("PASSWORD", "")

_raw_accounts = os.getenv("ACCOUNTS", "").strip()
ACCOUNTS = []
if _raw_accounts:
    for _pair in _raw_accounts.split(","):
        _parts = _pair.strip().split(":", 1)
        if len(_parts) == 2:
            ACCOUNTS.append({"email": _parts[0].strip(), "password": _parts[1].strip()})
if not ACCOUNTS and EMAIL and PASSWORD:
    ACCOUNTS.append({"email": EMAIL, "password": PASSWORD})

_current_account_idx = 0

STEP1_WAIT     = int(os.getenv("STEP1_WAIT",            "60"))
STEP2_WAIT     = int(os.getenv("STEP2_WAIT",            "30"))
STEP3_WAIT     = int(os.getenv("STEP3_WAIT",           "180"))
RENDER_TIMEOUT = int(os.getenv("STEP4_RENDER_TIMEOUT", "1200"))
POLL_INTERVAL  = 10
RELOAD_INTERVAL = 120

CSV_FILE  = "stories.csv"
OUT_BASE  = "output"
OUT_SHOTS = os.path.join(OUT_BASE, "screenshots")

CSV_FIELDS = [
    "Status", "Theme", "Title", "Story",
    "Gen_Title", "Summary", "Tags",
    "Video_Path", "Thumb_Path", "Project_URL", "Notes",
    "Created_Time", "Completed_Time",
]

_shutdown = False
_browser  = None

def _sig(sig, frame):
    global _shutdown, _browser
    print("\n[STOP] Ctrl+C — cleaning up...")
    _shutdown = True
    if _browser:
        try: _browser.close()
        except: pass
    import sys; sys.exit(0)

signal.signal(signal.SIGINT, _sig)

for _d in [OUT_BASE, OUT_SHOTS]:
    os.makedirs(_d, exist_ok=True)

# ── CSV ────────────────────────────────────────────────────────────────────────
def ensure_csv():
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=CSV_FIELDS).writeheader()
        print(f"[CSV] Created {CSV_FILE} — add stories and re-run.")
        return False
    return True

def read_csv():
    with open(CSV_FILE, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def write_csv(rows):
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)

def update_row(idx, **kw):
    rows = read_csv()
    if 0 <= idx < len(rows):
        rows[idx].update(kw); write_csv(rows)

def story_dir(safe_name):
    d = os.path.join(OUT_BASE, safe_name)
    os.makedirs(d, exist_ok=True)
    return d

# ── Sleep ──────────────────────────────────────────────────────────────────────
def sleep_log(seconds, reason=""):
    secs = int(seconds)
    if secs <= 0: return
    label = f" ({reason})" if reason else ""
    print(f"  [wait] {secs}s{label}...")
    for _ in range(secs):
        if _shutdown: return
        time.sleep(1)

def _wait_dismissing(page, seconds, reason=""):
    """Wait in 5-second chunks, dismissing popups each chunk."""
    label = f" ({reason})" if reason else ""
    print(f"  [wait] {seconds}s{label} (popup-watching)...")
    elapsed = 0
    while elapsed < seconds:
        if _shutdown: return
        chunk = min(5, seconds - elapsed)
        for _ in range(chunk):
            if _shutdown: return
            time.sleep(1)
        elapsed += chunk
        _dismiss_all(page)
        if elapsed % 30 == 0 and elapsed < seconds:
            print(f"  [wait] ...{seconds - elapsed}s remaining")

# ── Popups ─────────────────────────────────────────────────────────────────────
def _all_frames(page):
    try: return page.frames
    except: return [page]

# Close generic popups / banners (NOT the animation panel or enhance dialog)
_CLOSE_SELECTORS = [
    'button.notice-popup-modal__close',
    'button[aria-label="close"]',
    'button[aria-label="Close"]',
    '.sora2-modal-close',
    'button:has-text("Got it")',
    'button:has-text("Got It")',
    'button:has-text("Close samples")',
    'button:has-text("Later")',
    'button:has-text("Not now")',
    'button:has-text("No thanks")',
    '.notice-bar__close',
]

_POPUP_JS = """\
() => {
    const BAD = ["Got it","Got It","Close","Done","OK","Later","No thanks",
                 "Maybe later","Not now","Dismiss","Close samples","No","Cancel"];
    let n = 0;
    document.querySelectorAll('button,span,div,a').forEach(el => {
        const t = (el.innerText || el.textContent || '').trim();
        if (BAD.includes(t)) {
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) { el.click(); n++; }
        }
    });
    document.querySelectorAll(
        '.arco-modal-mask,.driver-overlay,.diy-tour__mask,[class*="tour-mask"],[class*="modal-mask"]'
    ).forEach(el => { try { el.style.display='none'; } catch(e){} });
    return n;
}"""

def _dismiss_all(page):
    for fr in _all_frames(page):
        try: fr.evaluate(_POPUP_JS)
        except: pass
        for sel in _CLOSE_SELECTORS:
            try:
                loc = fr.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=1000)
            except: pass
        try: fr.keyboard.press("Escape")
        except: pass

def dismiss_popups(page, timeout=10, sweeps=3):
    for _ in range(sweeps):
        if _shutdown: return
        _dismiss_all(page)
        time.sleep(0.8)

# ── Animation modal / enhance dialog closer ────────────────────────────────────
# Based on DOM dump from actual run:
#   BUTTON.animation-modal__tab | Animate One
#   BUTTON.animation-modal__tab | Animate All
#   DIV.shiny-button-container  | Next / Animate All
#   DIV.arco-modal-mask (backdrop) = REAL blocking dialog
#
# Strategy:
#   A) If .arco-modal-mask (backdrop) visible → real dialog → check checkbox + close X
#   B) If animation-modal__tab buttons visible → animation panel → press Escape / close btn
#   C) Never click the panel's "Next" — that only dismisses in-place

_REAL_DIALOG_JS = """\
() => {
    const masks = Array.from(document.querySelectorAll(
        '.arco-modal-mask,[class*="modal-mask"]'
    )).filter(el => {
        const r = el.getBoundingClientRect();
        return r.width > 100 && r.height > 100;
    });
    if (!masks.length) return null;

    const chk = Array.from(document.querySelectorAll(
        'input[type="checkbox"],.arco-checkbox-icon,label[class*="checkbox"]'
    )).find(el => {
        const par = el.closest('label') || el.parentElement;
        const txt = ((par && par.innerText) || el.innerText || '').toLowerCase();
        return txt.includes('remind') || txt.includes('again') || txt.includes('ask');
    });
    if (chk) { try { chk.click(); } catch(e) {} }

    const xBtn = document.querySelector(
        '.arco-modal-close-btn,[aria-label="Close"],[aria-label="close"],' +
        '.arco-icon-close,[class*="modal-close"],[class*="close-icon"]'
    );
    if (xBtn && xBtn.getBoundingClientRect().width > 0) {
        xBtn.click(); return 'dialog: closed X';
    }
    const wrapper = document.querySelector('.arco-modal-wrapper');
    if (wrapper) {
        wrapper.remove();
        masks.forEach(m => m.remove());
        return 'dialog: removed wrapper';
    }
    return 'dialog: mask found but no X';
}"""

_ANIM_PANEL_JS = """\
() => {
    const tabs = Array.from(document.querySelectorAll(
        '[class*="animation-modal__tab"],[class*="animation-modal-tab"]'
    )).filter(el => el.getBoundingClientRect().width > 0);
    if (!tabs.length) return null;

    const closeEl = Array.from(document.querySelectorAll(
        '[class*="animation-modal"] [class*="close"],' +
        '[class*="animation-modal"] [class*="back"],' +
        '[class*="shiny-button-container"] [class*="close"]'
    )).find(el => el.getBoundingClientRect().width > 0);
    if (closeEl) { closeEl.click(); return 'anim-panel: closed'; }
    return 'anim-panel: press-escape';
}"""

def _dismiss_animation_modal(page):
    # Try real dialog (has backdrop mask)
    try:
        r = page.evaluate(_REAL_DIALOG_JS)
        if r:
            print(f"  [modal] {r}")
            time.sleep(2)
            return
    except: pass

    # Try animation panel
    try:
        r = page.evaluate(_ANIM_PANEL_JS)
        if r:
            print(f"  [modal] {r}")
            try: page.keyboard.press("Escape")
            except: pass
            time.sleep(1.5)
            return
    except: pass

    # Playwright fallback for real dialog
    for sel in ["label:has-text(\"Don't remind again\")", "label:has-text(\"Don't ask again\")"]:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(timeout=1500); time.sleep(0.5)
        except: pass

    for sel in ['.arco-modal-close-btn', 'button[aria-label="Close"]', '.arco-icon-close']:
        try:
            loc = page.locator(sel).first
            if loc.is_visible():
                loc.click(timeout=2000)
                print(f"  [modal] closed via '{sel}'")
                time.sleep(2); return
        except: pass

    try: page.keyboard.press("Escape"); time.sleep(0.5)
    except: pass

def _close_preview_popup(page):
    """Dismiss post-render preview modal to reveal Download button."""
    js = """\
() => {
    let n = 0;
    document.querySelectorAll(
        '.arco-modal-close-btn,[aria-label="Close"],[aria-label="close"],' +
        '[class*="modal-close"],[class*="close-btn"]'
    ).forEach(el => {
        const r = el.getBoundingClientRect();
        if (r.width > 0) { el.click(); n++; }
    });
    document.querySelectorAll(
        '.arco-modal-mask,[class*="modal-mask"],[class*="overlay"]'
    ).forEach(el => { try { el.style.display='none'; } catch(e){} });
    return n;
}"""
    for _ in range(4):
        if _shutdown: return
        try: page.evaluate(js)
        except: pass
        for sel in ['.arco-modal-close-btn', 'button[aria-label="Close"]',
                    'button:has-text("Close")', 'button:has-text("Cancel")']:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=2000)
            except: pass
        try: page.keyboard.press("Escape")
        except: pass
        time.sleep(0.8)

# ── DOM helpers ────────────────────────────────────────────────────────────────
def wait_site_loaded(page, key_locator=None, timeout=60):
    try: page.wait_for_load_state("domcontentloaded", timeout=timeout * 1000)
    except: pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _shutdown: return False
        try:
            if page.evaluate("document.readyState") in ("interactive", "complete"):
                break
        except: pass
        time.sleep(0.3)
    if key_locator is not None:
        try:
            key_locator.wait_for(
                state="visible",
                timeout=max(1000, int((deadline - time.time()) * 1000))
            )
        except: return False
    return True

def dom_click_text(page, texts, timeout=60):
    js = """\
(texts) => {
    const all = Array.from(document.querySelectorAll(
        'button,div[class*="btn"],span[class*="btn"],a,' +
        'div[class*="vlog-btn"],div[class*="footer-btn"],' +
        'div[class*="shiny-action"],div[class*="header-left-btn"]'
    ));
    for (let i = all.length - 1; i >= 0; i--) {
        const el = all[i]; let dt = '';
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
        if _shutdown: return False
        r = page.evaluate(js, texts)
        if r:
            print(f"  [click] '{r}'")
            return True
        time.sleep(2)
    return False

def dom_click_class(page, cls, timeout=30):
    js = f"""\
() => {{
    const all = Array.from(document.querySelectorAll('[class*="{cls}"]'));
    for (let i = all.length-1; i >= 0; i--) {{
        const el = all[i], r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) {{ el.click(); return el.className; }}
    }}
    return null;
}}"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _shutdown: return False
        r = page.evaluate(js)
        if r:
            print(f"  [click-class] ~'{cls}'")
            return True
        time.sleep(2)
    return False

def screenshot(page, name):
    path = os.path.join(OUT_SHOTS, f"{name}_{int(time.time())}.png")
    try: page.screenshot(path=path, full_page=True)
    except: pass
    return path

def debug_buttons(page):
    js = """\
() => Array.from(document.querySelectorAll(
    'button,div[class*="btn"],span[class*="btn"],a,div[class*="vlog-btn"]'
)).filter(el => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && (el.innerText || '').trim();
}).map(el =>
    el.tagName + '.' + el.className.substring(0, 40) +
    ' | ' + (el.innerText || '').trim().substring(0, 60)
);"""
    try:
        items = page.evaluate(js)
        print(f"  [debug-url] {page.url}")
        for i in (items or []): print(f"    {i}")
    except: pass

# ── Accounts ───────────────────────────────────────────────────────────────────
def _auth_json_path(account):
    safe = re.sub(r"[^\w]", "_", account["email"])
    return f"auth_{safe}.json"

def _get_account():
    return ACCOUNTS[_current_account_idx % len(ACCOUNTS)]

def next_account():
    global _current_account_idx
    _current_account_idx += 1
    if _current_account_idx >= len(ACCOUNTS):
        print("[Account] All accounts exhausted!")
        return None
    acc = ACCOUNTS[_current_account_idx]
    print(f"[Account] Switching to {_current_account_idx+1}/{len(ACCOUNTS)}: {acc['email']}")
    return acc

def _credit_exhausted(page):
    try:
        body = page.evaluate("() => (document.body && document.body.innerText) || ''")
        for kw in ["insufficient credits","not enough credits","credit limit",
                   "out of credits","credits exhausted","quota exceeded"]:
            if kw in body.lower():
                return True
    except: pass
    return False

# ── LOGIN ──────────────────────────────────────────────────────────────────────
def login(page, account=None):
    if account is None:
        account = _get_account()
    email    = account["email"]
    password = account["password"]
    auth_file = _auth_json_path(account)

    print(f"[Login] Account: {email}")
    page.goto("https://magiclight.ai/login/?to=%252Fkids-story%252F", timeout=60000)
    try: page.wait_for_load_state("domcontentloaded", timeout=30000)
    except: pass
    sleep_log(4, "page settle")

    if "login" not in page.url.lower():
        print("[Login] Saved session valid")
        sleep_log(2)
        _dismiss_post_login_popups(page)
        return True

    print("[Login] Filling credentials...")

    for attempt in range(3):
        try:
            el = page.locator(".entry-email")
            el.wait_for(state="visible", timeout=10000)
            el.click()
            print("  [login] .entry-email clicked")
            break
        except:
            print(f"  [login] .entry-email attempt {attempt+1}/3 failed")
            sleep_log(2)

    sleep_log(2, "inputs settle")

    email_filled = False
    for sel in ['input[type="text"]', 'input[type="email"]', 'input[name="email"]',
                'input[placeholder*="mail" i]']:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=6000)
            loc.scroll_into_view_if_needed()
            loc.click(); time.sleep(0.3)
            loc.fill(email)
            print(f"  [login] Email filled via '{sel}'")
            email_filled = True; break
        except: continue

    if not email_filled:
        debug_buttons(page)
        raise Exception(f"Login failed — email input not found for {email}")

    time.sleep(0.4)

    pass_filled = False
    for sel in ['input[type="password"]', 'input[name="password"]',
                'input[placeholder*="password" i]']:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=6000)
            loc.scroll_into_view_if_needed()
            loc.click(); time.sleep(0.3)
            loc.fill(password)
            print(f"  [login] Password filled via '{sel}'")
            pass_filled = True; break
        except: continue

    if not pass_filled:
        raise Exception(f"Login failed — password input not found for {email}")

    time.sleep(0.4)

    clicked = False
    for attempt in range(3):
        for sel in ["text=Continue", "div.signin-continue",
                    "button:has-text('Continue')", "a:has-text('Continue')"]:
            try:
                el = page.locator(sel).first
                if el.is_visible():
                    el.click(); clicked = True
                    print(f"  [login] Continue via '{sel}'"); break
            except: pass
        if clicked: break
        sleep_log(1, f"retry Continue {attempt+1}")

    if not clicked:
        debug_buttons(page)
        raise Exception(f"Login failed — Continue not found for {email}")

    try:
        page.wait_for_url(lambda u: "login" not in u.lower(), timeout=30000)
    except:
        sleep_log(8, "redirect wait")

    if "login" in page.url.lower():
        raise Exception(f"Login failed — still on login page for {email}")

    print(f"[Login] Logged in -> {page.url}")
    sleep_log(3, "post-login popups")
    _dismiss_post_login_popups(page)

    try:
        page.context.storage_state(path=auth_file)
        print(f"[Login] Session saved -> {auth_file}")
    except: pass
    return True


def _dismiss_post_login_popups(page):
    print("[Login] Dismissing post-login popups...")
    js = """\
() => {
    let n = 0;
    document.querySelectorAll(
        'button.notice-popup-modal__close,button[aria-label="close"],' +
        'button[aria-label="Close"],.sora2-modal-close,.arco-modal-close-btn,.notice-bar__close'
    ).forEach(el => {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) { el.click(); n++; }
    });
    const texts = ["Skip","Got it","Got It","Close","Done","Later",
                   "Not now","Maybe later","Close samples","No thanks","Dismiss"];
    document.querySelectorAll('button,div[role="button"],a').forEach(el => {
        const t = (el.innerText || '').trim();
        if (texts.includes(t)) {
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) { el.click(); n++; }
        }
    });
    document.querySelectorAll(
        ".arco-modal-mask,[class*='modal-mask'],.diy-tour__mask,[class*='tour-mask']"
    ).forEach(el => { try { el.style.display = 'none'; } catch(e){} });
    return n;
}"""
    for i in range(6):
        if _shutdown: return
        try:
            n = page.evaluate(js)
            if n: print(f"  [popup] round {i+1}: {n} dismissed")
        except: pass
        time.sleep(1.2)

    for sel in ["button:has-text('Skip')", "button:has-text('Close samples')",
                "button:has-text('Got it')", "button:has-text('Got It')",
                "button.notice-popup-modal__close", ".arco-modal-close-btn"]:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(timeout=2000); time.sleep(0.6)
        except: pass

    try: page.keyboard.press("Escape"); time.sleep(0.5)
    except: pass
    print("[Login] Post-login popups done")

# ── STEP 1: Story Input ────────────────────────────────────────────────────────
def step1(page, story_text):
    print("[Step 1] Story input...")
    page.goto("https://magiclight.ai/kids-story/", timeout=60000)
    wait_site_loaded(page, None, timeout=60)
    dismiss_popups(page, timeout=10)

    ta = page.get_by_role("textbox", name="Please enter an original")
    wait_site_loaded(page, ta, timeout=60)
    dismiss_popups(page, timeout=6)
    ta.wait_for(state="visible", timeout=20000)
    ta.click(); ta.fill(story_text)
    print("  [step1] Story filled"); sleep_log(1)

    try:
        page.locator("div").filter(has_text=re.compile(r"^Pixar 2\.0$")).first.click()
        print("  [step1] Pixar 2.0 selected"); time.sleep(0.5)
    except: print("  [step1] Pixar 2.0 not found")

    try:
        page.locator("div").filter(has_text=re.compile(r"^16:9$")).first.click()
        print("  [step1] 16:9 selected"); time.sleep(0.5)
    except: print("  [step1] 16:9 not found")

    page.evaluate("window.scrollTo(0, document.body.scrollHeight)"); sleep_log(1)
    _select_dropdown(page, "Voiceover", "Sophia")
    _select_dropdown(page, "Background Music", "Silica")
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)"); sleep_log(1)

    clicked = False
    for sel in ["button.arco-btn-primary:has-text('Next')", "button:has-text('Next')",
                ".vlog-bottom", "div[class*='footer-btn']:has-text('Next')"]:
        try:
            el = page.locator(sel)
            if el.count() > 0 and el.first.is_visible():
                el.first.click(); clicked = True; break
        except: pass

    if not clicked:
        clicked = dom_click_text(page, ["Next", "Next Step", "Continue"], timeout=20)

    if not clicked:
        debug_buttons(page)
        raise Exception("Step 1 Next button not found")

    print("  [step1] Next clicked")
    _wait_dismissing(page, STEP1_WAIT, "step1 AI generation")


def _select_dropdown(page, label_text, option_text):
    js_open = """\
(label) => {
    const all = Array.from(document.querySelectorAll('label,div,span,p'));
    for (const el of all) {
        const own = Array.from(el.childNodes)
            .filter(n => n.nodeType === 3).map(n => n.textContent.trim()).join('');
        if (own !== label && (el.innerText || '').trim() !== label) continue;
        let c = el.parentElement;
        for (let i = 0; i < 6; i++) {
            if (!c) break;
            const t = c.querySelector('.arco-select-view,.arco-select-view-input,' +
                '[class*="select-view"],[class*="arco-select"]');
            if (t && t.getBoundingClientRect().width > 0) { t.click(); return label; }
            c = c.parentElement;
        }
    }
    return null;
}"""
    js_pick = """\
(opt) => {
    const items = Array.from(document.querySelectorAll(
        '.arco-select-option,[class*="select-option"],[class*="option-item"]'
    )).filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
    for (const el of items)
        if ((el.innerText || '').trim() === opt) { el.click(); return opt; }
    return null;
}"""
    try:
        r = page.evaluate(js_open, label_text)
        if r:
            time.sleep(0.8)
            r2 = page.evaluate(js_pick, option_text)
            if r2: print(f"  [step1] {label_text} -> {option_text}")
            else:
                page.keyboard.press("Escape")
                print(f"  [step1] '{option_text}' not found in {label_text}")
        else:
            print(f"  [step1] {label_text} dropdown not found")
    except Exception as e:
        print(f"  [step1] dropdown error: {e}")

# ── STEP 2: Cast ───────────────────────────────────────────────────────────────
def step2(page):
    print(f"[Step 2] Cast — waiting {STEP2_WAIT}s...")
    dismiss_popups(page, timeout=5)
    _wait_dismissing(page, STEP2_WAIT, "characters generate")
    dismiss_popups(page, timeout=5)

    if not dom_click_class(page, "step2-footer-btn-left", timeout=60):
        dom_click_text(page, ["Next Step", "Next", "Animate All"], timeout=30)

    sleep_log(4)
    # Close any animation panel that opened
    _dismiss_animation_modal(page)
    sleep_log(3)
    print("[Step 2] done")

# ── STEP 3: Storyboard ─────────────────────────────────────────────────────────
def step3(page):
    print(f"[Step 3] Storyboard — up to {STEP3_WAIT}s...")
    dismiss_popups(page, timeout=5)

    js_img = """\
() => document.querySelectorAll(
    '[class*="role-card"] img,[class*="scene"] img,' +
    '[class*="storyboard"] img,[class*="story-board"] img'
).length"""

    deadline = time.time() + STEP3_WAIT
    while time.time() < deadline:
        if _shutdown: break
        if page.evaluate(js_img) >= 2: break
        _dismiss_all(page)
        time.sleep(5)
        print(f"  waiting... {int(deadline - time.time())}s left")

    sleep_log(3)
    _set_subtitle_style(page)

    if not dom_click_class(page, "step2-footer-btn-left", timeout=20):
        dom_click_text(page, ["Next", "Next Step"], timeout=15)

    sleep_log(4)
    _dismiss_animation_modal(page)
    sleep_log(3)
    print("[Step 3] done")


def _set_subtitle_style(page):
    for txt in ["Subtitle Settings", "Subtitle", "Caption"]:
        try:
            t = page.locator(f"text='{txt}'")
            if t.count() > 0 and t.first.is_visible():
                t.first.click(); sleep_log(2); break
        except: pass
    result = page.evaluate("""\
() => {
    let items = Array.from(document.querySelectorAll('.coverFontList-item'));
    if (!items.length) items = Array.from(document.querySelectorAll(
        '[class*="coverFont"] [class*="item"],[class*="subtitle-item"]'
    ));
    const vis = items.filter(el => {
        const r = el.getBoundingClientRect(); return r.width > 5 && r.height > 5;
    });
    if (vis.length >= 10) { vis[9].click(); return 'subtitle style #10 set'; }
    return 'only ' + vis.length + ' items';
}""")
    print(f"  [step3] {result}")

# ── STEP 4: Generate + Wait + Download ────────────────────────────────────────
def step4(page, safe_name):
    print("[Step 4] Navigating to Generate...")
    MAX_NEXT = 12

    # ── js: is there a real blocking modal mask? ──────────────────────────────
    # From debug dump: .arco-modal-mask is the backdrop for real dialogs.
    # animation-modal__tab = the animation editor panel (NOT a blocking modal).
    js_modal_blocking = """\
() => {
    // Only count as 'blocking' if a full-page backdrop mask is visible
    const masks = Array.from(document.querySelectorAll(
        '.arco-modal-mask,[class*="modal-mask"]'
    )).filter(el => {
        const r = el.getBoundingClientRect();
        return r.width > 200 && r.height > 200;
    });
    if (masks.length) return 'mask';

    // Animation panel open? (has backdrop with animation tabs)
    // These have shiny-button-container Next/Animate All on top of storyboard
    // but NO full-page mask — the panel is a slide-in, not a blocking modal.
    return null;
}"""

    # ── js: click only the header navigation Next ─────────────────────────────
    # From debug dump, the header Next is:
    #   DIV.header-shiny-action__btn header-shiny-ac | Next
    js_header_next = """\
() => {
    if (typeof Node === 'undefined') return null;

    // Priority 1: header-shiny-action__btn (confirmed from debug dump)
    for (const el of Array.from(document.querySelectorAll(
        '[class*="header-shiny-action__btn"],[class*="header-left-btn"]'
    ))) {
        const t = (el.innerText || '').trim();
        const r = el.getBoundingClientRect();
        if (t === 'Next' && r.width > 0) { el.click(); return 'header-shiny: Next'; }
    }

    // Priority 2: primary button
    for (const el of Array.from(document.querySelectorAll('button.arco-btn-primary'))) {
        const t = (el.innerText || '').trim();
        const r = el.getBoundingClientRect();
        if (t === 'Next' && r.width > 0) { el.click(); return 'arco-primary: Next'; }
    }
    return null;
}"""

    # ── js: is Generate button visible? ──────────────────────────────────────
    js_has_gen = """\
() => {
    const texts = ["Generate","Create Video","Export","Create now","Render"];
    const all = Array.from(document.querySelectorAll(
        'button,div[class*="btn"],span[class*="btn"],div[class*="footer-btn"]'
    ));
    for (let i = all.length-1; i >= 0; i--) {
        const el = all[i]; let dt = '';
        el.childNodes.forEach(n => { if (n.nodeType === Node.TEXT_NODE) dt += n.textContent; });
        const t = dt.trim() || (el.innerText || '').trim();
        if (texts.includes(t)) {
            const r = el.getBoundingClientRect();
            if (r.width > 0) return t;
        }
    }
    return null;
}"""

    for attempt in range(MAX_NEXT):
        # Close any real dialog first
        _dismiss_animation_modal(page)
        sleep_log(2)

        # Check if Generate is already visible
        found = page.evaluate(js_has_gen)
        if found:
            print(f"  [step4] Generate found after {attempt} attempts: '{found}'")
            break

        # Check for blocking mask
        blocking = page.evaluate(js_modal_blocking)
        if blocking:
            print(f"  [step4] Real modal blocking ({blocking}) — re-dismissing")
            _dismiss_animation_modal(page)
            sleep_log(3)
            continue

        # Click header Next
        r = page.evaluate(js_header_next)
        print(f"  [step4] attempt {attempt+1}: {r or 'no header Next'}")
        if not r:
            # No header Next visible either — check page state
            debug_buttons(page)
        sleep_log(4)
    else:
        debug_buttons(page)
        raise Exception("Could not reach Generate button after max attempts")

    if not dom_click_text(page, ["Generate", "Create Video", "Export", "Create now"], timeout=20):
        debug_buttons(page)
        raise Exception("Generate click failed")

    sleep_log(3)
    dom_click_text(page, ["OK", "Ok", "Confirm"], timeout=5)
    sleep_log(3)
    _dismiss_all(page)

    # ── Wait for render ────────────────────────────────────────────────────────
    print(f"[Step 4] Waiting for render (max {RENDER_TIMEOUT//60} min)...")
    start = time.time(); last_reload = start; render_done = False

    js_state = """\
() => {
    const prog = Array.from(document.querySelectorAll(
        '[class*="progress"],[class*="Progress"],[class*="render-progress"],[class*="generating"]'
    )).filter(el => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && (el.innerText || '').match(/[0-9]+\s*%/);
    });
    if (prog.length > 0) {
        const m = (prog[0].innerText || '').match(/(\d+)\s*%/);
        return 'progress:' + (m ? m[1] : '?') + '%';
    }
    const body = (document.body && document.body.innerText) || '';
    const kws = ['video has been generated','generation complete',
                 'successfully generated','video is ready','Export completed'];
    for (const k of kws)
        if (body.toLowerCase().includes(k.toLowerCase())) return 'text:' + k;
    const vid = document.querySelector('video[src*=".mp4"],video source[src*=".mp4"]');
    if (vid && vid.src) return 'video:' + vid.src.substring(0, 60);
    const btns = Array.from(document.querySelectorAll('button,a,div[class*="btn"]'));
    for (const el of btns) {
        const t = (el.innerText || '').trim();
        const r = el.getBoundingClientRect();
        if (r.width > 0 && (t === 'Download' || t === 'Download video' || t === 'Download Video'))
            return 'btn:' + t;
    }
    const anc = document.querySelector('a[href*=".mp4"],a[download]');
    if (anc && anc.offsetWidth > 0) return 'anchor';
    return null;
}"""

    last_pct = ""
    while time.time() - start < RENDER_TIMEOUT:
        if _shutdown: break
        elapsed = int(time.time() - start)

        if time.time() - last_reload >= RELOAD_INTERVAL:
            try:
                print(f"  [step4] Reloading... ({elapsed//60}m elapsed)")
                page.reload(timeout=30000, wait_until="domcontentloaded")
                wait_site_loaded(page, None, timeout=30)
                _dismiss_all(page)
            except Exception as e:
                print(f"  [step4] reload error: {e}")
            last_reload = time.time()

        _dismiss_all(page)
        sig = page.evaluate(js_state)

        if sig is None:
            if elapsed % 30 == 0:
                rem = RENDER_TIMEOUT - elapsed
                print(f"  [step4] {elapsed//60}m{elapsed%60}s | {rem//60}m{rem%60}s left")
        elif sig.startswith("progress:"):
            pct = sig.split(":", 1)[1]
            if pct != last_pct:
                print(f"  [step4] Rendering... {pct}"); last_pct = pct
        else:
            print(f"  [step4] Render done ({elapsed}s) -> {sig}")
            render_done = True; break

        time.sleep(POLL_INTERVAL)

    if not render_done:
        print("  [step4] Timeout — trying download anyway")

    sleep_log(3, "UI settle")
    _close_preview_popup(page)
    sleep_log(2)
    return _download(page, safe_name)

# ── DOWNLOAD + METADATA ────────────────────────────────────────────────────────
def _download(page, safe_name):
    out = {"video": "", "thumb": "", "gen_title": "", "summary": "", "tags": ""}
    sdir = story_dir(safe_name)

    meta = page.evaluate("""\
() => {
    function byLabel(label) {
        const all = Array.from(document.querySelectorAll('div,span,label,p,h3,h4,h5'));
        for (const el of all) {
            const own = Array.from(el.childNodes)
                .filter(n => n.nodeType === 3).map(n => n.textContent.trim()).join('');
            if (own !== label && (el.innerText || '').trim() !== label) continue;
            if (!el.getBoundingClientRect().width) continue;
            let c = el.parentElement;
            for (let i = 0; i < 5; i++) {
                if (!c) break;
                for (const inp of c.querySelectorAll('input,textarea,[contenteditable="true"]')) {
                    const v = (inp.value || inp.innerText || '').trim();
                    if (v && v.length > 2) return v;
                }
                c = c.parentElement;
            }
        }
        return '';
    }
    function near(lbl) {
        for (const el of Array.from(document.querySelectorAll('*'))) {
            if ((el.innerText || '').trim() === lbl && el.getBoundingClientRect().width > 0) {
                const sib = el.nextElementSibling;
                if (sib && (sib.innerText || '').trim().length > 2) return (sib.innerText || '').trim();
                if (el.parentElement) {
                    const kids = Array.from(el.parentElement.children);
                    const idx = kids.indexOf(el);
                    if (idx >= 0 && kids[idx+1]) return (kids[idx+1].innerText || '').trim();
                }
            }
        }
        return '';
    }
    return {
        title:    byLabel('Title')    || near('Title')    || '',
        summary:  byLabel('Summary')  || near('Summary')  || '',
        hashtags: byLabel('Hashtags') || byLabel('Tags')  || near('Hashtags') || near('Tags') || '',
    };
}""") or {}

    out["gen_title"] = meta.get("title", "")
    out["summary"]   = meta.get("summary", "")
    out["tags"]      = meta.get("hashtags", "")
    print(f"  [meta] Title='{out['gen_title'][:40]}' Summary='{out['summary'][:40]}'")

    cookies = {c["name"]: c["value"] for c in page.context.cookies()}
    headers = {"User-Agent": "Mozilla/5.0", "Referer": page.url}

    # Thumbnail
    thumb_dest = os.path.join(sdir, f"{safe_name}_thumb.jpg")
    thumb_url = page.evaluate("""\
() => {
    const all = Array.from(document.querySelectorAll('div,span,section,h3,h4,p'));
    for (const el of all) {
        const t = (el.innerText || '').trim().toLowerCase();
        if (!t.includes('thumbnail') && !t.includes('magic thumbnail')) continue;
        let c = el;
        for (let i = 0; i < 8; i++) {
            if (!c) break;
            const img = c.querySelector('img[src]');
            if (img && img.src.startsWith('http') && img.naturalWidth >= 100) return img.src;
            c = c.parentElement;
        }
    }
    const imgs = Array.from(document.querySelectorAll('img[src]'))
        .filter(i => i.src.startsWith('http') && !i.src.includes('logo') &&
                     !i.src.includes('icon') && i.naturalWidth >= 200)
        .sort((a, b) => (b.naturalWidth*b.naturalHeight) - (a.naturalWidth*a.naturalHeight));
    return imgs.length ? imgs[0].src : null;
}""")
    if thumb_url:
        try:
            r = requests.get(thumb_url, timeout=30, cookies=cookies, headers=headers)
            if r.status_code == 200 and len(r.content) > 5000:
                with open(thumb_dest, "wb") as f: f.write(r.content)
                out["thumb"] = thumb_dest
                print(f"  [dl] Thumbnail -> {thumb_dest} ({len(r.content)//1024} KB)")
        except Exception as e: print(f"  [dl] Thumbnail error: {e}")

    # Video
    video_dest = os.path.join(sdir, f"{safe_name}.mp4")
    vid_url = page.evaluate("""\
() => {
    const v = document.querySelector('video');
    if (v && v.src && v.src.includes('.mp4')) return v.src;
    const s = document.querySelector('video source');
    if (s && s.src && s.src.includes('.mp4')) return s.src;
    const a = document.querySelector('a[href*=".mp4"]');
    if (a) return a.href;
    return null;
}""")

    if vid_url:
        try:
            print(f"  [dl] Downloading video... {vid_url[:80]}")
            r = requests.get(vid_url, stream=True, timeout=180, cookies=cookies, headers=headers)
            r.raise_for_status()
            total = 0
            with open(video_dest, "wb") as f:
                for chunk in r.iter_content(65536):
                    if chunk:
                        f.write(chunk); total += len(chunk)
                        if total % (1024*1024) < 65536:
                            print(f"    {total//1024} KB...")
            if total > 10000:
                out["video"] = video_dest
                print(f"  [dl] Video -> {video_dest} ({total//1024} KB)")
            else:
                print(f"  [dl] Video too small ({total}B)"); os.remove(video_dest)
        except Exception as e: print(f"  [dl] Video error: {e}")

    if not out["video"]:
        print("  [dl] Trying native download button...")
        _close_preview_popup(page); sleep_log(2)
        for sel in ["button:has-text('Download video')", "button:has-text('Download')",
                    "a:has-text('Download')", "a[download]", "a[href*='.mp4']"]:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    with page.expect_download(timeout=120000) as dl_info:
                        loc.first.click()
                    dl_info.value.save_as(video_dest)
                    out["video"] = video_dest
                    print(f"  [dl] Video (native) -> {video_dest}"); break
            except Exception as e: print(f"  [dl] {sel}: {e}")

    return out

# ── RETRY via User Center ──────────────────────────────────────────────────────
def _retry_from_user_center(page, project_url, safe_name):
    print("  [retry] Opening User Center...")
    sleep_log(5, "pre-retry")
    try:
        page.goto("https://magiclight.ai/user-center/", timeout=60000)
        wait_site_loaded(page, None, timeout=45)
        sleep_log(4, "user-center settle")
        _dismiss_all(page)
    except Exception as e:
        print(f"  [retry] User Center failed: {e}"); return None

    print(f"  [retry] URL: {page.url}")

    clicked = page.evaluate("""\
(targetUrl) => {
    if (targetUrl) {
        const parts = targetUrl.replace(/[/]+$/, '').split('/');
        const projId = parts[parts.length - 1];
        if (projId && projId.length > 5) {
            const match = Array.from(document.querySelectorAll('a[href]'))
                .find(a => a.href && a.href.includes(projId));
            if (match && match.getBoundingClientRect().width > 0) {
                match.click(); return 'matched ID: ' + projId;
            }
        }
    }
    const editLinks = Array.from(document.querySelectorAll(
        'a[href*="/project/edit/"],a[href*="/edit/"]'
    )).filter(a => a.getBoundingClientRect().width > 0);
    if (editLinks.length) { editLinks[0].click(); return 'edit-link'; }

    const selectors = [
        '[class*="project"] a','[class*="video"] a','[class*="work"] a',
        '[class*="story"] a','[class*="card"] a','[class*="item"] a[href]',
    ];
    for (const sel of selectors) {
        const items = Array.from(document.querySelectorAll(sel))
            .filter(el => el.getBoundingClientRect().width > 0);
        if (items.length) { items[0].click(); return 'card-link: ' + sel; }
    }
    const thumbs = Array.from(document.querySelectorAll('a')).filter(a => {
        const r = a.getBoundingClientRect();
        return r.width > 80 && r.height > 50 &&
               (a.querySelector('img') || a.querySelector('video'));
    });
    if (thumbs.length) { thumbs[0].click(); return 'thumb-link'; }
    return null;
}""", project_url or "")

    if not clicked:
        if project_url and '/project/' in project_url:
            print(f"  [retry] Direct goto: {project_url}")
            try:
                page.goto(project_url, timeout=60000)
                wait_site_loaded(page, None, timeout=30)
                sleep_log(3); _dismiss_all(page)
                return _download(page, safe_name)
            except Exception as e:
                print(f"  [retry] Direct goto failed: {e}")
        print("  [retry] Could not find project"); return None

    print(f"  [retry] Opened ({clicked})")
    sleep_log(5, "project load"); wait_site_loaded(page, None, 30); _dismiss_all(page)
    try: return _download(page, safe_name)
    except Exception as e:
        print(f"  [retry] Download failed: {e}"); return None

# ── MAIN ───────────────────────────────────────────────────────────────────────
def _make_safe(row_num, title):
    s = re.sub(r"[^\w\-]", "_", f"row{row_num}_{title[:40]}")
    return s.strip("_")

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--max",      type=int, default=0)
    p.add_argument("--headless", action="store_true")
    return p.parse_args()

def _make_context(browser, account):
    auth_file = _auth_json_path(account)
    ctx_kwargs = {"accept_downloads": True, "no_viewport": True}
    if os.path.exists(auth_file):
        ctx_kwargs["storage_state"] = auth_file
        print(f"[Context] Loaded session {auth_file}")
    return browser.new_context(**ctx_kwargs)

def main():
    global _browser, _current_account_idx
    args = parse_args()

    if not ACCOUNTS:
        print("[ERROR] No accounts. Set EMAIL+PASSWORD or ACCOUNTS in .env"); return

    if not ensure_csv(): return

    rows    = read_csv()
    pending = [(i, r) for i, r in enumerate(rows)
               if r.get("Status", "").strip().lower() == "pending"]

    if not pending:
        print("[Main] No Pending rows."); return

    limit   = args.max if args.max > 0 else len(pending)
    pending = pending[:limit]
    print(f"[Main] Processing {len(pending)} stories | accounts: {len(ACCOUNTS)}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless, args=["--start-maximized"])
        _browser = browser

        account = _get_account()
        context = _make_context(browser, account)
        page    = context.new_page()

        try:
            login(page, account)
        except Exception as e:
            print(f"[FATAL] Login: {e}")
            auth_file = _auth_json_path(account)
            if os.path.exists(auth_file):
                print("[FATAL] Removing stale session and retrying...")
                os.remove(auth_file)
                try:
                    context.close()
                    context = _make_context(browser, account)
                    page    = context.new_page()
                    login(page, account)
                except Exception as e2:
                    print(f"[FATAL] Retry login failed: {e2}")
                    browser.close(); return
            else:
                browser.close(); return

        for csv_idx, row in pending:
            if _shutdown: break

            story = row.get("Story", "").strip()
            if not story:
                print(f"[Skip] Row {csv_idx+2}: empty Story"); continue

            title   = row.get("Title", f"Row{csv_idx+2}").strip() or f"Row{csv_idx+2}"
            row_num = csv_idx + 2
            safe    = _make_safe(row_num, title)

            print(f"\n{'='*60}\n  Row {row_num}: {title}\n  Output: output/{safe}/\n{'='*60}")
            update_row(csv_idx, Status="Processing",
                       Created_Time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

            project_url = ""
            result = None

            try:
                step1(page, story)
                step2(page)
                step3(page)
                project_url = page.url
                update_row(csv_idx, Project_URL=project_url)
                result = step4(page, safe)

                if _credit_exhausted(page):
                    account = next_account()
                    if account:
                        context.close()
                        context = _make_context(browser, account)
                        page    = context.new_page()
                        login(page, account)

            except Exception as e:
                screenshot(page, f"error_row{row_num}")
                debug_buttons(page)
                print(f"  [Main] Row {row_num} error: {e}")

                if _credit_exhausted(page):
                    account = next_account()
                    if account:
                        context.close()
                        context = _make_context(browser, account)
                        page    = context.new_page()
                        login(page, account)
                        try: result = _retry_from_user_center(page, project_url, safe)
                        except: result = None
                    else:
                        update_row(csv_idx, Status="Error",
                                   Notes="Credits exhausted",
                                   Completed_Time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                        break
                else:
                    print("  [Main] Retrying via User Center...")
                    try: result = _retry_from_user_center(page, project_url, safe)
                    except Exception as re_err:
                        print(f"  [retry] {re_err}"); result = None

                if not result:
                    update_row(csv_idx, Status="Error", Notes=str(e)[:300],
                               Completed_Time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    print(f"  [Main] Row {row_num} -> Error")
                    sleep_log(5); continue

            video_ok = bool(result and result.get("video") and os.path.exists(result["video"]))
            status   = "Done" if video_ok else "No_Video"
            update_row(csv_idx,
                Status         = status,
                Gen_Title      = (result or {}).get("gen_title") or title,
                Summary        = (result or {}).get("summary", ""),
                Tags           = (result or {}).get("tags", ""),
                Video_Path     = (result or {}).get("video", ""),
                Thumb_Path     = (result or {}).get("thumb", ""),
                Project_URL    = page.url,
                Notes          = "OK" if video_ok else "Video download failed",
                Completed_Time = datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            print(f"  [Main] Row {row_num} -> {status}")
            if len(pending) > 1:
                sleep_log(5, "cooldown")

        print("\n[Main] All done — closing.")
        try: browser.close()
        except: pass
        _browser = None


if __name__ == "__main__":
    main()
