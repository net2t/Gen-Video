"""
╔══════════════════════════════════════════════════════════════════╗
║   VideoProcessor — Cloud + Local Video Processor                ║
║                                                                  ║
║   Works TWO ways:                                               ║
║     1. GitHub Actions  — reads Google Sheet, downloads from     ║
║                          Drive, processes, re-uploads           ║
║     2. Local PC        — scans INPUT_FOLDER (or downloads/),    ║
║                          processes videos found there,          ║
║                          uploads result to Drive                ║
║                                                                  ║
║   What it does to each video:                                   ║
║     • Overlay logo.png on top-left  (hides watermark)          ║
║     • Trim last N seconds from end  (removes outro junk)        ║
║     • Upload processed video to Google Drive                    ║
║     • Update Google Sheet status                                ║
║                                                                  ║
║   Repo : github.com/net2t/VideoProcessor                        ║
║   By   : Nadeem (net2t)                                         ║
╚══════════════════════════════════════════════════════════════════╝
"""

# ── Standard library ──────────────────────────────────────────────────────────
import os
import re
import sys
import json
import time
import shutil
import logging
import argparse
import tempfile
import subprocess
from datetime import datetime
from pathlib import Path

# ── Third-party ───────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass   # dotenv optional — env vars can be set directly

try:
    import gspread
    from google.oauth2.service_account import Credentials as SACredentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
    import io as _io
    _GOOGLE_OK = True
except ImportError:
    _GOOGLE_OK = False


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG  — all values come from .env or environment variables
# ══════════════════════════════════════════════════════════════════════════════
SPREADSHEET_ID  = os.getenv("SPREADSHEET_ID",         "")
DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")
TRIM_SECONDS    = int(os.getenv("TRIM_SECONDS",        "4"))
LOGO_PATH       = os.getenv("LOGO_PATH",               "logo.png")
LOGO_X          = int(os.getenv("LOGO_X",              "10"))
LOGO_Y          = int(os.getenv("LOGO_Y",              "10"))
LOGO_WIDTH      = int(os.getenv("LOGO_WIDTH",          "120"))
LOGO_OPACITY    = float(os.getenv("LOGO_OPACITY",      "1.0"))

# ── Local mode config ─────────────────────────────────────────────────────────
# INPUT_FOLDER  : where to scan for videos on local PC
#                 Leave blank → auto-scans downloads/ subfolder in project
# OUTPUT_FOLDER : where processed videos are saved locally before upload
#                 Leave blank → saves next to input file as *_processed.mp4
INPUT_FOLDER    = os.getenv("INPUT_FOLDER",  "")
OUTPUT_FOLDER   = os.getenv("OUTPUT_FOLDER", "")

# ── Supported video extensions ────────────────────────────────────────────────
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".flv", ".wmv"}

# ── Google API scopes ─────────────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ── Sheet column indices (1-based) ────────────────────────────────────────────
COL_TITLE       = 2    # B
COL_STATUS      = 7    # G
COL_THUMB_URL   = 8    # H  — Drive URL of raw video (set by AutoMagicAI)
COL_NOTES       = 13   # M
COL_PROJECT_URL = 14   # N
COL_PROCESSED   = 15   # O  — Drive URL of processed video  (set by us)


# ══════════════════════════════════════════════════════════════════════════════
#  LOGGING  — writes to console AND to logs/process_YYYYMMDD.log
# ══════════════════════════════════════════════════════════════════════════════
def setup_logging() -> logging.Logger:
    """
    Set up logging to both console and a daily log file.
    Log files live in:  logs/process_YYYYMMDD_HHMMSS.log
    """
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)

    timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file   = logs_dir / f"process_{timestamp}.log"

    fmt = "%(asctime)s  %(levelname)-8s  %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt=datefmt,
        handlers=[
            logging.StreamHandler(sys.stdout),           # console
            logging.FileHandler(log_file, encoding="utf-8"),  # file
        ]
    )
    logger = logging.getLogger("VideoProcessor")
    logger.info(f"Log file: {log_file.resolve()}")
    return logger


log = setup_logging()


