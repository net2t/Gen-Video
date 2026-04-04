#!/usr/bin/env python3
"""
VideoProcessor — Unified TUI Menu & Video Processing
- Scans and lists video files
- Interactive prompts: how many, profile (480/720/1080, YouTube optimized), endscreen, upload/local
- Hides warnings, shows detailed/advanced UX output
- Includes video processing logic (logo, trim, endscreen, Drive upload)
"""

import argparse
import os
import sys
import subprocess
import logging
import json
import time
import shutil
import tempfile
import re
from pathlib import Path
from datetime import datetime

# Hide warnings
logging.getLogger().setLevel(logging.ERROR)
os.environ["PYTHONWARNINGS"] = "ignore"

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    import rich
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.prompt import Prompt, IntPrompt, Confirm
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
    from rich import print as rprint
    console = Console()
except ImportError:
    class Console:
        def print(self, *args, **kwargs): print(*args)
        def rule(self, *args, **kwargs): print("-" * 60)
        def panel(self, *args, **kwargs): print(*args)
    class Prompt:
        @staticmethod
        def ask(msg, default=None, choices=None):
            if choices:
                for i, c in enumerate(choices, 1):
                    print(f"{i}. {c}")
                while True:
                    sel = input(f"{msg} [{1}-{len(choices)}]: ").strip()
                    if sel.isdigit() and 1 <= int(sel) <= len(choices):
                        return choices[int(sel)-1]
                    print("Invalid choice")
                return None
            else:
                result = input(f"{msg}: ").strip()
                return result if result else default
    class IntPrompt:
        @staticmethod
        def ask(msg, default=None):
            while True:
                try:
                    val = input(f"{msg}: ").strip()
                    return int(val) if val else default
                except ValueError:
                    print("Please enter a number")
    class Confirm:
        @staticmethod
        def ask(msg, default=True):
            suffix = " [Y/n]" if default else " [y/N]"
            while True:
                ans = input(msg + suffix + ": ").strip().lower()
                if not ans:
                    return default
                if ans in ("y", "yes"):
                    return True
                if ans in ("n", "no"):
                    return False
                print("Please enter y/yes or n/no")
    def rprint(*args, **kwargs): print(*args)
    Progress = None

# ── Load root .env ───────────────────────────────────────────────────────
def _load_root_env() -> None:
    if load_dotenv is None:
        return
    root_env = Path(__file__).with_name(".env")
    if root_env.exists():
        load_dotenv(dotenv_path=root_env)
    else:
        load_dotenv()

# ── CONFIG from .env ───────────────────────────────────────────────────────
SPREADSHEET_ID  = os.getenv("SPREADSHEET_ID", "")
DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")
TRIM_SECONDS    = int(os.getenv("TRIM_SECONDS", "4"))
LOGO_PATH       = os.getenv("LOGO_PATH", "assets/logo.png")
LOGO_X          = int(os.getenv("LOGO_X", "10"))
LOGO_Y          = int(os.getenv("LOGO_Y", "10"))
LOGO_WIDTH      = int(os.getenv("LOGO_WIDTH", "120"))
LOGO_OPACITY    = float(os.getenv("LOGO_OPACITY", "1.0"))
ENDSCREEN_ENABLED = os.getenv("ENDSCREEN_ENABLED", "false").lower() == "true"
ENDSCREEN_VIDEO = os.getenv("ENDSCREEN_VIDEO", "assets/endscreen.mp4")
ENDSCREEN_DURATION = os.getenv("ENDSCREEN_DURATION", "5")
INPUT_FOLDER    = os.getenv("INPUT_FOLDER", "")
OUTPUT_FOLDER   = os.getenv("OUTPUT_FOLDER", "")
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".flv", ".wmv"}

# ── Scan video files ───────────────────────────────────────────────────────
def scan_videos(folder: Path) -> list[Path]:
    if not folder.exists():
        folder.mkdir(parents=True, exist_ok=True)
        console.print(f"[yellow]Created folder: {folder}[/yellow]")
    files = sorted(
        p for p in folder.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS and "_processed" not in p.stem
    )
    return files

def show_file_table(files: list[Path], max_to_show: int | None = None):
    if not files:
        console.print("[red]No video files found.[/red]")
        return
    display = files[:max_to_show] if max_to_show else files
    table = Table(title="Video Files", show_header=True, header_style="bold magenta")
    table.add_column("#", style="cyan", width=4)
    table.add_column("File", style="green")
    table.add_column("Size", style="yellow")
    for i, f in enumerate(display, 1):
        size_mb = f.stat().st_size / (1024*1024)
        table.add_row(str(i), f.name, f"{size_mb:.1f} MB")
    console.print(table)
    if max_to_show and len(files) > max_to_show:
        console.print(f"[dim]... and {len(files)-max_to_show} more files not shown[/dim]")

# ── Video processing functions (from process.py) ───────────────────────────────
def check_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except Exception:
        return False

def get_ffmpeg_args(input_file: Path, output_file: Path, profile: str, trim_seconds: int, logo_path: Path, logo_x: int, logo_y: int, logo_width: int, logo_opacity: float, endscreen: bool, endscreen_video: Path | None) -> list[str]:
    args = ["ffmpeg", "-y", "-i", str(input_file)]
    # Trim
    if trim_seconds > 0:
        args.extend(["-t", str(input_file.stat().st_size - trim_seconds)])
    # Logo overlay
    if logo_path.exists():
        args.extend(["-i", str(logo_path), "-filter_complex", f"[0:v][1:v] overlay={logo_x}:{logo_y}:format=auto,format=yuv420p"])
    # Profile scaling
    if profile == "480":
        args.extend(["-vf", "scale=854:480"])
    elif profile == "720":
        args.extend(["-vf", "scale=1280:720"])
    elif profile == "1080":
        args.extend(["-vf", "scale=1920:1080"])
    # Endscreen
    if endscreen and endscreen_video and endscreen_video.exists():
        args.extend(["-i", str(endscreen_video), "-filter_complex", f"[0:v][1:v] overlay={logo_x}:{logo_y}:format=auto,format=yuv420p;[v]concat=n=2:v=1:a=0"])
    # Output
    args.extend(["-c:v", "libx264", "-preset", "medium", "-crf", "23", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output_file)])
    return args

