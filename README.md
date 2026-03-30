# 🎬 VideoProcessor

Automatic post-processor for **Bright Little Stories** videos.

Works **two ways** — same script, same settings:

| Mode | How to run | When to use |
|------|-----------|-------------|
| ☁️ **Cloud** | GitHub Actions (automatic) | No PC needed — runs on schedule |
| 💻 **Local** | `python process.py` on your PC | Manual runs, testing |

---

## What It Does to Each Video

1. **Logo overlay** — places `logo.png` on the top-left corner of the video to cover the MagicLight.AI watermark
2. **Trim end** — cuts the last N seconds (default: 4) to remove the MagicLight outro
3. **Upload to Drive** — uploads the processed video to your Google Drive folder (mirrors the same subfolder structure)
4. **Update Sheet** — sets Status → `Processed` and writes the Drive URL to column O

---

## Project Structure

```
VideoProcessor/
├── process.py                   ← Main script (cloud + local)
├── logo.png                     ← Your sticker that covers the watermark
├── .env                         ← Your config (never commit this)
├── .env.example                 ← Template — copy to .env
├── credentials.json             ← Service Account key (never commit this)
├── requirements.txt             ← Python packages
├── .gitignore
├── logs/                        ← Auto-created — one log file per run
│   └── process_20260328_130000.log
├── downloads/                   ← Default local scan folder (auto-created)
└── .github/
    └── workflows/
        └── process.yml          ← GitHub Actions trigger
```

---

## Google Sheet — Required Column O

Add a header `Processed Video URL` to **column O** of your sheet.

| Col | Header |
|-----|--------|
| A | Theme |
| B | Title |
| ... | ... |
| G | Status |
| H | Magic Thumbnail |
| ... | ... |
| N | Project URL |
| **O** | **Processed Video URL** ← add this |

---

## Setup — Step by Step

### Step 1 — Clone or Create the Repo

```bash
# Option A: Clone this repo
git clone https://github.com/net2t/VideoProcessor.git
cd VideoProcessor

# Option B: Create new repo on GitHub, then clone it
```

### Step 2 — Install Python dependencies

```bash
pip install -r requirements.txt
```

### Step 3 — Install FFmpeg (local PC only)

FFmpeg is **pre-installed** on GitHub Actions runners automatically.

For your local PC (Windows):
```bash
# Option 1 — easiest via pip
pip install imageio-ffmpeg

# Option 2 — install system-wide
# Download from https://ffmpeg.org/download.html
# Extract to C:\ffmpeg\
# Add C:\ffmpeg\bin to your Windows PATH
```

### Step 4 — Create a Service Account (Google Cloud)

A Service Account lets the script access Drive and Sheets without a browser login.

1. Go to https://console.cloud.google.com/
2. Select your project (same as AutoMagicAI)
3. **APIs & Services → Library** — enable:
   - ✅ Google Drive API
   - ✅ Google Sheets API
4. **APIs & Services → Credentials → + Create Credentials → Service Account**
   - Name: `video-processor`
   - Role: `Editor`
   - Click Done
5. Click the service account → **Keys tab → Add Key → JSON**
6. Save the downloaded file as `credentials.json` in this project folder

### Step 5 — Share Sheet and Drive with the Service Account

Open `credentials.json` and find `"client_email"` — copy that email address.

- Open your **Google Sheet** → Share → paste email → Editor → Send
- Open your **Drive folder** → Right-click → Share → paste email → Editor → Done

### Step 6 — Configure .env

```bash
# Copy the template
copy .env.example .env     # Windows
cp .env.example .env       # Mac/Linux

# Edit .env and fill in your values
```

Minimum required settings:
```ini
SPREADSHEET_ID=your_sheet_id_here
GOOGLE_DRIVE_FOLDER_ID=your_folder_id_here
TRIM_SECONDS=4
LOGO_PATH=logo.png
LOGO_X=10
LOGO_Y=10
LOGO_WIDTH=120
```

### Step 7 — Add logo.png

