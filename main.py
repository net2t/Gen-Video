"""
MagicLight Auto — Kids Story Video Generator
=============================================
Version : 2.0.0
Released: 2026-04-03
Repo    : https://github.com/net2t/VideoProcessor

CSV: stories.csv  ->  output/row{N}_{title}/

Usage:
    python main.py              # Process all Pending rows
    python main.py --max 2      # Process max 2 stories
    python main.py --headless   # No browser window

Credentials (.env):
    EMAIL=your@email.com
    PASSWORD=yourpassword

Status values written to CSV:
    Processing  — currently running
    Done        — video downloaded successfully
    No_Video    — render done but video download failed
    Low Credit  — account ran out of credits, stopped
    Error       — unexpected failure

──────────────────────────────────────────────────────────────────────────────
  STABLE FUNCTIONS — do NOT refactor without full regression test
──────────────────────────────────────────────────────────────────────────────
  login()                    — always logs out first, then fresh login
  _dismiss_animation_modal() — uses .arco-modal-mask as real-dialog signal
  step4() / js_header_next   — ONLY clicks header-shiny-action__btn for Next
  _dismiss_all()             — generic banner/popup killer, NOT dialog-aware
──────────────────────────────────────────────────────────────────────────────
"""

__version__ = "2.0.0"

import re
import os
import csv
import sys
import time
import signal
import warnings
import argparse
import requests
from datetime import datetime

# Suppress noisy deprecation / SSL warnings
warnings.filterwarnings("ignore")
os.environ.setdefault("PYTHONWARNINGS", "ignore")

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

# Rich terminal UI
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich import print as rprint

console = Console(highlight=False)

def _log(msg, style="white"):
    console.print(msg, style=style)

def _step(label, style="bold cyan"):
    console.print(f"\n[bold]{label}[/bold]", style=style)

def _ok(msg):
    console.print(f"  [bold green]✓[/bold green] {msg}")

def _warn(msg):
    console.print(f"  [bold yellow]⚠[/bold yellow]  {msg}")

def _err(msg):
    console.print(f"  [bold red]✗[/bold red] {msg}")

def _info(msg):
    console.print(f"  [dim]{msg}[/dim]")

# ── Config ─────────────────────────────────────────────────────────────────────
load_dotenv()

EMAIL    = os.getenv("EMAIL", "")
PASSWORD = os.getenv("PASSWORD", "")

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
    _warn("[STOP] Ctrl+C — cleaning up...")
    _shutdown = True
    if _browser:
        try: _browser.close()
        except: pass
    sys.exit(0)

signal.signal(signal.SIGINT, _sig)

for _d in [OUT_BASE, OUT_SHOTS]:
    os.makedirs(_d, exist_ok=True)

# ── CSV ────────────────────────────────────────────────────────────────────────
def ensure_csv():
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=CSV_FIELDS).writeheader()
        _warn(f"Created {CSV_FILE} — add stories and re-run.")
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
    _info(f"[wait] {secs}s{label}...")
    for _ in range(secs):
        if _shutdown: return
        time.sleep(1)

def _wait_dismissing(page, seconds, reason=""):
    label = f" ({reason})" if reason else ""
    _info(f"[wait] {seconds}s{label} (popup-watch)...")
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
            _info(f"...{seconds - elapsed}s remaining")

# ── Popup helpers ──────────────────────────────────────────────────────────────
def _all_frames(page):
    try: return page.frames
    except: return [page]

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
                 "Maybe later","Not now","Dismiss","Close samples","No","Cancel","Skip"];
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
    try:
        r = page.evaluate(_REAL_DIALOG_JS)
        if r:
            _info(f"[modal] {r}")
            time.sleep(2); return
    except: pass
    try:
        r = page.evaluate(_ANIM_PANEL_JS)
        if r:
            _info(f"[modal] {r}")
            try: page.keyboard.press("Escape")
            except: pass
            time.sleep(1.5); return
    except: pass
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
                _info(f"[modal] closed via '{sel}'")
                time.sleep(2); return
        except: pass
    try: page.keyboard.press("Escape"); time.sleep(0.5)
    except: pass

