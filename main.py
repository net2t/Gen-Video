#!/usr/bin/env python3
"""
VideoProcessor — Enhanced TUI Menu
- Scans and lists video files
- Interactive prompts: how many, profile (480/720), endscreen, upload/local
- Hides warnings, shows detailed/advanced UX output
"""

import argparse
import os
import sys
import subprocess
import logging
from pathlib import Path

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

# ── Scan video files ───────────────────────────────────────────────────────
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".flv", ".wmv"}

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

# ── Build process arguments from user choices ───────────────────────────────────
def build_process_args(
    count: int,
    profile: str,
    endscreen: bool,
    upload: bool,
    trim_seconds: int,
    logo_path: Path,
    logo_x: int,
    logo_y: int,
    logo_width: int,
    logo_opacity: float,
    endscreen_video: Path | None = None,
    output_folder: Path | None = None,
) -> list[str]:
    args = ["--mode", "local"]
    if count:
        args.extend(["--max", str(count)])
    # Pass profile via environment; process.py can map it to ffmpeg -crf or -vf scale
    os.environ["VIDEO_PROFILE"] = profile
    if endscreen and endscreen_video and endscreen_video.exists():
        os.environ["ENDSCREEN_ENABLED"] = "true"
        os.environ["ENDSCREEN_VIDEO"] = str(endscreen_video)
    else:
        os.environ["ENDSCREEN_ENABLED"] = "false"
    # Upload/local
    os.environ["UPLOAD_TO_DRIVE"] = "true" if upload else "false"
    # Trim/logo settings (override .env)
    os.environ["TRIM_SECONDS"] = str(trim_seconds)
    os.environ["LOGO_PATH"] = str(logo_path)
    os.environ["LOGO_X"] = str(logo_x)
    os.environ["LOGO_Y"] = str(logo_y)
    os.environ["LOGO_WIDTH"] = str(logo_width)
    os.environ["LOGO_OPACITY"] = str(logo_opacity)
    if output_folder:
        os.environ["OUTPUT_FOLDER"] = str(output_folder)
    return args

# ── Run process.py with args ───────────────────────────────────────────────────
def run_process_with_args(args: list[str]) -> int:
    import process
    saved_argv = sys.argv
    try:
        sys.argv = [saved_argv[0], *args]
        process.main()
        return 0
    finally:
        sys.argv = saved_argv

# ── Main TUI menu ───────────────────────────────────────────────────────────────
def main() -> int:
    _load_root_env()
    console.rule("[bold blue]VideoProcessor — Enhanced TUI[/bold blue]")

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

    # Summary panel
    summary = (
        f"[bold]Videos to process:[/bold] {count}\n"
        f"[bold]Profile:[/bold] {profile_name}\n"
        f"[bold]Endscreen:[/bold] {'Yes' if endscreen else 'No'}\n"
        f"[bold]Upload to Drive:[/bold] {'Yes' if upload else 'No'}\n"
        f"[bold]Trim seconds:[/bold] {trim_seconds}\n"
        f"[bold]Logo:[/bold] {logo_path.name if logo_path.exists() else 'Not found'}\n"
    )
    console.panel(summary, title="[bold magenta]Configuration[/bold magenta]")

    if not Confirm.ask("Proceed with processing?", default=True):
        console.print("[yellow]Cancelled.[/yellow]")
        return 0

    # Build args and run
    args = build_process_args(
        count=count,
        profile=profile_val,
        endscreen=endscreen,
        upload=upload,
        trim_seconds=trim_seconds,
        logo_path=logo_path,
        logo_x=logo_x,
        logo_y=logo_y,
        logo_width=logo_width,
        logo_opacity=logo_opacity,
        endscreen_video=endscreen_video,
        output_folder=output_folder,
    )
    console.rule("[bold green]Starting Processing[/bold green]")
    return run_process_with_args(args)

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/yellow]")
        sys.exit(130)