# ══════════════════════════════════════════════════════════════════════════════
#  GOOGLE AUTH
#  Priority:
#    1. GOOGLE_CREDENTIALS env var  (GitHub Actions — full JSON string)
#    2. credentials.json file       (local PC)
# ══════════════════════════════════════════════════════════════════════════════
def get_credentials() -> "SACredentials":
    if not _GOOGLE_OK:
        log.error("Google libraries not installed. Run: pip install -r requirements.txt")
        sys.exit(1)

    creds_str = os.getenv("GOOGLE_CREDENTIALS", "")
    if creds_str:
        try:
            info = json.loads(creds_str)
            log.info("Auth: using GOOGLE_CREDENTIALS env var (GitHub Actions)")
            return SACredentials.from_service_account_info(info, scopes=SCOPES)
        except json.JSONDecodeError as e:
            log.error(f"Auth: failed to parse GOOGLE_CREDENTIALS JSON — {e}")
            sys.exit(1)

    creds_file = Path("credentials.json")
    if creds_file.exists():
        log.info("Auth: using credentials.json (local PC)")
        return SACredentials.from_service_account_file(str(creds_file), scopes=SCOPES)

    log.error(
        "No credentials found!\n"
        "  Local PC     → place credentials.json in project folder\n"
        "  GitHub Actions → add GOOGLE_CREDENTIALS secret"
    )
    sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════════
#  GOOGLE SHEET
# ══════════════════════════════════════════════════════════════════════════════
def get_sheet(creds):
    client = gspread.authorize(creds)
    sheet  = client.open_by_key(SPREADSHEET_ID).sheet1
    log.info(f"Sheet: connected → {SPREADSHEET_ID}")
    return sheet


def get_sheet_rows(sheet) -> list[dict]:
    """
    Return rows where Status = 'Done' and column O (Processed) is empty.
    These are videos that AutoMagicAI generated but we haven't processed yet.
    """
    all_data = sheet.get_all_values()
    if len(all_data) < 2:
        log.warning("Sheet is empty or has no data rows.")
        return []

    rows = []
    for row_idx, row in enumerate(all_data[1:], start=2):
        while len(row) < COL_PROCESSED:
            row.append("")

        status    = row[COL_STATUS    - 1].strip().lower()
        processed = row[COL_PROCESSED - 1].strip()
        drive_url = row[COL_THUMB_URL - 1].strip()
        title     = row[COL_TITLE     - 1].strip() or f"Row_{row_idx}"

        if status != "done":
            continue
        if processed:
            log.info(f"Row {row_idx} '{title}' — already processed, skipping.")
            continue
        if not drive_url or "drive.google.com" not in drive_url:
            log.warning(f"Row {row_idx} '{title}' — no Drive URL in column H, skipping.")
            continue

        rows.append({
            "row_idx":   row_idx,
            "title":     title,
            "drive_url": drive_url,
        })

    log.info(f"Sheet: {len(rows)} row(s) ready to process.")
    return rows