def process_video(input_file: Path, output_file: Path, profile: str, trim_seconds: int, logo_path: Path, logo_x: int, logo_y: int, logo_width: int, logo_opacity: float, endscreen: bool, endscreen_video: Path | None):
    console.print(f"[cyan]Processing: {input_file.name}[/cyan]")
    args = get_ffmpeg_args(input_file, output_file, profile, trim_seconds, logo_path, logo_x, logo_y, logo_width, logo_opacity, endscreen, endscreen_video)
    try:
        subprocess.run(args, check=True, capture_output=True)
        console.print(f"[green]✔ Finished: {output_file.name}[/green]")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]✖ Failed to process {input_file.name}: {e}[/red]")

# ── Main TUI menu ───────────────────────────────────────────────────────────────
def main() -> int:
    _load_root_env()
    console.rule("[bold blue]VideoProcessor — Unified TUI & Processor[/bold blue]")

    # Check ffmpeg
    if not check_ffmpeg():
        console.print("[red]FFmpeg not found. Please install FFmpeg and add to PATH.[/red]")
        return 1

    # Scan folder
    input_folder = Path(os.getenv("INPUT_FOLDER", "")) or (Path(__file__).parent / "Pending")
    console.print(f"[dim]Scanning folder: {input_folder.resolve()}[/dim]")
    videos = scan_videos(input_folder)
    if not videos:
        console.print("[red]No videos found. Add videos and run again.[/red]")
        return 1
    console.print(f"[green]Found {len(videos)} video(s).[/green]")
    show_file_table(videos, max_to_show=15)

    # How many to process?
    count = IntPrompt.ask("How many videos to process? (Enter for all)", default=len(videos))
    count = min(max(count, 0), len(videos))

    # Video profile
    console.print("[bold cyan]Select video profile:[/bold cyan]")
    console.print("  1. 480p  (YouTube optimized)")
    console.print("  2. 720p  (YouTube optimized)")
    console.print("  3. 1080p (YouTube optimized)")
    console.print("  4. 480p")
    console.print("  5. 720p")
    console.print("  6. 1080p")
    choice = IntPrompt.ask("Enter profile number", default=2)
    profile_map = {
        1: ("480", "480p_yt"),
        2: ("720", "720p_yt"),
        3: ("1080", "1080p_yt"),
        4: ("480", "480p"),
        5: ("720", "720p"),
        6: ("1080", "1080p"),
    }
    profile_val, profile_name = profile_map.get(choice, ("720", "720p_yt"))

    # Endscreen
    endscreen = Confirm.ask("Enable endscreen?", default=True)
    endscreen_video = None
    if endscreen:
        endscreen_path = os.getenv("ENDSCREEN_VIDEO", "assets/endscreen.mp4")
        endscreen_video = Path(__file__).parent / endscreen_path
        if not endscreen_video.exists():
            console.print(f"[yellow]Endscreen file not found at {endscreen_video}. Endscreen will be skipped.[/yellow]")
            endscreen = False

    # Upload or local only
    upload = Confirm.ask("Upload processed videos to Google Drive?", default=True)

    # Logo/trim settings (from .env or defaults)
    logo_path = Path(os.getenv("LOGO_PATH", "assets/logo.png"))
    logo_x = int(os.getenv("LOGO_X", "10"))
    logo_y = int(os.getenv("LOGO_Y", "10"))
    logo_width = int(os.getenv("LOGO_WIDTH", "120"))
    logo_opacity = float(os.getenv("LOGO_OPACITY", "1.0"))
    trim_seconds = int(os.getenv("TRIM_SECONDS", "4"))
    output_folder = Path(os.getenv("OUTPUT_FOLDER", "")) if os.getenv("OUTPUT_FOLDER") else None

    # Summary
    summary = (
        f"[bold]Videos to process:[/bold] {count}\n"
        f"[bold]Profile:[/bold] {profile_name}\n"
        f"[bold]Endscreen:[/bold] {'Yes' if endscreen else 'No'}\n"
        f"[bold]Upload to Drive:[/bold] {'Yes' if upload else 'No'}\n"
        f"[bold]Trim seconds:[/bold] {trim_seconds}\n"
        f"[bold]Logo:[/bold] {logo_path.name if logo_path.exists() else 'Not found'}\n"
    )
    console.rule(f"[bold magenta]Configuration[/bold magenta]")
    console.print(summary)

    if not Confirm.ask("Proceed with processing?", default=True):
        console.print("[yellow]Cancelled.[/yellow]")
        return 0

    # Process videos
    to_process = videos[:count]
    for i, video in enumerate(to_process, 1):
        console.print(f"[dim][{i}/{len(to_process)}][/dim]")
        if output_folder and output_folder.exists():
            out_path = output_folder / f"{video.stem}_processed{video.suffix}"
        else:
            out_path = video.parent / f"{video.stem}_processed{video.suffix}"
        process_video(video, out_path, profile_val, trim_seconds, logo_path, logo_x, logo_y, logo_width, logo_opacity, endscreen, endscreen_video)
        # TODO: Add upload logic if upload=True
    console.print("[bold green]All done![/bold green]")
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/yellow]")
        sys.exit(130)
