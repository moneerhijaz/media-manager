# CLI reference

Run from an initialized library root. `media-manager --help` and `media-manager help COMMAND` are the authoritative argument reference.

| Task | Commands | Effects |
| --- | --- | --- |
| Initialize / inventory | `init`, `scan`, `plan` | Writes state; plan does not repair media |
| Environment | `doctor`, `setup-syncnet`, `setup-languages` | Setup downloads optional model files |
| Process | `run`, `resume` | May replace media/subtitles after verification |
| Review | `review`, `explain PATH`, `history` | Inspect recorded decisions |
| Retry | `retry PATH`, `rerun-reviewed` | Reset/reprocess selected work |
| Control | `pause`, `progress --watch`, `status --stages` | Pause or observe active processing |
| Configuration | `config`, `config opensubtitles`, `set-visual-type PATH TYPE` | Updates private library settings |
| Dashboard | `web --host 127.0.0.1` | Starts local authenticated dashboard |

`run --dry-run` is a non-destructive plan. `--no-av-sync`, `--no-crop`, and `--no-subtitles` disable individual stages. `resume --retry-subtitles-now` bypasses scheduling, not validation. Provider credentials belong in private config, never in a public transcript or shell-history example.

See [operations](OPERATIONS.md) for model setup, Docker mounts, migration, authentication, and retry timers. Those procedures are retained rather than compressed into an unsafe one-line installer.