def update_sheet_row(sheet, row_idx: int, processed_url: str, trim_sec: int):
    """Write processed video URL and update status to 'Processed'."""
    try:
        sheet.update_cell(row_idx, COL_PROCESSED, processed_url)
        sheet.update_cell(row_idx, COL_STATUS,    "Processed")
        sheet.update_cell(
            row_idx, COL_NOTES,
            f"✅ Processed | Logo: top-left ({LOGO_X},{LOGO_Y}) | Trimmed: {trim_sec}s"
        )
        log.info(f"Sheet: row {row_idx} updated → Processed")
    except Exception as e:
        log.warning(f"Sheet: could not update row {row_idx} — {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  GOOGLE DRIVE
# ══════════════════════════════════════════════════════════════════════════════
def get_drive_service(creds):
    svc = build("drive", "v3", credentials=creds)
    log.info("Drive: service ready.")
    return svc


def extract_file_id(url: str) -> str:
    """Extract file ID from any Google Drive share URL format."""
    m = re.search(r"/file/d/([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1)
    raise ValueError(f"Cannot extract file ID from: {url}")


def download_from_drive(svc, file_id: str, dest: Path) -> bool:
    """Download a Drive file to local path. Returns True on success."""
    try:
        req  = svc.files().get_media(fileId=file_id)
        fh   = _io.FileIO(str(dest), "wb")
        dl   = MediaIoBaseDownload(fh, req, chunksize=10 * 1024 * 1024)
        done = False
        while not done:
            status, done = dl.next_chunk()
            if status:
                log.info(f"Drive: downloading... {int(status.progress()*100)}%")
        log.info(f"Drive: downloaded → {dest.name}")
        return True
    except Exception as e:
        log.error(f"Drive: download failed — {e}")
        return False


def get_or_create_subfolder(svc, parent_id: str, name: str) -> str:
    """Find or create a subfolder inside parent_id. Returns folder ID."""
    q = (f"name='{name}' and '{parent_id}' in parents "
         f"and mimeType='application/vnd.google-apps.folder' and trashed=false")
    res   = svc.files().list(q=q, fields="files(id,name)").execute()
    files = res.get("files", [])
    if files:
        fid = files[0]["id"]
        log.info(f"Drive: found existing subfolder '{name}' → {fid}")
        return fid
    meta   = {"name": name,
               "mimeType": "application/vnd.google-apps.folder",
               "parents": [parent_id]}
    folder = svc.files().create(body=meta, fields="id").execute()
    fid    = folder["id"]
    log.info(f"Drive: created subfolder '{name}' → {fid}")
    return fid


def upload_to_drive(svc, local_path: Path, folder_id: str) -> str:
    """Upload file to Drive folder, make public, return shareable URL."""
    media = MediaFileUpload(str(local_path), mimetype="video/mp4", resumable=True)
    meta  = {"name": local_path.name, "parents": [folder_id]}
    req   = svc.files().create(body=meta, media_body=media, fields="id,webViewLink")
    resp  = None
    while resp is None:
        status, resp = req.next_chunk()
        if status:
            log.info(f"Drive: uploading... {int(status.progress()*100)}%")
    file_id  = resp["id"]
    view_url = resp.get("webViewLink",
                        f"https://drive.google.com/file/d/{file_id}/view")
    svc.permissions().create(
        fileId=file_id,
        body={"role": "reader", "type": "anyone"}
    ).execute()
    log.info(f"Drive: uploaded → {view_url}")
    return view_url


# ══════════════════════════════════════════════════════════════════════════════
#  FFMPEG HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def find_ffmpeg() -> str:
    """Locate ffmpeg binary. Raises RuntimeError if not found."""
    ff = shutil.which("ffmpeg")
    if ff:
        return ff
    # Common Windows paths
    for p in [r"C:\ffmpeg\bin\ffmpeg.exe",
              r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"]:
        if os.path.isfile(p):
            return p
    raise RuntimeError(
        "ffmpeg not found.\n"
        "  Install option 1 (easiest): pip install imageio-ffmpeg\n"
        "  Install option 2: https://ffmpeg.org/download.html"
    )


def get_duration(ffmpeg: str, path: Path) -> float:
    """Return video duration in seconds."""
    ffprobe = shutil.which("ffprobe") or ffmpeg.replace("ffmpeg", "ffprobe")
    try:
        r = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json",
             "-show_format", str(path)],
            capture_output=True, text=True, timeout=30
        )
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        pass
    # Fallback via ffmpeg stderr
    r = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, timeout=30
    )
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", r.stderr)
    if m:
        h, mi, s = m.groups()
        return int(h) * 3600 + int(mi) * 60 + float(s)
    raise RuntimeError(f"Cannot read duration of {path}")


