# MagicLight Auto — Kids Story Video Generator

> Automated, unattended video generation for [MagicLight.ai](https://magiclight.ai/kids-story/) using Playwright.  
> **Version 1.0.0** — April 2026

---

## What It Does

Reads a list of stories from `stories.csv`, submits each one to MagicLight, waits for the video to render, then saves:

| File | Description |
|---|---|
| `output/row{N}_{title}/{title}.mp4` | Final rendered video |
| `output/row{N}_{title}/{title}_thumb.jpg` | Thumbnail image |
| `stories.csv` | Updated with status, title, summary, hashtags |

---

## Quick Start

### 1. Install dependencies
```bash
pip install playwright python-dotenv requests
playwright install chromium
```

### 2. Configure credentials
Copy `.env.example` to `.env` and fill in:
```env
# Single account
EMAIL=your@email.com
PASSWORD=yourpassword

# OR multiple accounts (rotated when credits run out)
ACCOUNTS=user1@email.com:pass1,user2@email.com:pass2
```

### 3. Set up your stories
Edit `stories.csv` — add one row per story:

| Column | Required | Description |
|---|---|---|
| Status | ✅ | Set to `Pending` to process, `Done`/`Error` are set by script |
| Title | ✅ | Short label for folder naming |
| Story | ✅ | Full story text to submit |
| Theme | ❌ | Optional tag for your reference |

### 4. Run
```bash
# Process all Pending rows
python magiclight_auto.py

# Process only 1 story (test run)
python magiclight_auto.py --max 1

# Run without visible browser (headless)
python magiclight_auto.py --headless --max 1
```

> ⚠️ **Headless note:** `--headless` works but headed mode is more reliable for sites with heavy animations. Use headless for server/background runs.

---

## Multi-Account Support

Set `ACCOUNTS` in `.env` with comma-separated `email:password` pairs:
```
ACCOUNTS=acc1@x.com:pass1,acc2@x.com:pass2,acc3@x.com:pass3
```
- Each account gets its own session file (`auth_*.json`)
- Script auto-detects credit exhaustion messages and rotates to the next account
- ~25 stories per account (60 credits each, 1500 credits/account with card linked)

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `EMAIL` | — | Login email (fallback if ACCOUNTS not set) |
| `PASSWORD` | — | Login password |
| `ACCOUNTS` | — | Multi-account list |
| `STEP1_WAIT` | `60` | Seconds to wait after story submission |
| `STEP2_WAIT` | `30` | Seconds to wait for character generation |
| `STEP3_WAIT` | `180` | Max seconds to wait for storyboard images |
| `STEP4_RENDER_TIMEOUT` | `1200` | Max seconds to wait for video render (20 min) |

---

## Output Structure
```
output/
  row4_Rina_and_the_Sticker_Trade/
    row4_Rina_and_the_Sticker_Trade.mp4
    row4_Rina_and_the_Sticker_Trade_thumb.jpg
  row5_Timo_and_the_Magic_Teacup/
    ...
screenshots/          ← Error screenshots for debugging
```

---

## Retry / Error Handling

If a story fails, the script:
1. Takes a screenshot → `output/screenshots/`
2. Navigates to User Center and finds the project by URL/ID
3. If project found → retries download
4. Marks row as `Error` in CSV and moves to next story

---

## Security

- **Never commit** `.env` or `auth_*.json` — they contain credentials and session tokens
- These are already in `.gitignore`
- Session files are per-account: `auth_user_email_com.json`

---

## Project Structure
```
magiclight_auto.py     ← Main automation script (DO NOT modify core functions)
stories.csv            ← Input/output data
.env                   ← Credentials (gitignored)
.env.example           ← Template (safe to commit)
CHANGELOG.md           ← Version history
README.md              ← This file
```
