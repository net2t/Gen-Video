import argparse
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


def _load_root_env() -> None:
    if load_dotenv is None:
        return
    root_env = Path(__file__).with_name(".env")
    if root_env.exists():
        load_dotenv(dotenv_path=root_env)
    else:
        load_dotenv()


def _run_story_to_video(argv: list[str]) -> int:
    # Import lazily so dependencies are only needed when option 1 is used
    temp_folder = Path(__file__).parent / "Temp-Folder"
    sys.path.insert(0, str(temp_folder))
    import main as automagic_main  # type: ignore

    # Ensure AutoMagicAI uses root paths for shared files
    repo_root = Path(__file__).parent
    os.environ.setdefault("DOTENV_PATH", str(repo_root / ".env"))
    os.environ.setdefault("CREDS_FILE", str(repo_root / "credentials.json"))
    os.environ.setdefault("OAUTH_CREDS_FILE", str(repo_root / "oauth_credentials.json"))
    os.environ.setdefault("OAUTH_TOKEN_FILE", str(repo_root / "token.json"))
    os.environ.setdefault("COOKIES_FILE", str(repo_root / "cookies.json"))
    os.environ.setdefault("DOWNLOADS_DIR", str(repo_root / "downloads"))

    # Let Temp-Folder/main.py parse its own args
    saved_argv = sys.argv
    try:
        sys.argv = [saved_argv[0], *argv]
        automagic_main.main()
        return 0
    finally:
        sys.argv = saved_argv


def _run_video_processor(argv: list[str]) -> int:
    import process

    saved_argv = sys.argv
    try:
        sys.argv = [saved_argv[0], *argv]
        process.main()
        return 0
    finally:
        sys.argv = saved_argv


def main() -> int:
    _load_root_env()

    p = argparse.ArgumentParser(
        description=(
            "VideoProcessor — unified entry\n\n"
            "1) Story → Video generation (MagicLight / AutoMagicAI)\n"
            "2) Video processing (logo/trim/endscreen + Drive upload)\n"
        )
    )
    p.add_argument(
        "--menu",
        action="store_true",
        help="Show interactive menu instead of selecting a mode via flags.",
    )
    p.add_argument(
        "--mode",
        choices=["generate", "process"],
        default=None,
        help="generate = story→video generation, process = video processing",
    )

    # Everything after `--` is passed through to the selected sub-tool
    p.add_argument("pass_through", nargs=argparse.REMAINDER)
    args = p.parse_args()

    def _strip_double_dash(rest: list[str]) -> list[str]:
        return rest[1:] if rest[:1] == ["--"] else rest

    if args.menu or args.mode is None:
        print("=" * 60)
        print("  VideoProcessor — Main Menu")
        print("  1: Generate Story → Video")
        print("  2: Process/Edit videos + Upload")
        print("=" * 60)
        choice = input("Select an option (1, 2, or q to quit): ").strip().lower()
        if choice == "q":
            return 0
        if choice == "1":
            return _run_story_to_video(_strip_double_dash(args.pass_through))
        if choice == "2":
            return _run_video_processor(_strip_double_dash(args.pass_through))
        print("Invalid choice")
        return 2

    if args.mode == "generate":
        return _run_story_to_video(_strip_double_dash(args.pass_through))

    return _run_video_processor(_strip_double_dash(args.pass_through))


if __name__ == "__main__":
    raise SystemExit(main())
