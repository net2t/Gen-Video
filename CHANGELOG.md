# Changelog

All notable changes to MagicLight Auto are documented here.  
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [1.0.0] — 2026-04-01

### 🎉 First stable release — fully tested single & multi-story runs

### Added
- **Multi-account rotation** via `ACCOUNTS=email:pass,email:pass` in `.env`
  - Per-account session files (`auth_*.json`) for persistent login
  - Auto-detects credit exhaustion and switches account
- **Active popup dismissal** — `_wait_dismissing()` kills popups every 5 s during all long waits, preventing UI blockage
- **Precise dialog handling** — detects real blocking dialogs via `.arco-modal-mask` backdrop (not body text), checks "Don't remind again" checkbox, closes with ✕
- **Animation panel awareness** — `animation-modal__tab` panel on Storyboard step correctly handled with Escape, never confused with the enhance dialog
- **Header-only Next clicks** — step4 navigation loop strictly targets `header-shiny-action__btn`, never modal/footer buttons
- **Render progress monitoring** — polls `%` progress element every 10 s, logs updates, reloads page every 2 min
- **Post-render preview popup dismissal** — clears download-preview modal before attempting download
- **Per-story output folders** — `output/row{N}_{title}/` with `.mp4` + `_thumb.jpg`
- **Metadata extraction** — Title, Summary, Hashtags written to `stories.csv` after each story
- **User Center retry** — on error, opens user-center, finds project by URL ID, card, or direct navigation
- **Clean shutdown** — Ctrl+C handler saves in-progress state before exit
- **`--max N`** flag to limit processing count
- **`--headless`** flag for server/background runs
- **`CHANGELOG.md`**, `README.md`, `.gitignore`, `.env.example`

### Fixed
- Login correctly clicks `.entry-email` tab before filling `input[type="text"]` (not `type="email"`)
- Stale `auth.json` auto-deleted and login retried with fresh session
- `handle_generation_dialog` removed from step4 loop (was clicking random Next buttons and causing infinite loops)
- Post-login popups: explicitly clicks Skip → Close samples in codegen order

### Security
- `.env` and `auth_*.json` excluded from git via `.gitignore`
- Credentials only loaded from environment variables, never hardcoded

---

## Planned — [1.1.0]

- [ ] Scheduled runs (cron / Task Scheduler integration)
- [ ] Google Sheets sync instead of CSV
- [ ] Telegram/Discord notification on completion
- [ ] Auto-upload downloaded video to YouTube/TikTok
- [ ] Headless reliability improvements (stealth mode)
