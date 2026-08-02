# Changelog

## 3.2.1 - 2026-08-02

- Wait up to 30 seconds for a preferred `1280x720` YouTube source frame.
- Accept `640x360` or better after that wait, but reject lower source resolutions
  and retry with a fresh browser session.
- Recheck source dimensions immediately before canvas capture so element, iframe,
  and full-screen fallbacks cannot publish an enlarged low-resolution frame.
- Keep source-resolution validation local, deterministic, configurable, and
  independent of optional AI inspection.

## 3.2.0 - 2026-08-02

- Add optional OpenAI vision inspection with `gpt-5.6-luna`, structured
  pass/fail results, timestamp checks, and advisory or enforcing modes.
- Keep AI inspection disabled by default and package its SDK as the optional
  `youtube-live-snapshot[ai]` extra.
- Keep YouTube controls available during inspection, wait for them to settle,
  and perform at most one direct LIVE-control action per capture attempt.
- Reject captures when YouTube's own LIVE control still reports delayed playback
  after that action.
- Ignore ambiguous DVR timing values when headless YouTube does not render a
  LIVE control, preserving the initially loaded frame instead of seeking about
  one hour backward.
- Validate screenshots before publishing them and retry blank, loading, and error frames.
- Write attempts to temporary files so failed validation never replaces persistent captures.
- Avoid waiting on a browser play promise that can remain pending in newer Chromium builds.

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
