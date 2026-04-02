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
    from google.oauth2.credentials import Credentials as OAuthCredentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
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

# ── Endscreen config ─────────────────────────────────────────────────────────
ENDSCREEN_ENABLED     = os.getenv("ENDSCREEN_ENABLED", "false").lower() == "true"
ENDSCREEN_VIDEO       = os.getenv("ENDSCREEN_VIDEO",   "endscreen.mp4")
ENDSCREEN_DURATION    = os.getenv("ENDSCREEN_DURATION", "5")  # Can be "auto" or a number

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
#  PROFILE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════
def load_profiles() -> dict:
    """
    Load profiles from profiles.json file.
    Returns dict with profile data, or empty dict if file not found.
    """
    profiles_file = Path("profiles.json")
    if not profiles_file.exists():
        log.warning("profiles.json not found - using default settings")
        return {}
    
    try:
        with open(profiles_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        log.info(f"Loaded {len(data.get('profiles', {}))} profile(s) from profiles.json")
        return data
    except Exception as e:
        log.error(f"Failed to load profiles.json - {e}")
        return {}


def get_profile(profile_name: str = None) -> dict:
    """
    Get a specific profile by name, or return default profile.
    Falls back to hardcoded defaults if no profiles file exists.
    """
    profiles_data = load_profiles()
    profiles = profiles_data.get('profiles', {})
    
    # Determine which profile to use
    if profile_name and profile_name in profiles:
        selected_profile = profiles[profile_name]
        log.info(f"Using profile: {profile_name} - {selected_profile.get('description', '')}")
    elif profile_name:
        log.warning(f"Profile '{profile_name}' not found, using default")
        selected_profile = profiles.get('default', {})
    else:
        # Use environment variable or default
        env_profile = os.getenv("VIDEO_PROFILE", "")
        if env_profile and env_profile in profiles:
            selected_profile = profiles[env_profile]
            log.info(f"Using profile from env: {env_profile} - {selected_profile.get('description', '')}")
        else:
            default_profile_name = profiles_data.get('default_profile', 'default')
            selected_profile = profiles.get(default_profile_name, {})
            log.info(f"Using default profile: {default_profile_name}")
    
    # Merge with hardcoded defaults for backward compatibility
    return merge_profile_with_defaults(selected_profile)


def merge_profile_with_defaults(profile: dict) -> dict:
    """
    Merge profile settings with hardcoded defaults for backward compatibility.
    Environment variables take precedence over profile settings.
    """
    # Start with hardcoded defaults
    merged = {
        "video_processing": {
            "trim_seconds": TRIM_SECONDS,
            "logo_enabled": True,
            "logo_path": LOGO_PATH,
            "logo_x": LOGO_X,
            "logo_y": LOGO_Y,
            "logo_width": LOGO_WIDTH,
            "logo_opacity": LOGO_OPACITY,
            "endscreen_enabled": ENDSCREEN_ENABLED,
            "endscreen_video": ENDSCREEN_VIDEO,
            "endscreen_duration": ENDSCREEN_DURATION,
        },
        "output_settings": {
            "video_codec": "libx264",
            "audio_codec": "aac",
            "video_preset": "veryfast",
            "crf": 23,
            "audio_bitrate": "128k",
            "pixel_format": "yuv420p",
            "movflags": "+faststart",
        },
        "youtube_optimization": {
            "target_resolution": "1920x1080",
            "aspect_ratio": "16:9",
            "frame_rate": 30,
            "bitrate_strategy": "auto",
        }
    }
    
    # Override with profile settings
    if profile:
        for section in ["video_processing", "output_settings", "youtube_optimization"]:
            if section in profile:
                merged[section].update(profile[section])
    
    # Override with environment variables (highest precedence)
    merged["video_processing"]["trim_seconds"] = TRIM_SECONDS
    merged["video_processing"]["logo_path"] = LOGO_PATH
    merged["video_processing"]["logo_x"] = LOGO_X
    merged["video_processing"]["logo_y"] = LOGO_Y
    merged["video_processing"]["logo_width"] = LOGO_WIDTH
    merged["video_processing"]["logo_opacity"] = LOGO_OPACITY
    merged["video_processing"]["endscreen_enabled"] = ENDSCREEN_ENABLED
    merged["video_processing"]["endscreen_video"] = ENDSCREEN_VIDEO
    merged["video_processing"]["endscreen_duration"] = ENDSCREEN_DURATION
    
    return merged


def list_profiles() -> list[str]:
    """
    Return list of available profile names.
    """
    profiles_data = load_profiles()
    return list(profiles_data.get('profiles', {}).keys())


def print_profiles():
    """
    Print all available profiles with their descriptions.
    """
    profiles_data = load_profiles()
    profiles = profiles_data.get('profiles', {})
    
    if not profiles:
        log.info("No profiles found in profiles.json")
        return
    
    log.info("Available profiles:")
    log.info("=" * 50)
    for name, profile in profiles.items():
        desc = profile.get('description', 'No description')
        is_default = name == profiles_data.get('default_profile', 'default')
        marker = " (default)" if is_default else ""
        log.info(f"  {name}{marker}")
        log.info(f"    {desc}")
    log.info("=" * 50)


# ══════════════════════════════════════════════════════════════════════════════
#  LOGGING  — console output only (no log files)
# ══════════════════════════════════════════════════════════════════════════════
def setup_logging() -> logging.Logger:
    """
    Set up logging to console only (no log files).
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),           # console only
        ],
    )
    logger = logging.getLogger("VideoProcessor")
    return logger


log = setup_logging()


# ══════════════════════════════════════════════════════════════════════════════
#  AUTHENTICATION
#    Priority order:
#    1. GOOGLE_CREDENTIALS env var  (GitHub Actions — full JSON string)
#    2. credentials.json file       (local PC - service account)
#    3. auth.json file             (local PC - OAuth)
# ══════════════════════════════════════════════════════════════════════════════
def get_credentials():
    if not _GOOGLE_OK:
        log.error("Google libraries not installed. Run: pip install -r requirements.txt")
        sys.exit(1)

    # Try service account first (for GitHub Actions)
    creds_str = os.getenv("GOOGLE_CREDENTIALS", "")
    if creds_str:
        try:
            info = json.loads(creds_str)
            log.info("Auth: using GOOGLE_CREDENTIALS env var (GitHub Actions)")
            return SACredentials.from_service_account_info(info, scopes=SCOPES)
        except json.JSONDecodeError as e:
            log.error(f"Auth: failed to parse GOOGLE_CREDENTIALS JSON — {e}")
            sys.exit(1)

    # Try service account file (local PC)
    creds_file = Path("credentials.json")
    if creds_file.exists():
        log.info("Auth: using credentials.json (local PC - service account)")
        return SACredentials.from_service_account_file(str(creds_file), scopes=SCOPES)

    # Try OAuth flow (local PC - solves storage quota issue)
    auth_file = Path("auth.json")
    token_file = Path("token.json")
    
    if auth_file.exists():
        creds = None
        if token_file.exists():
            try:
                creds = OAuthCredentials.from_authorized_user_file(str(token_file), SCOPES)
                log.info("Auth: using existing token.json (OAuth)")
            except Exception:
                creds = None
        
        # If there are no (valid) credentials available, let the user log in.
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    log.info("Auth: refreshed OAuth token")
                except Exception as e:
                    log.warning(f"Auth: token refresh failed — {e}")
                    creds = None
            
            if not creds:
                try:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        str(auth_file), 
                        SCOPES,
                        redirect_uri='http://localhost:8080/'
                    )
                    creds = flow.run_local_server(port=8080)
                    log.info("Auth: completed OAuth flow")
                except Exception as e:
                    log.error(f"Auth: OAuth flow failed — {e}")
                    sys.exit(1)
            
            # Save the credentials for the next run
            with open(token_file, 'w') as token:
                token.write(creds.to_json())
        
        return creds

    log.error(
        "No credentials found!\n"
        "  Options:\n"
        "    • Local PC (service account) → place credentials.json in project folder\n"
        "    • Local PC (OAuth)          → place auth.json in project folder\n"
        "    • GitHub Actions            → add GOOGLE_CREDENTIALS secret"
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


def get_video_info(ffmpeg: str, path: Path) -> dict:
    """Get video information including resolution and duration."""
    ffprobe = shutil.which("ffprobe") or ffmpeg.replace("ffmpeg", "ffprobe")
    cmd = [ffprobe, "-v", "quiet", "-print_format", "json", 
           "-select_streams", "v:0", "-show_entries", 
           "stream=width,height,duration", str(path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        # Fallback to get_duration only
        try:
            duration = get_duration(ffmpeg, path)
            return {"width": 1920, "height": 1080, "duration": duration}
        except:
            raise RuntimeError(f"Cannot read video info from {path}")
    
    import json as _json
    info = _json.loads(result.stdout)
    stream = info.get("streams", [{}])[0]
    
    return {
        "width": int(stream.get("width", 1920)),
        "height": int(stream.get("height", 1080)),
        "duration": float(stream.get("duration", 0))
    }


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
                       trim_sec: int, profile: dict = None) -> bool:
    """
    Single FFmpeg command that does:
      1. Overlay logo.png at top-left corner  → hides MagicLight watermark
      2. Trim last N seconds from end         → removes MagicLight outro
      3. Add endscreen (if enabled)          → custom branding/outro
    Returns True on success.
    """
    # Use profile settings or fallback to provided parameters
    if profile:
        vp = profile.get('video_processing', {})
        actual_trim_sec = vp.get('trim_seconds', trim_sec)
        logo_enabled = vp.get('logo_enabled', True)
        logo_x = vp.get('logo_x', LOGO_X)
        logo_y = vp.get('logo_y', LOGO_Y)
        logo_width = vp.get('logo_width', LOGO_WIDTH)
        logo_opacity = vp.get('logo_opacity', LOGO_OPACITY)
        endscreen_enabled = vp.get('endscreen_enabled', ENDSCREEN_ENABLED)
        endscreen_video = vp.get('endscreen_video', ENDSCREEN_VIDEO)
        endscreen_duration = vp.get('endscreen_duration', ENDSCREEN_DURATION)
        
        out = profile.get('output_settings', {})
        video_codec = out.get('video_codec', 'libx264')
        audio_codec = out.get('audio_codec', 'aac')
        video_preset = out.get('video_preset', 'veryfast')
        crf = out.get('crf', 23)
        audio_bitrate = out.get('audio_bitrate', '128k')
        pixel_format = out.get('pixel_format', 'yuv420p')
        movflags = out.get('movflags', '+faststart')
    else:
        # Fallback to provided parameters and global variables
        actual_trim_sec = trim_sec
        logo_enabled = True
        logo_x = LOGO_X
        logo_y = LOGO_Y
        logo_width = LOGO_WIDTH
        logo_opacity = LOGO_OPACITY
        endscreen_enabled = ENDSCREEN_ENABLED
        endscreen_video = ENDSCREEN_VIDEO
        endscreen_duration = ENDSCREEN_DURATION
        
        video_codec = 'libx264'
        audio_codec = 'aac'
        video_preset = 'veryfast'
        crf = 23
        audio_bitrate = '128k'
        pixel_format = 'yuv420p'
        movflags = '+faststart'

    log.info(f"FFmpeg: getting duration of '{input_path.name}'...")
    try:
        input_info = get_video_info(ffmpeg, input_path)
        duration = input_info["duration"]
        input_width = input_info["width"]
        input_height = input_info["height"]
    except Exception as e:
        log.error(f"FFmpeg: cannot read video info — {e}")
        return False

    # Check if endscreen is enabled and file exists
    endscreen_path = None
    actual_endscreen_duration = endscreen_duration
    if endscreen_enabled:
        endscreen_path = Path(endscreen_video)
        if not endscreen_path.exists():
            log.warning(f"FFmpeg: endscreen enabled but file not found at '{endscreen_path}' — skipping endscreen")
            endscreen_path = None
        else:
            # Auto-detect duration if set to "auto"
            if endscreen_duration == "auto":
                try:
                    actual_endscreen_duration = get_duration(ffmpeg, endscreen_path)
                    log.info(f"FFmpeg: endscreen enabled → auto-detected {actual_endscreen_duration:.1f}s from '{endscreen_path.name}'")
                except Exception as e:
                    log.warning(f"FFmpeg: failed to get endscreen duration — {e} — using 5s default")
                    actual_endscreen_duration = 5
            else:
                actual_endscreen_duration = float(endscreen_duration)
                log.info(f"FFmpeg: endscreen enabled → {actual_endscreen_duration}s from '{endscreen_path.name}'")

    end_time = max(1.0, duration - actual_trim_sec)
    log.info(f"FFmpeg: duration={duration:.1f}s  trim={actual_trim_sec}s  "
             f"output ends at {end_time:.1f}s")

    if not logo_enabled or not logo.exists():
        if not logo_enabled:
            log.info(f"FFmpeg: logo disabled in profile — trim only (no overlay)")
        else:
            log.warning(f"FFmpeg: logo not found at '{logo}' — trim only (no overlay)")
        
        if endscreen_path:
            # Trim + endscreen (no logo) - simple concat
            cmd = [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(input_path),
                "-i", str(endscreen_path),
                "-filter_complex", f"[0:v]trim=end={end_time},format=yuv420p[v1];[1:v]trim=duration={actual_endscreen_duration},scale={input_width}:{input_height},format=yuv420p[v2];[v1][v2]concat=n=2:v=1:a=0[outv]",
                "-map", "[outv]",
                "-map", "0:a?",
                "-c:v", video_codec, "-preset", video_preset, "-crf", str(crf),
                "-c:a", audio_codec, "-b:a", audio_bitrate,
                "-pix_fmt", pixel_format, "-movflags", movflags,
                str(output_path)
            ]
        else:
            # Trim only (no logo, no endscreen)
            cmd = [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(input_path),
                "-t", str(end_time),
                "-c:v", video_codec, "-preset", video_preset, "-crf", str(crf),
                "-c:a", audio_codec, "-b:a", audio_bitrate,
                "-pix_fmt", pixel_format, "-movflags", movflags,
                str(output_path)
            ]
    else:
        if endscreen_path:
            # Logo + trim + endscreen - simple concat
            logo_f = f"[1:v]scale={logo_width}:-1"
            if logo_opacity < 1.0:
                logo_f += f",colorchannelmixer=aa={logo_opacity:.2f}"
            logo_f += f"[logo];[0:v][logo]overlay=x={logo_x}:y={logo_y}[v1];[v1]trim=end={end_time},format=yuv420p[v2];[2:v]trim=duration={actual_endscreen_duration},scale={input_width}:{input_height},format=yuv420p[v3];[v2][v3]concat=n=2:v=1:a=0[outv]"
            
            cmd = [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(input_path),
                "-i", str(logo),
                "-i", str(endscreen_path),
                "-filter_complex", logo_f,
                "-map", "[outv]",
                "-map", "0:a?",
                "-c:v", video_codec, "-preset", video_preset, "-crf", str(crf),
                "-c:a", audio_codec, "-b:a", audio_bitrate,
                "-pix_fmt", pixel_format, "-movflags", movflags,
                str(output_path)
            ]
        else:
            # Logo + trim (no endscreen)
            logo_f = f"[1:v]scale={logo_width}:-1"
            if logo_opacity < 1.0:
                logo_f += f",colorchannelmixer=aa={logo_opacity:.2f}"
            logo_f += f"[logo];[0:v][logo]overlay=x={logo_x}:y={logo_y}[v]"

            cmd = [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-i",  str(input_path),
                "-i",  str(logo),
                "-t",  str(end_time),
                "-filter_complex", logo_f,
                "-map", "[v]",
                "-map", "0:a?",
                "-c:v", video_codec, "-preset", video_preset, "-crf", str(crf),
                "-c:a", audio_codec, "-b:a", audio_bitrate,
                "-pix_fmt", pixel_format, "-movflags", movflags,
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
def run_cloud_mode(args, ffmpeg: str, logo: Path, profile: dict = None):
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
            ok = run_ffmpeg_process(ffmpeg, raw_path, out_path, logo, TRIM_SECONDS, profile)
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


def run_local_mode(args, ffmpeg: str, logo: Path, profile: dict = None):
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
                       os.path.exists("auth.json") or
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
        ok = run_ffmpeg_process(ffmpeg, video_path, out_path, logo, TRIM_SECONDS, profile)
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
        "--profile", default=None,
        help="Processing profile to use (from profiles.json). Use --list-profiles to see options."
    )
    parser.add_argument(
        "--list-profiles", action="store_true",
        help="List all available processing profiles and exit."
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

    # Handle --list-profiles
    if args.list_profiles:
        print_profiles()
        sys.exit(0)

    # Load profile
    profile = get_profile(args.profile)
    
    # Extract profile settings for display
    vp = profile.get('video_processing', {})
    out = profile.get('output_settings', {})

    # ── Auto-detect mode ──────────────────────────────────────────────────────
    if args.mode is None:
        has_cloud_creds = bool(os.getenv("GOOGLE_CREDENTIALS"))
        has_local_creds = os.path.exists("credentials.json") or os.path.exists("auth.json")
        
        if SPREADSHEET_ID and has_cloud_creds:
            args.mode = "cloud"
            log.info("Auto-detected mode: cloud (SPREADSHEET_ID + GOOGLE_CREDENTIALS found)")
        elif SPREADSHEET_ID and has_local_creds:
            args.mode = "cloud"
            log.info("Auto-detected mode: cloud (SPREADSHEET_ID + local credentials found)")
        else:
            args.mode = "local"
            log.info("Auto-detected mode: local (no SPREADSHEET_ID or credentials)")

    log.info("=" * 60)
    log.info("  VideoProcessor")
    log.info(f"  Mode      : {args.mode}")
    log.info(f"  Profile   : {args.profile or 'default'}")
    log.info(f"  Trim      : {vp.get('trim_seconds', TRIM_SECONDS)}s from end")
    log.info(f"  Logo      : {vp.get('logo_path', LOGO_PATH)}  pos=({vp.get('logo_x', LOGO_X)},{vp.get('logo_y', LOGO_Y)})  w={vp.get('logo_width', LOGO_WIDTH)}px")
    log.info(f"  Endscreen : {'enabled' if vp.get('endscreen_enabled', ENDSCREEN_ENABLED) else 'disabled'}")
    log.info(f"  Quality   : {out.get('video_codec', 'libx264')} CRF {out.get('crf', 23)} preset {out.get('video_preset', 'veryfast')}")
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
    logo_path = vp.get('logo_path', LOGO_PATH)
    logo = Path(logo_path)
    if vp.get('logo_enabled', True) and logo.exists():
        log.info(f"Logo: found → {logo.resolve()}")
    elif vp.get('logo_enabled', True):
        log.warning(f"Logo NOT found at '{logo}' — will trim only, no watermark cover.")
    else:
        log.info("Logo: disabled in profile")

    # ── Check endscreen ───────────────────────────────────────────────────────
    if vp.get('endscreen_enabled', ENDSCREEN_ENABLED):
        endscreen_video = vp.get('endscreen_video', ENDSCREEN_VIDEO)
        endscreen = Path(endscreen_video)
        if endscreen.exists():
            endscreen_duration = vp.get('endscreen_duration', ENDSCREEN_DURATION)
            if endscreen_duration == "auto":
                log.info(f"Endscreen: enabled → auto duration from '{endscreen.name}'")
            else:
                log.info(f"Endscreen: enabled → {endscreen_duration}s from '{endscreen.name}'")
        else:
            log.warning(f"Endscreen enabled but file not found at '{endscreen}' — skipping endscreen")
    else:
        log.info("Endscreen: disabled")

    # ── Run selected mode ─────────────────────────────────────────────────────
    if args.mode == "cloud":
        run_cloud_mode(args, ffmpeg, logo, profile)
    else:
        run_local_mode(args, ffmpeg, logo, profile)


if __name__ == "__main__":
    main()
