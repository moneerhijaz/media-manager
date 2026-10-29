# Safety boundaries

Back up originals before `run`, `resume`, or reviewed-stage retries. `plan` is the recommended first look. Work on copies until you have inspected representative results.

The executor creates candidate output, verifies it, writes a commit journal, and then replaces the source. Checkpoints are checked against source signatures before reuse. Uncertain A/V results are recorded for review rather than treated as proof of synchronization. These mechanisms reduce risk; they do not establish perceptual correctness or eliminate storage/power failures.

Keep `.media-manager/`, `.env`, subtitle-provider credentials, web tokens, SQLite data, logs, media, and model weights private. Never publish a library directory as a demo. Default CLI web binding is `0.0.0.0`; pass `--host 127.0.0.1` for local-only evaluation and protect first-time setup. Docker defaults and remote access are described in [operations](OPERATIONS.md).

`install.sh` replaces program files beneath `$PREFIX/lib/stateful-media-manager`; `uninstall.sh` removes that program directory and wrapper. They do not manage Python dependencies or erase per-library state. Prefer a virtual environment or pipx; review PREFIX before running either script. Do not use `docker compose down -v` unless intentionally deleting shared dashboard state.

No model weights or external API credentials were used in the core test run. A synthetic scan transcript demonstrates inventory only, not successful real-world repair.