def _close_preview_popup(page):
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
            _info(f"[click] '{r}'")
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
            _info(f"[click-class] ~'{cls}'")
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
        _info(f"[debug-url] {page.url}")
        for i in (items or []): _info(f"  {i}")
    except: pass

# ── Credit check ───────────────────────────────────────────────────────────────
def _credit_exhausted(page):
    try:
        body = page.evaluate("() => (document.body && document.body.innerText) || ''")
        for kw in ["insufficient credits","not enough credits","credit limit",
                   "out of credits","credits exhausted","quota exceeded",
                   "your credits","credits remaining"]:
            if kw in body.lower():
                # Check if a numeric credit balance is visible and is 0 or very low
                pass
        for kw in ["insufficient credits","not enough credits","out of credits",
                   "credits exhausted","quota exceeded"]:
            if kw in body.lower():
                return True
    except: pass
    return False

# ── LOGIN ──────────────────────────────────────────────────────────────────────
def _logout(page):
    """Attempt to logout any existing session before fresh login."""
    _info("[logout] Clearing session...")
    try:
        page.goto("https://magiclight.ai/", timeout=30000)
        wait_site_loaded(page, None, timeout=20)
        time.sleep(2)
        # Try clicking user avatar / profile menu then logout
        page.evaluate("""\
() => {
    const logoutTexts = ['Log out','Logout','Sign out','Sign Out','Log Out'];
    const els = Array.from(document.querySelectorAll('a,button,div,span'));
    for (const el of els) {
        const t = (el.innerText || '').trim();
        if (logoutTexts.includes(t) && el.getBoundingClientRect().width > 0) {
            el.click(); return t;
        }
    }
    return null;
}""")
        time.sleep(1)
    except: pass
    # Clear cookies/storage regardless
    try: page.context.clear_cookies()
    except: pass

def login(page, account=None):
    if account is None:
        account = {"email": EMAIL, "password": PASSWORD}
    email    = account["email"]
    password = account["password"]

    _step("[Login] Starting fresh login...")
    _logout(page)

    page.goto("https://magiclight.ai/login/?to=%252Fkids-story%252F", timeout=60000)
    try: page.wait_for_load_state("domcontentloaded", timeout=30000)
    except: pass
    sleep_log(4, "page settle")

    _info(f"[Login] URL: {page.url}")

    # Click "Log in with Email" tab/button if present (new UI)
    for sel in [
        'text=Log in with Email',
        'button:has-text("Log in with Email")',
        'div:has-text("Log in with Email")',
        '.entry-email',
        '[class*="entry-email"]',
    ]:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=3000):
                el.click()
                _info(f"[Login] Email tab clicked via '{sel}'")
                sleep_log(2, "inputs settle")
                break
        except: pass

    # Fill email
    email_filled = False
    for sel in ['input[type="text"]', 'input[type="email"]', 'input[name="email"]',
                'input[placeholder*="mail" i]']:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=6000)
            loc.scroll_into_view_if_needed()
            loc.click(); time.sleep(0.3)
            loc.fill(email)
            _info(f"[Login] Email filled via '{sel}'")
            email_filled = True; break
        except: continue

    if not email_filled:
        debug_buttons(page)
        raise Exception(f"Login failed — email input not found for {email}")

    time.sleep(0.4)

    # Fill password
    pass_filled = False
    for sel in ['input[type="password"]', 'input[name="password"]',
                'input[placeholder*="password" i]']:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=6000)
            loc.scroll_into_view_if_needed()
            loc.click(); time.sleep(0.3)
            loc.fill(password)
            _info(f"[Login] Password filled via '{sel}'")
            pass_filled = True; break
        except: continue

    if not pass_filled:
        raise Exception(f"Login failed — password input not found for {email}")

    time.sleep(0.4)

    # Click Continue
    clicked = False
    for attempt in range(3):
        for sel in ["text=Continue", "div.signin-continue",
                    "button:has-text('Continue')", "a:has-text('Continue')"]:
            try:
                el = page.locator(sel).first
                if el.is_visible():
                    el.click(); clicked = True
                    _info(f"[Login] Continue via '{sel}'"); break
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

    _ok(f"[Login] Logged in → {page.url}")
    sleep_log(3, "post-login popups")
    _dismiss_post_login_popups(page)
    return True