def run_ffmpeg_process(ffmpeg: str, input_path: Path,
                       output_path: Path, logo: Path,
                       trim_sec: int) -> bool:
    """
    Single FFmpeg command that does BOTH:
      1. Overlay logo.png at top-left corner  → hides MagicLight watermark
      2. Trim last N seconds from end         → removes MagicLight outro
    Returns True on success.
    """
    log.info(f"FFmpeg: getting duration of '{input_path.name}'...")
    try:
        duration = get_duration(ffmpeg, input_path)
    except Exception as e:
        log.error(f"FFmpeg: cannot read duration — {e}")
        return False

    end_time = max(1.0, duration - trim_sec)
    log.info(f"FFmpeg: duration={duration:.1f}s  trim={trim_sec}s  "
             f"output ends at {end_time:.1f}s")

    if not logo.exists():
        log.warning(f"FFmpeg: logo not found at '{logo}' — trim only (no overlay)")
        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(input_path),
            "-t", str(end_time),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(output_path)
        ]
    else:
        # Build logo filter:  scale → opacity (if < 1) → overlay at (X, Y)
        logo_f = f"[1:v]scale={LOGO_WIDTH}:-1"
        if LOGO_OPACITY < 1.0:
            logo_f += f",colorchannelmixer=aa={LOGO_OPACITY:.2f}"
        logo_f += f"[logo];[0:v][logo]overlay=x={LOGO_X}:y={LOGO_Y}[v]"

        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i",  str(input_path),
            "-i",  str(logo),
            "-t",  str(end_time),
            "-filter_complex", logo_f,
            "-map", "[v]",
            "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(output_path)
        ]

    log.info(f"FFmpeg: processing '{input_path.name}'...")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode == 0:
        mb = output_path.stat().st_size / (1024 * 1024)
        log.info(f"FFmpeg: done → '{output_path.name}' ({mb:.1f} MB)")
        return True

    log.error(f"FFmpeg: FAILED\n{result.stderr[-800:]}")
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  SAFE FOLDER NAME  (same logic as AutoMagicAI — keeps Drive folder matching)
# ══════════════════════════════════════════════════════════════════════════════
def safe_name(row_idx: int, title: str) -> str:
    return (
        f"Row_{row_idx}_{title[:40]}"
        .replace(" ", "_").replace("/", "_").replace("\\", "_")
        .replace(":", "_").replace("*", "_").replace("?", "_")
        .replace('"', "_").replace("<", "_").replace(">", "_")
        .replace("|", "_")
    )


# ══════════════════════════════════════════════════════════════════════════════
#  MODE 1 — CLOUD MODE  (GitHub Actions)
#  Reads rows from Google Sheet, downloads from Drive, processes, re-uploads
# ══════════════════════════════════════════════════════════════════════════════
def run_cloud_mode(args, ffmpeg: str, logo: Path):
    log.info("=" * 60)
    log.info("MODE: Cloud (Google Sheet + Drive)")
    log.info("=" * 60)

    if not SPREADSHEET_ID:
        log.error("SPREADSHEET_ID is not set. Check .env or GitHub Secrets.")
        sys.exit(1)
    if not DRIVE_FOLDER_ID:
        log.error("GOOGLE_DRIVE_FOLDER_ID is not set. Check .env or GitHub Secrets.")
        sys.exit(1)

    creds = get_credentials()
    sheet = get_sheet(creds)
    svc   = get_drive_service(creds)

    rows = get_sheet_rows(sheet)
    if not rows:
        log.info("Nothing to process — all Done rows are already processed.")
        return

    if args.max:
        rows = rows[:args.max]
        log.info(f"Limit: processing max {args.max} row(s).")

    if args.dry_run:
        log.info("DRY RUN — rows that would be processed:")
        for r in rows:
            log.info(f"  Row {r['row_idx']:>3} | {r['title']}")
        log.info(f"Total: {len(rows)} row(s).")
        return

    tmp_dir    = Path(tempfile.mkdtemp(prefix="vp_cloud_"))
    ok_count   = fail_count = 0
    t0         = time.time()

    try:
        for row in rows:
            row_idx   = row["row_idx"]
            title     = row["title"]
            drive_url = row["drive_url"]
            folder_nm = safe_name(row_idx, title)

            log.info(f"\n{'─'*50}")
            log.info(f"Processing row {row_idx}: {title}")

            # Download
            try:
                file_id = extract_file_id(drive_url)
            except ValueError as e:
                log.error(str(e))
                fail_count += 1
                continue

            raw_path = tmp_dir / f"{folder_nm}_raw.mp4"
            if not download_from_drive(svc, file_id, raw_path):
                fail_count += 1
                continue

            # Process
            out_path = tmp_dir / f"{folder_nm}_processed.mp4"
            ok = run_ffmpeg_process(ffmpeg, raw_path, out_path, logo, TRIM_SECONDS)
            raw_path.unlink(missing_ok=True)   # free space immediately

            if not ok:
                fail_count += 1
                continue

            # Upload
            try:
                sub_id = get_or_create_subfolder(svc, DRIVE_FOLDER_ID, folder_nm)
                url    = upload_to_drive(svc, out_path, sub_id)
            except Exception as e:
                log.error(f"Drive upload failed — {e}")
                fail_count += 1
                out_path.unlink(missing_ok=True)
                continue
            finally:
                out_path.unlink(missing_ok=True)

            # Update sheet
            update_sheet_row(sheet, row_idx, url, TRIM_SECONDS)
            ok_count += 1

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    elapsed = int(time.time() - t0)
    log.info(f"\n{'='*60}")
    log.info(f"Cloud mode done! ✅ {ok_count} processed  ❌ {fail_count} failed  "
             f"⏱ {elapsed}s")
    log.info(f"{'='*60}")

    if fail_count:
        sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════════
