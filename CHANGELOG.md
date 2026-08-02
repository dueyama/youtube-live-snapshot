# Changelog

## 3.3.1 - 2026-08-02

- Increase the default AI timestamp tolerance from two to six minutes so the
  freshness gate allows modest camera-clock skew as well as stream delay.

## 3.3.0 - 2026-08-02

- Publish an official image only after every enabled mechanical and AI check
  passes.
- Preserve failed attempts on a best-effort basis under the output directory's
  `rejected/` archive, with a safe JSON record and the attempted PNG when one
  exists; keep the hidden attempt PNG if the archive itself is unavailable.
- Distinguish confirmed `rejected` frames from `unverified` AI results and
  pre-image `capture_error` failures.
- Keep all failure types within one bounded attempt budget; `max_retries: 3`
  means three total attempts including the first.
- Make `enforce` the default AI mode and treat the former `advisory` value as a
  deprecated enforcing alias so an AI failure can no longer become official.
- Keep rejected evidence outside official capture discovery, calendar input,
  and normal capture pruning, with no automatic deletion.
- Preserve an existing official filename on collision, archive the newly
  validated but unsaved attempt, and never report that collision as a
  successful save.

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
- Require stream URLs and sunset coordinates through runtime configuration.
- Add path validation, broader runtime-data ignores, CI, and third-party font notices.
- Document Raspberry Pi OS, general Linux, macOS, and AI-assisted setup.
