# Project Instructions

## Service and data boundary

- This repository owns YouTube Live Snapshot capture/render features, tests, Python dependencies, deployment artifacts, and image/render behavior.
- `ytlive-snapshot capture` and `ytlive-snapshot render` are the public commands.
- Host service units, process lifecycle, boot ordering, credentials, and OS maintenance belong to the host-management layer, not this repository.
- Do not start, stop, or restart a production service while performing application-only work unless the user explicitly requests it.
- Persistent captures must live outside the replaceable application directory.
- Do not delete or overwrite persistent captures unless the user explicitly requests that exact operation.
- Keep host names, user names, private stream URLs, coordinates, credentials, and host-specific absolute paths out of tracked files.

## Verification

- Run `python -m unittest discover -s tests -v` after changing Python behavior.
- Compile the `ytlive_snapshot` package and the test suite.
- Run `bash -n` for every changed shell script.
- Render both `ja` and `en` calendar samples after changing calendar layout or localization.
- Report application tests separately from any production deployment or service verification.