Place your `logo.png` sticker file in the same folder as `process.py`.

This is the image that will be overlaid on the top-left corner to cover the MagicLight watermark. Make it approximately the same size as the watermark.

### Step 8 — Add column O to your Sheet

Open your Google Sheet → click cell O1 → type `Processed Video URL`

---

## Running Locally (PC)

### Cloud mode (reads from Sheet + Drive)
```bash
python process.py --mode cloud
```

### Local mode (scans downloads/ folder)
```bash
python process.py --mode local
```

### Auto-detect mode (default — no --mode needed)
```bash
# If SPREADSHEET_ID is set in .env → runs cloud mode
# If not → runs local mode
python process.py
```

### Dry run (preview only — no changes)
```bash
python process.py --dry-run
```

### Limit number of videos
```bash
python process.py --max 3
```

### Local mode — custom folder
Set `INPUT_FOLDER` in your `.env`:
```ini
INPUT_FOLDER=C:\Users\NADEEM\Downloads
```
Or put videos in the `downloads/` folder next to `process.py` — it auto-scans there.

---

## Setting Up GitHub Actions (Cloud)

### Step 1 — Add GitHub Secrets

Go to: **GitHub → Your Repo → Settings → Secrets and variables → Actions**

Add these 3 secrets:

| Secret Name | Value |
|-------------|-------|
| `SPREADSHEET_ID` | Your Google Sheet ID (from Sheet URL) |
| `GOOGLE_CREDENTIALS` | Full contents of `credentials.json` (open in Notepad, select all, copy) |
| `GOOGLE_DRIVE_FOLDER_ID` | Your Drive folder ID (from folder URL) |

### Step 2 — Push your files to GitHub

Make sure these files are in your repo:
- `process.py`
- `logo.png`
- `requirements.txt`
- `.github/workflows/process.yml`

**Do NOT push:** `.env`, `credentials.json` (they are in .gitignore)

### Step 3 — Test with dry run

1. Go to: **GitHub → Actions tab → 🎬 Process Videos**
2. Click **Run workflow**
3. Set **Dry run** = `true`
4. Click green **Run workflow** button
5. Watch the logs — you should see rows listed

### Step 4 — Run for real

Same steps but set **Dry run** = `false`

### Schedule

Default: every day at **1:00 PM Pakistan time** (08:00 UTC).

To change, edit `.github/workflows/process.yml`:
```yaml
- cron: "0 8 * * *"   # 1:00 PM Pakistan
- cron: "0 3 * * *"   # 8:00 AM Pakistan
- cron: "0 15 * * *"  # 8:00 PM Pakistan
```

Pakistan = UTC + 5. So for 2 PM Pakistan → 9:00 UTC → `"0 9 * * *"`

---

## Status Flow

```
AutoMagicAI (your PC)          VideoProcessor (GitHub Actions)
─────────────────────          ──────────────────────────────
Generated                      
    ↓
  Pending  (project URL saved)
    ↓
  Done     (video on Drive)  ──→  Processed  (logo + trimmed)
```

---

## Log Files

Every run creates a log file in `logs/`:
```
logs/
├── process_20260328_130000.log
├── process_20260329_080012.log
└── process_20260330_080005.log
```

Log files contain timestamps, progress, and any errors. Check them if something goes wrong.

---

## Troubleshooting

**`No credentials found`**
→ `credentials.json` is missing. Follow Step 4 above.

**`Permission denied` on Drive**
→ You forgot to share the Drive folder with the service account email. Follow Step 5.

**`Logo NOT found`**
→ `logo.png` is missing from the project folder. Add it.

**`No unprocessed videos found`**
→ In local mode: put videos in the `downloads/` folder.
   In cloud mode: AutoMagicAI has not set any rows to `Done` yet.

**Video has no logo on it**
→ Logo was not found during processing. Check `LOGO_PATH` in `.env`.

**Video ends too early or too late**
→ Adjust `TRIM_SECONDS` in `.env` (default: 4).

---

## Author

**Nadeem** · github.com/net2t · Bright Little Stories
