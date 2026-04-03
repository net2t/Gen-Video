# MagicLight Auto — Kids Story Video Generator

> Automated, unattended video generation for [MagicLight.ai](https://magiclight.ai/kids-story/) using Playwright.  
> **Version 2.0.0** — April 2026

---

## What It Does

Reads stories from `stories.csv`, submits each one to MagicLight, waits for the video to render, then saves:

| File | Description |
|---|---|
| `output/row{N}_{title}/{title}.mp4` | Final rendered video |
| `output/row{N}_{title}/{title}_thumb.jpg` | Thumbnail image (or first storyboard image as fallback) |
| `stories.csv` | Updated with Status, Gen_Title, Summary, Tags, paths |

---

## Quick Start

### 1. Install dependencies
```bash
pip install playwright python-dotenv requests rich
playwright install chromium
```

### 2. Configure credentials
Copy `.env.example` to `.env` and fill in:
```env
EMAIL=your@email.com
PASSWORD=yourpassword
```

### 3. Set up your stories
Edit `stories.csv` — add one row per story:

| Column | Required | Description |
|---|---|---|
| Status | ✅ | Set to `Pending` to process |
| Title | ✅ | Short label for folder naming |
| Story | ✅ | Full story text to submit |
| Theme | ❌ | Optional tag for your reference |

### 4. Run
```bash
# Process all Pending rows
python main.py

# Process only 1 story (test run)
python main.py --max 1

# Run without visible browser
python main.py --headless --max 1
```

---

## Status Values

| Status | Meaning |
|---|---|
| `Pending` | Waiting to be processed |
| `Processing` | Currently running |
| `Done` | Video downloaded successfully |
| `No_Video` | Render done but video download failed |
| `Low Credit` | Account ran out of credits — processing stopped |
| `Error` | Unexpected failure |

> **Low Credit**: When the account has insufficient credits, the script stops immediately and marks the current row as `Low Credit`. Add credits to your MagicLight account and re-run.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `EMAIL` | — | Login email |
| `PASSWORD` | — | Login password |
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

## Login Behavior

Each run performs a **fresh login** (clears any existing session first). No saved sessions are reused. This ensures the automation always starts from a clean state.

---

## Retry / Error Handling

If a story fails, the script:
1. Takes a screenshot → `output/screenshots/`
2. Navigates to User Center and finds the project by URL/ID
3. If project found → retries download
4. Marks row as `Error` in CSV and moves to next story

---

## Security

- **Never commit** `.env` — it contains credentials
- Already in `.gitignore`

---

## Project Structure
```
main.py                ← Main automation script
stories.csv            ← Input/output data
.env                   ← Credentials (gitignored)
.env.example           ← Template (safe to commit)
CHANGELOG.md           ← Version history
README.md              ← This file
```
