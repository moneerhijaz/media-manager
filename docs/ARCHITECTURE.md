# Architecture

- `cli.py` and `web.py` expose the pipeline and review controls.
- `scanner.py`, `fingerprint.py`, `probe.py`, and `identify.py` inventory and identify media.
- `database.py` stores per-library stage state and events; `checkpoints.py` persists resumable analysis.
- `avsync/`, `crop/`, and `subtitles/` contain independent analysis/transformation stages.
- `executor.py` coordinates stage eligibility, candidate verification, commit journals, and recovery.
- `verifier.py` checks remux output; `utils.py` supplies replacement helpers.
- `libraries.py` manages dashboard library registration separately from library processing state.

A/V commit recovery can recognize already-replaced output after a crash through fingerprints. Source changes invalidate stale analysis. SQLite records stage outcomes while checkpoint files retain finer-grained progress; these are complementary stores, not a single transaction across all files.

See [decisions](DECISIONS.md) and [safety](SAFETY.md) for tradeoffs. Tests cover policy and recovery using fixtures; optional model correctness still requires a representative evaluation set.