def _dismiss_post_login_popups(page):
    _info("[Login] Dismissing post-login popups...")
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
            if n: _info(f"  [popup] round {i+1}: {n} dismissed")
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
    _ok("[Login] Post-login popups cleared")


# ── STEP 1: Story Input ────────────────────────────────────────────────────────
def step1(page, story_text):
    _step("[Step 1] Story input →")
    page.goto("https://magiclight.ai/kids-story/", timeout=60000)
    wait_site_loaded(page, None, timeout=60)
    dismiss_popups(page, timeout=10)

    ta = page.get_by_role("textbox", name="Please enter an original")
    wait_site_loaded(page, ta, timeout=60)
    dismiss_popups(page, timeout=6)
    ta.wait_for(state="visible", timeout=20000)
    ta.click(); ta.fill(story_text)
    _ok("Story text filled")
    sleep_log(1)

    # Style — Pixar 2.0
    try:
        page.locator("div").filter(has_text=re.compile(r"^Pixar 2\.0$")).first.click()
        _ok("Style: Pixar 2.0 selected")
        time.sleep(0.5)
    except: _warn("Pixar 2.0 not found — using default style")

    # Aspect ratio — 16:9
    try:
        page.locator("div").filter(has_text=re.compile(r"^16:9$")).first.click()
        _ok("Aspect: 16:9 selected")
        time.sleep(0.5)
    except: _warn("16:9 not found — using default ratio")

    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    sleep_log(1)
    _select_dropdown(page, "Voiceover", "Sophia")
    _select_dropdown(page, "Background Music", "Silica")
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    sleep_log(1)

    # Next button
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

    _ok("Next → Step 2")
    _wait_dismissing(page, STEP1_WAIT, "AI generating script")


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
            if r2: _ok(f"{label_text} → {option_text}")
            else:
                page.keyboard.press("Escape")
                _warn(f"'{option_text}' not found in {label_text} dropdown")
        else:
            _warn(f"{label_text} dropdown not found")
    except Exception as e:
        _warn(f"Dropdown error: {e}")


# ── STEP 2: Cast ───────────────────────────────────────────────────────────────
def step2(page):
    _step(f"[Step 2] Cast generation ({STEP2_WAIT}s)...")
    dismiss_popups(page, timeout=5)
    _wait_dismissing(page, STEP2_WAIT, "characters generating")
    dismiss_popups(page, timeout=5)

    # Updated: "Next Step" now at bottom-right, try multiple selectors
    clicked = False
    for sel in [
        "div[class*='step2-footer-btn-left']",
        "button:has-text('Next Step')",
        "div[class*='footer']:has-text('Next Step')",
        "div[class*='vlog-btn']:has-text('Next Step')",
    ]:
        try:
            el = page.locator(sel)
            if el.count() > 0 and el.first.is_visible():
                el.first.click(); clicked = True
                _ok(f"Next Step clicked via '{sel}'"); break
        except: pass

    if not clicked:
        clicked = dom_click_text(page, ["Next Step", "Next", "Animate All"], timeout=30)

    sleep_log(4)
    _dismiss_animation_modal(page)
    sleep_log(3)
    _ok("[Step 2] Done")


# ── STEP 3: Storyboard ─────────────────────────────────────────────────────────
def step3(page):
    _step(f"[Step 3] Storyboard (up to {STEP3_WAIT}s)...")
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
        _info(f"  waiting... {int(deadline - time.time())}s left")

    sleep_log(3)
    _set_subtitle_style(page)

    # Updated: Next button moves to top-right header in Step 3
    clicked = False
    for sel in [
        "[class*='header'] button:has-text('Next')",
        "[class*='header-shiny-action__btn']:has-text('Next')",
        "div[class*='step2-footer-btn-left']",
    ]:
        try:
            el = page.locator(sel)
            if el.count() > 0 and el.first.is_visible():
                el.first.click(); clicked = True
                _ok(f"Next clicked via '{sel}'"); break
        except: pass

    if not clicked:
        clicked = dom_click_text(page, ["Next", "Next Step"], timeout=15)

    sleep_log(4)
    _dismiss_animation_modal(page)
    sleep_log(3)
    _ok("[Step 3] Done")


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
    _info(f"[step3] {result}")


