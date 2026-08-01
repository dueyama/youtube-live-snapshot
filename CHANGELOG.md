# Changelog

## 3.1.0 - 2026-08-01

- Rename the project to YouTube Live Snapshot (`youtube-live-snapshot`).
- Add the unified `ytlive-snapshot capture` and `ytlive-snapshot render` commands.
- Move capture and rendering implementations into the `ytlive_snapshot` package.
- Remove former Python and shell compatibility entry points.
- Normalize YouTube watch, share, live, and embed URLs for embedded capture.
- Add Japanese and English calendar labels.
- Support same-year and shifted-source-year calendars.
- Add multiple configurable fixed capture times per day.
- Keep date-based sunset capture with a configurable offset and prefix.
- Allow safe custom capture prefixes in renderer categories.
- Move private stream URLs and coordinates out of source defaults.
- Add path validation, broader runtime-data ignores, CI, and third-party font notices.
- Document Raspberry Pi OS, general Linux, macOS, and AI-assisted setup.