#  MODE 2 — LOCAL MODE  (PC)
#  Scans INPUT_FOLDER (or downloads/) for .mp4 files, processes them,
#  uploads to Drive, updates Sheet if credentials are available.
# ══════════════════════════════════════════════════════════════════════════════
def collect_local_videos(scan_root: Path) -> list[Path]:
    """
    Recursively find all video files under scan_root.
    Skips files that already end with _processed (already done).
    """
    found = []
    for p in sorted(scan_root.rglob("*")):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            if "_processed" not in p.stem:
                found.append(p)
    return found


def run_local_mode(args, ffmpeg: str, logo: Path):
    log.info("=" * 60)
    log.info("MODE: Local PC")
    log.info("=" * 60)

    # ── Determine scan root ───────────────────────────────────────────────────
    if INPUT_FOLDER:
        scan_root = Path(INPUT_FOLDER)
    else:
        # Default: look for a 'downloads' folder next to process.py
        scan_root = Path(__file__).parent / "downloads"

    if not scan_root.exists():
        log.warning(f"Scan folder does not exist: {scan_root}")
        log.info("Creating it now — add videos there and run again.")
        scan_root.mkdir(parents=True, exist_ok=True)
        return

    log.info(f"Scanning: {scan_root.resolve()}")
    videos = collect_local_videos(scan_root)

    if not videos:
        log.info(f"No unprocessed videos found in '{scan_root}'.")
        log.info("Put .mp4 files (or subfolders with .mp4) in that folder and run again.")
        return

    log.info(f"Found {len(videos)} video(s):")
    for v in videos:
        log.info(f"  {v.relative_to(scan_root)}")

    if args.max:
        videos = videos[:args.max]
        log.info(f"Limit: processing max {args.max} video(s).")

    if args.dry_run:
        log.info("DRY RUN — no files will be changed.")
        return

    # ── Try to connect to Drive/Sheet (optional for local mode) ──────────────
    svc   = None
    sheet = None
    if _GOOGLE_OK and (os.path.exists("credentials.json") or
                       os.getenv("GOOGLE_CREDENTIALS")):
        try:
            creds = get_credentials()
            svc   = get_drive_service(creds)
            if SPREADSHEET_ID:
                sheet = get_sheet(creds)
            log.info("Google Drive + Sheet: connected.")
        except Exception as e:
            log.warning(f"Google connection failed — local processing only. ({e})")
    else:
        log.info("No credentials found — will process locally without Drive upload.")

    ok_count = fail_count = 0
    t0 = time.time()

    for i, video_path in enumerate(videos, 1):
        rel = video_path.relative_to(scan_root)
        log.info(f"\n{'─'*50}")
        log.info(f"[{i}/{len(videos)}] {rel}")

        # Output path: same folder, _processed suffix
        if OUTPUT_FOLDER:
            out_dir = Path(OUTPUT_FOLDER) / video_path.parent.relative_to(scan_root)
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"{video_path.stem}_processed.mp4"
        else:
            out_path = video_path.parent / f"{video_path.stem}_processed.mp4"

        # Process
        ok = run_ffmpeg_process(ffmpeg, video_path, out_path, logo, TRIM_SECONDS)
        if not ok:
            fail_count += 1
            continue

        ok_count += 1

        # Upload to Drive if connected
        if svc and DRIVE_FOLDER_ID:
            try:
                # Mirror subfolder structure in Drive
                # subfolder name = parent folder name of the video
                subfolder_name = video_path.parent.name or "Processed"
                sub_id = get_or_create_subfolder(svc, DRIVE_FOLDER_ID, subfolder_name)
                drive_url = upload_to_drive(svc, out_path, sub_id)

                # Update sheet if available — match by video parent folder name
                if sheet and SPREADSHEET_ID:
                    # Find row where title roughly matches folder name
                    _try_sheet_update_by_folder(sheet, subfolder_name,
                                                drive_url, TRIM_SECONDS)
            except Exception as e:
                log.warning(f"Drive/Sheet update failed — {e}")

    elapsed = int(time.time() - t0)
    log.info(f"\n{'='*60}")
    log.info(f"Local mode done! ✅ {ok_count} processed  ❌ {fail_count} failed  "
             f"⏱ {elapsed}s")
    log.info(f"{'='*60}")

    if fail_count:
        sys.exit(1)