# ── STEP 4: Generate + Wait + Download ────────────────────────────────────────
def step4(page, safe_name):
    _step("[Step 4] Navigating to Generate...")
    MAX_NEXT = 12

    js_modal_blocking = """\
() => {
    const masks = Array.from(document.querySelectorAll(
        '.arco-modal-mask,[class*="modal-mask"]'
    )).filter(el => {
        const r = el.getBoundingClientRect();
        return r.width > 200 && r.height > 200;
    });
    if (masks.length) return 'mask';
    return null;
}"""

    js_header_next = """\
() => {
    if (typeof Node === 'undefined') return null;
    for (const el of Array.from(document.querySelectorAll(
        '[class*="header-shiny-action__btn"],[class*="header-left-btn"]'
    ))) {
        const t = (el.innerText || '').trim();
        const r = el.getBoundingClientRect();
        if (t === 'Next' && r.width > 0) { el.click(); return 'header-shiny: Next'; }
    }
    for (const el of Array.from(document.querySelectorAll('button.arco-btn-primary'))) {
        const t = (el.innerText || '').trim();
        const r = el.getBoundingClientRect();
        if (t === 'Next' && r.width > 0) { el.click(); return 'arco-primary: Next'; }
    }
    return null;
}"""

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
        _dismiss_animation_modal(page)
        sleep_log(2)

        found = page.evaluate(js_has_gen)
        if found:
            _ok(f"Generate button found after {attempt} attempts: '{found}'")
            break

        blocking = page.evaluate(js_modal_blocking)
        if blocking:
            _warn(f"Modal blocking ({blocking}) — re-dismissing")
            _dismiss_animation_modal(page)
            sleep_log(3)
            continue

        r = page.evaluate(js_header_next)
        _info(f"[step4] attempt {attempt+1}: {r or 'no header Next'}")
        if not r:
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
    _info(f"[Step 4] Waiting for render (max {RENDER_TIMEOUT//60} min)...")
    start = time.time(); last_reload = start; render_done = False

    js_state = r"""
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
                _info(f"[step4] Reloading... ({elapsed//60}m elapsed)")
                page.reload(timeout=30000, wait_until="domcontentloaded")
                wait_site_loaded(page, None, timeout=30)
                _dismiss_all(page)
            except Exception as e:
                _warn(f"Reload error: {e}")
            last_reload = time.time()

        _dismiss_all(page)
        sig = page.evaluate(js_state)

        if sig is None:
            if elapsed % 30 == 0:
                rem = RENDER_TIMEOUT - elapsed
                _info(f"[step4] {elapsed//60}m{elapsed%60}s elapsed | {rem//60}m{rem%60}s left")
        elif sig.startswith("progress:"):
            pct = sig.split(":", 1)[1]
            if pct != last_pct:
                console.print(f"  [cyan]⟳[/cyan] Rendering... [bold]{pct}[/bold]")
                last_pct = pct
        else:
            _ok(f"Render done ({elapsed}s) → {sig}")
            render_done = True; break

        time.sleep(POLL_INTERVAL)

    if not render_done:
        _warn("Render timeout — attempting download anyway")

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
    _info(f"[meta] Title='{out['gen_title'][:40]}'  Summary='{out['summary'][:40]}'")

    cookies = {c["name"]: c["value"] for c in page.context.cookies()}
    headers = {"User-Agent": "Mozilla/5.0", "Referer": page.url}

    # ── Thumbnail ──────────────────────────────────────────────────────────────
    thumb_dest = os.path.join(sdir, f"{safe_name}_thumb.jpg")
    thumb_url = page.evaluate("""\
