# MagicLight Auto 🪄 — Kids Story Video Generator

**Version 3.0.0 (FINAL STABLE)**

A robust, fully automated Python Selenium/Playwright script that generates continuous Kids Story videos via MagicLight.ai. It manages authentication, reads story data dynamically from a Google Sheet, handles UI post-render procedures (including complex popup dismissal and animation processing), downloads the final video/thumbnail pair natively, and optionally uploads the results straight directly to Google Drive.

![Console Interface Screenshot](output/screens/console_interface.png) *(Preview placeholder)*

## 🌟 Features
- **Google Sheets Integration**: Pulls "Pending" stories directly from your sheet and synchronizes the Status back in real time.
- **Dynamic Story Mapping**: Combines Google Sheet columns C (Title), D (Story), and E (Moral) automatically.
- **Google Drive Sync**: Prompts explicitly on startup (or via `.env`) to upload the final `.mp4` and `.jpg` thumbnail straight into a shared Google Drive folder, injecting a `Drive_Link` URL back into your sheet.
- **Post-Render Backup**: Handles heavy animation delays gracefully (waiting up to 10 minutes). If video generation gets stuck, automatically navigates to "User Center", opens the project forcefully, and triggers native Chromium download. 
- **Graceful Stop**: Interactive console (powered by `rich`) and native Windows `Ctrl+C` handling that safely shuts down the Playwright browser.

---

## 🛠️ Setup Guide

### 1. Requirements
- Python 3.10+
- Google Chrome installed on your machine
- Required libraries (install them using `pip`):
  ```bash
  pip install playwright gspread google-auth google-api-python-client rich python-dotenv requests
  playwright install chromium
  ```

### 2. Environment Variables (`.env`)
Create a `.env` file in the project's root folder based on the `.env.example`:

```env
EMAIL=yourmagiclight@email.com
PASSWORD=yourmagiclightpassword

SHEET_ID=1MPfnJ2UajI-eKKqGS4y6eb3BEgXpJiZ44nr556cfXRE
SHEET_NAME=Database
CREDS_JSON=credentials.json

# (Optional) Google Drive Folder ID to upload the video + thumbnail
DRIVE_FOLDER_ID=

STEP1_WAIT=60
STEP2_WAIT=30
STEP3_WAIT=180
STEP4_RENDER_TIMEOUT=1200
```

### 3. Google Services Authentication
1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and enable the **Google Drive API** and **Google Sheets API**.
2. Create a "Service Account" and download the private key as a JSON file. Rename it to `credentials.json` and place it in the same root folder as `main.py`.
3. Give that specific Service Account email address **Editor** permissions to your Google Sheet.
4. *For Drive Upload*: Give that specific Service Account email address **Editor** permissions to your Google Drive folder where you want to upload videos.

### 4. Google Sheets Layout
Your Google sheet (default name: "Database") **MUST** exactly contain these headers:
- `Status` (Set new rows to "Pending", will change to "Processing", "Done", "No_Video", "Low Credit", or "Error")
- `Title` (Col C - 3rd column)
- `Story` (Col D - 4th column)
- `Moral` (Col E - 5th column)
- `Gen Title` -> Col F output
- `Gen Summary` -> Col G output
- `Gen Tags` -> Col H output
- `Video_Path`, `Thumb_Path`, `Drive_Link`, `Project_URL` -> Output path updates.

---

## 🚀 Usage

Run the file using Python:
```bash
python main.py
```
This triggers an **Interactive Menu**:
1. You will be asked how many "Pending" stories to process (Enter `0` to process all pending rows infinitely).
2. If `DRIVE_FOLDER_ID` inside `.env` is empty, you'll be asked if you want to paste a Drive Link ID for this session.

### Direct Execution
To bypass the interactive prompt for automated CI/CRON logic:
```bash
python main.py --max 5
```

If you wish to run without the popup UI, add `--headless`:
```bash
python main.py --max 5 --headless
```

---

## 📝 Changelog (v3.0.0)
- **[Feature] Interactive Menu:** Startup flow now prompts for the generation limit and Drive upload intent.
- **[Feature] Google Drive Support:** Uses standard `google-api-python-client` with your existing `credentials.json` to push out MP4s to standard drives.
- **[Improvement] KeyboardInterrupt Control:** Safely wraps the execution logic to gracefully power off Chromium processes when hitting CTRL+C.
- **[Bugfix] Long Scene Generation Loop:** Increased background render timeout verification `MAX_NEXT` to 10 minutes to resolve slow scene generation timeout bugs.
- **[Improvement] Sheet Output Targeting:** Rewrote the metadata output to hit explicitly named arrays `Gen Title`, `Gen Summary`, `Gen Tags`.
- **[Cleanup] Git Structure:** Wiped deprecated CSV logics (`stories.csv`) and rotated `magiclight_auto.py` fully into `main.py`.

---
*Created strictly for automated MagicLight pipelines.*