def _try_sheet_update_by_folder(sheet, folder_name: str,
                                 drive_url: str, trim_sec: int):
    """
    Try to find a matching row in the sheet by comparing the folder name
    to the safe_name of each row. Updates if found.
    This is a best-effort match — won't crash if no match is found.
    """
    try:
        all_data = sheet.get_all_values()
        for row_idx, row in enumerate(all_data[1:], start=2):
            while len(row) < COL_PROCESSED:
                row.append("")
            title     = row[COL_TITLE - 1].strip()
            status    = row[COL_STATUS - 1].strip().lower()
            processed = row[COL_PROCESSED - 1].strip()
            if processed:
                continue
            # Build the safe name the same way AutoMagicAI does
            expected = safe_name(row_idx, title)
            if expected == folder_name or title.replace(" ", "_") in folder_name:
                update_sheet_row(sheet, row_idx, drive_url, trim_sec)
                return
        log.info(f"Sheet: no matching row found for folder '{folder_name}' "
                 f"— Drive upload OK but sheet not updated.")
    except Exception as e:
        log.warning(f"Sheet lookup failed — {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(
        description=(
            "VideoProcessor — Logo overlay + Trim + Drive upload\n"
            "Runs in two modes:\n"
            "  cloud  → reads Google Sheet, downloads from Drive\n"
            "  local  → scans INPUT_FOLDER (or downloads/) on this PC"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--mode", choices=["cloud", "local"], default=None,
        help=(
            "cloud = GitHub Actions mode (Sheet + Drive)\n"
            "local = PC mode (scan folder)\n"
            "Default: auto-detect (cloud if SPREADSHEET_ID is set)"
        )
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Scan and print what would be processed, but make no changes."
    )
    parser.add_argument(
        "--max", type=int, default=None,
        help="Maximum number of videos to process in this run."
    )
    args = parser.parse_args()

    # ── Auto-detect mode ──────────────────────────────────────────────────────
    if args.mode is None:
        if SPREADSHEET_ID and os.getenv("GOOGLE_CREDENTIALS"):
            args.mode = "cloud"
            log.info("Auto-detected mode: cloud (SPREADSHEET_ID + GOOGLE_CREDENTIALS found)")
        else:
            args.mode = "local"
            log.info("Auto-detected mode: local (no SPREADSHEET_ID or GOOGLE_CREDENTIALS)")

    log.info("=" * 60)
    log.info("  VideoProcessor")
    log.info(f"  Mode      : {args.mode}")
    log.info(f"  Trim      : {TRIM_SECONDS}s from end")
    log.info(f"  Logo      : {LOGO_PATH}  pos=({LOGO_X},{LOGO_Y})  w={LOGO_WIDTH}px")
    log.info(f"  Drive     : {DRIVE_FOLDER_ID or 'NOT SET'}")
    log.info(f"  Dry run   : {args.dry_run}")
    log.info("=" * 60)

    # ── Find FFmpeg ───────────────────────────────────────────────────────────
    try:
        ffmpeg = find_ffmpeg()
        log.info(f"FFmpeg: {ffmpeg}")
    except RuntimeError as e:
        log.error(str(e))
        sys.exit(1)

    # ── Find logo ─────────────────────────────────────────────────────────────
    logo = Path(LOGO_PATH)
    if logo.exists():
        log.info(f"Logo: found → {logo.resolve()}")
    else:
        log.warning(f"Logo NOT found at '{logo}' — will trim only, no watermark cover.")

    # ── Run selected mode ─────────────────────────────────────────────────────
    if args.mode == "cloud":
        run_cloud_mode(args, ffmpeg, logo)
    else:
        run_local_mode(args, ffmpeg, logo)


if __name__ == "__main__":
    main()