() => {
    // Priority 1: element near "thumbnail" / "magic thumbnail" label
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
    // Priority 2: largest non-icon/logo image on page
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
                _ok(f"Thumbnail → {thumb_dest} ({len(r.content)//1024} KB)")
        except Exception as e: _warn(f"Thumbnail error: {e}")

    # ── Thumbnail fallback: first timeline/storyboard image ───────────────────
    if not out["thumb"]:
        fallback_url = page.evaluate("""\
() => {
    const selectors = [
        '[class*="timeline"] img[src]',
        '[class*="storyboard"] img[src]',
        '[class*="scene"] img[src]',
        '[class*="story-board"] img[src]',
        '[class*="frame"] img[src]',
        'img[src*="oss"][src]',
    ];
    for (const sel of selectors) {
        const imgs = Array.from(document.querySelectorAll(sel))
            .filter(i => i.src.startsWith('http') && i.naturalWidth >= 50);
        if (imgs.length) return imgs[0].src;
    }
    return null;
}""")
        if fallback_url:
            try:
                r = requests.get(fallback_url, timeout=30, cookies=cookies, headers=headers)
                if r.status_code == 200 and len(r.content) > 1000:
                    with open(thumb_dest, "wb") as f: f.write(r.content)
                    out["thumb"] = thumb_dest
                    _ok(f"Thumbnail (fallback image) → {thumb_dest} ({len(r.content)//1024} KB)")
            except Exception as e: _warn(f"Thumbnail fallback error: {e}")

    # ── Video ──────────────────────────────────────────────────────────────────
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
            _info(f"[dl] Downloading video... {vid_url[:80]}")
            r = requests.get(vid_url, stream=True, timeout=180, cookies=cookies, headers=headers)
            r.raise_for_status()
            total = 0
            with open(video_dest, "wb") as f:
                for chunk in r.iter_content(65536):
                    if chunk:
                        f.write(chunk); total += len(chunk)
                        if total % (1024*1024) < 65536:
                            _info(f"  {total//1024} KB...")
            if total > 10000:
                out["video"] = video_dest
                _ok(f"Video → {video_dest} ({total//1024} KB)")
            else:
                _warn(f"Video too small ({total}B)"); os.remove(video_dest)
        except Exception as e: _warn(f"Video download error: {e}")

    if not out["video"]:
        _info("[dl] Trying native download button...")
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
                    _ok(f"Video (native download) → {video_dest}"); break
            except Exception as e: _warn(f"{sel}: {e}")

    return out


# ── RETRY via User Center ──────────────────────────────────────────────────────
def _retry_from_user_center(page, project_url, safe_name):
    _info("[retry] Opening User Center...")
    sleep_log(5, "pre-retry")
    try:
        page.goto("https://magiclight.ai/user-center/", timeout=60000)
        wait_site_loaded(page, None, timeout=45)
        sleep_log(4, "user-center settle")
        _dismiss_all(page)
    except Exception as e:
        _warn(f"User Center failed: {e}"); return None

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
            _info(f"[retry] Direct goto: {project_url}")
            try:
                page.goto(project_url, timeout=60000)
                wait_site_loaded(page, None, timeout=30)
                sleep_log(3); _dismiss_all(page)
                return _download(page, safe_name)
            except Exception as e:
                _warn(f"Direct goto failed: {e}")
        _warn("[retry] Could not find project"); return None

    _ok(f"[retry] Project opened ({clicked})")
    sleep_log(5, "project load"); wait_site_loaded(page, None, 30); _dismiss_all(page)
    try: return _download(page, safe_name)
    except Exception as e:
        _warn(f"[retry] Download failed: {e}"); return None


# ── MAIN ───────────────────────────────────────────────────────────────────────
def _make_safe(row_num, title):
    s = re.sub(r"[^\w\-]", "_", f"row{row_num}_{title[:40]}")
    return s.strip("_")

def parse_args():
    p = argparse.ArgumentParser(description="MagicLight Auto — Kids Story Generator v2")
    p.add_argument("--max",      type=int, default=0,  help="Max stories to process (0=all)")
    p.add_argument("--headless", action="store_true",   help="Run browser headless")
    return p.parse_args()

def main():
    global _browser
    args = parse_args()

    console.print(Panel.fit(
        f"[bold cyan]MagicLight Auto[/bold cyan]  [dim]v{__version__}[/dim]\n"
        f"[dim]Kids Story Video Generator[/dim]",
        border_style="cyan"
    ))

    if not EMAIL or not PASSWORD:
        _err("No credentials. Set EMAIL + PASSWORD in .env"); return

    if not ensure_csv(): return

    rows    = read_csv()
    pending = [(i, r) for i, r in enumerate(rows)
               if r.get("Status", "").strip().lower() == "pending"]

    if not pending:
        _warn("No Pending rows in stories.csv."); return

    limit   = args.max if args.max > 0 else len(pending)
    pending = pending[:limit]
    _ok(f"Processing [bold]{len(pending)}[/bold] stor{'y' if len(pending)==1 else 'ies'}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless, args=["--start-maximized"])
        _browser = browser
        context = browser.new_context(accept_downloads=True, no_viewport=True)
        page    = context.new_page()

        # Always do a fresh login (logout first)
        try:
            login(page)
        except Exception as e:
            _err(f"[FATAL] Login failed: {e}")
            browser.close(); return

        for csv_idx, row in pending:
            if _shutdown: break

            story = row.get("Story", "").strip()
            if not story:
                _warn(f"Row {csv_idx+2}: empty Story — skipping"); continue

            title   = row.get("Title", f"Row{csv_idx+2}").strip() or f"Row{csv_idx+2}"
            row_num = csv_idx + 2
            safe    = _make_safe(row_num, title)

            console.print(Rule(style="cyan"))
            console.print(Panel(
                f"[bold]Row {row_num}:[/bold] {title}\n[dim]Output → output/{safe}/[/dim]",
                border_style="cyan", expand=False
            ))

            update_row(csv_idx, Status="Processing",
                       Created_Time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

            project_url = ""
            result = None

            try:
                step1(page, story)

                if _credit_exhausted(page):
                    _err("[Low Credit] Insufficient credits — stopping")
                    update_row(csv_idx, Status="Low Credit",
                               Notes="Credits exhausted before Step 2",
                               Completed_Time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    break

                step2(page)
                step3(page)
                project_url = page.url
                update_row(csv_idx, Project_URL=project_url)

                if _credit_exhausted(page):
                    _err("[Low Credit] Insufficient credits — stopping")
                    update_row(csv_idx, Status="Low Credit",
                               Notes="Credits exhausted before Step 4",
                               Completed_Time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    break

                result = step4(page, safe)

                if _credit_exhausted(page):
                    _err("[Low Credit] Insufficient credits detected post-render")
                    update_row(csv_idx, Status="Low Credit",
                               Notes="Credits exhausted",
                               Completed_Time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    break

            except Exception as e:
                screenshot(page, f"error_row{row_num}")
                debug_buttons(page)
                _err(f"Row {row_num} error: {e}")

                if _credit_exhausted(page):
                    _err("[Low Credit] Insufficient credits — stopping all processing")
                    update_row(csv_idx, Status="Low Credit",
                               Notes="Credits exhausted",
                               Completed_Time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    break

                _info("[retry] Attempting via User Center...")
                try: result = _retry_from_user_center(page, project_url, safe)
                except Exception as re_err:
                    _warn(f"[retry] {re_err}"); result = None

                if not result:
                    update_row(csv_idx, Status="Error", Notes=str(e)[:300],
                               Completed_Time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    _err(f"Row {row_num} → Error")
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
            if video_ok:
                _ok(f"[bold green]Row {row_num} → Done ✓[/bold green]")
            else:
                _warn(f"Row {row_num} → No_Video (render done, download failed)")

            if len(pending) > 1:
                sleep_log(5, "cooldown between stories")

        console.print(Rule(style="cyan"))
        _ok("[bold]All done — closing browser.[/bold]")
        try: browser.close()
        except: pass
        _browser = None

if __name__ == "__main__":
    main()

