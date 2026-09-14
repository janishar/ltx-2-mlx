# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the project is pre-1.0 (`0.x.y`), breaking changes bump `y` and additive
changes bump `z`. See [`docs/PIPELINE_MATURITY.md`](docs/PIPELINE_MATURITY.md)
for per-pipeline stability guarantees.

## [Unreleased]

### Features

- **Official LTX-2.5 weights, converted on load.** `--model` accepts a directory
  of the official Lightricks LTX-2.5 files; weights are converted to the MLX
  layout in memory and quantized with `--quantize-on-load {8,4,none}`. Only
  configs and tokenizer assets are cached, in `<repo>/.cache/`.
- **Mode launcher** (`scripts/ltx_run.py`) for every generation path, with a
  model capability check.
- **ltx studio** (`web/`): stdlib-only local web UI for every command, with
  sessions, input library, job queue, streaming log, take actions (reuse
  settings, chain, frames, video/audio reuse) and a timeline editor that
  combines clips across sessions.
- **Optional live preview** in ltx studio: stepwise previews streamed into the
  viewer while denoising, configurable interval, clip length and position, with
  a per-take preview scrubber.
- VS Code launch, task and settings configurations.

### Changed

- The CLI no longer defaults to a hosted model: `--model` falls back to
  `$LTX_MODEL` and exits with an error when neither is set.
- Weight-gated tests read local pack paths from `LTX_TEST_MODEL_DIR` (LTX-2.3
  int8) and `LTX_TEST_LTX25_PACK_DIR` (LTX-2.5 int8) instead of a fixed
  Hugging Face cache location.
- Releases are cut manually with `scripts/bump_version.py`,
  `scripts/validate_versions.py` and `scripts/generate_changelog.py`; the
  automated release workflows were removed.

## 0.15.4 and earlier

This project is a standalone fork. Changes up to and including `0.15.4` are
recorded in the git history (`git log v0.15.4`, or `git log 461df21` if the
tag is not present locally).
