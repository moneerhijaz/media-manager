# media-manager

`media-manager` is a stateful Linux command-line tool for inspecting and safely repairing a Plex-style media library. It remembers completed work, checkpoints long operations, and prefers a review result over an uncertain destructive change.

Its pipeline can:

- identify movies and TV episodes from a configurable library layout;
- inspect container integrity and record media fingerprints;
- analyze dialogue clips for audio/video lip-sync with SyncNet;
- apply a verified global A/V timestamp correction without re-encoding;
- scan the full video for permanent black borders and variable aspect ratios;
- apply lossless H.264 crop metadata when the evidence is safe;
- select existing, embedded, or OpenSubtitles captions;
- synchronize subtitles with ffsubsync and reject implausible output; and
- preserve stage decisions and provenance in SQLite.

> [!WARNING]
> This program can replace media and subtitle files after verification. Keep backups, test on disposable copies first, review the configuration, and use `plan` or `--dry-run` before processing a valuable library.

## Safety model

Media transformations are created in `.media-manager/work/` and checked with ffprobe before an atomic replacement. The original file is left untouched when analysis, transformation, or verification fails.

The manager records uncertain outcomes as `needs_review`, `insufficient_evidence`, or `unavailable`. It does not turn weak evidence into an automatic correction. Completed stages and intra-stage checkpoints survive a clean pause, process interruption, or reboot.

The project is designed for one mutating process per library. A lock prevents overlapping `run`, `resume`, `scan`, and `plan` commands; read-only status commands and `pause` remain available from another terminal.

## Platform and dependencies

| Component | Required | Purpose |
| --- | --- | --- |
| Linux | Yes | POSIX process control, advisory locking, and atomic replacement behavior |
| Python 3.11+ | Yes | Application runtime |
| FFmpeg and ffprobe | Yes | Probing, audio/frame extraction, remuxing, and crop metadata |
| NumPy 1.26+ | Yes | Native-pixel crop analysis |
| ffsubsync 0.5.1+ | Yes | Subtitle/audio alignment |
| OpenSubtitles account/API key | No | Downloading subtitles when no acceptable local subtitle exists |
| SyncNet stack and model weights | No | Learned A/V lip-sync analysis |

NumPy and ffsubsync are Python package dependencies. FFmpeg/ffprobe must be installed through the operating system and available on `PATH`.

### Optional SyncNet A/V stack

Install the `avsync` extra to enable learned lip-sync analysis:

- `syncnet-python`;
- PyTorch;
- OpenCV;
- Silero VAD; and
- PySceneDetect 0.6.x.

The PySceneDetect version is intentionally constrained below 0.7 because `syncnet-python 0.2.2` imports its legacy `VideoManager` API. Silero is used for speech-window selection; FFmpeg silence detection is the fallback.

SyncNet model weights are not bundled in this repository. `media-manager setup-syncnet` downloads them into the current library's private `.media-manager/models/` directory.

CPU inference is supported but can be slow on long or difficult media. The implementation tests short, speech-rich windows rather than every frame and checkpoints every completed candidate.

Synchformer appears in configuration as a future backend but is not implemented in this release.

## Installation

### pipx (recommended for the CLI)

From a cloned repository:

```bash
pipx install .
```

Install with the optional A/V stack:

```bash
pipx install '.[avsync]'
```

The distribution is named `stateful-media-manager`; the installed command is `media-manager`.

### Virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e .
```

For A/V sync and development tools:

```bash
python -m pip install -e '.[avsync,dev]'
```

### Minimal local installer

`install.sh` copies the package to `~/.local/lib/stateful-media-manager/` and creates `~/.local/bin/media-manager`. It does not install Python or system dependencies, so install those first.

```bash
chmod +x install.sh
./install.sh
```

The matching `uninstall.sh` removes program files only. It does not delete any library's `.media-manager/` state.

## Library layout

Run the command from the library root. By default, only `Movies/` and `TV Shows/` are managed:

```text
Media/
├── Movies/
│   ├── HD/
│   │   └── Example Film (2024)/
│   │       └── Example Film (2024).mp4
│   └── SD/
└── TV Shows/
    ├── HD/
    │   └── Example Series (2025)/
    │       └── Season 01/
    │           ├── Example Series (2025) - s01e01 - First Chapter.mp4
    │           └── Example Series (2025) - s01e02 - Second Chapter.mp4
    └── SD/
```

Movie/episode classification comes from the directory tree. Episode filenames must include a case-insensitive `sXXeXX` token, and the season must agree with the `Season XX` folder.

Directories, extensions, languages, and processing policy can be changed in `.media-manager/config.toml` after initialization.

## Quick start

```bash
cd "/path/to/Media"
media-manager init
media-manager doctor
media-manager plan
media-manager run --dry-run
media-manager run --limit 10
```

For SyncNet:

```bash
media-manager setup-syncnet
media-manager doctor
```

For OpenSubtitles:

```bash
media-manager config opensubtitles
```

`init` is optional: `run` initializes the current directory automatically when needed.

## Common commands

```bash
media-manager scan                   # discover/update state without processing
media-manager plan                   # show pending work
media-manager run                    # scan and process unfinished media
media-manager run --limit 10         # limit the pending batch
media-manager run --no-av-sync       # temporarily skip A/V analysis
media-manager run --no-crop          # temporarily skip crop analysis
media-manager run --no-subtitles     # temporarily skip subtitles
media-manager resume                 # continue the checkpointed pipeline
media-manager pause                  # request a clean stop from another terminal
media-manager status --stages        # stage totals and current checkpoint
media-manager progress --watch       # live current-stage view
media-manager review                 # list items requiring attention
media-manager explain PATH           # show decisions for one media file
media-manager retry --failed         # reset failed stages
media-manager retry --review         # reset review stages
media-manager retry --stage crop PATH
media-manager config                 # print effective config with API key redacted
media-manager doctor                 # check tools, modules, weights, and provider setup
```

One-run skip flags do not mark a stage complete. A later normal run will still process the skipped stage.

Multiple subtitle languages can be requested with:

```bash
media-manager run --subtitle-language en,es
```

## Pause and recovery

From the active terminal, press `Ctrl+C`. From another terminal in the same library, run:

```bash
media-manager pause
```

The pause command waits for the active operation and child tools to stop cleanly. If a genuinely unresponsive process cannot stop, use:

```bash
media-manager pause --force
```

Forced stopping resumes from the last durable checkpoint rather than claiming the current unit completed. Continue with `media-manager resume`.

State is stored under the library root:

```text
.media-manager/
├── config.toml
├── media-manager.db
├── checkpoints/
├── logs/
├── models/
├── work/
├── opensubtitles-password
└── manager.lock
```

The SQLite database is authoritative. Human-readable `.media-manager.json` markers may also be written beside managed media.

## A/V synchronization

The A/V stage selects an authoritative audio stream, finds sustained speech, ranks distributed candidate windows, and sends promising windows to SyncNet. It uses multiple high-confidence observations to estimate a global offset, spread, and drift.

Before committing a correction, media-manager tests both shift directions on an independent clip and reruns SyncNet. A full-file timestamp correction is applied only when validation materially improves alignment. Video and audio are stream-copied rather than re-encoded.

Multiple audio streams block automatic correction by default. To deliberately shift all audio streams together:

```toml
[av_sync]
shift_all_audio_streams = true
```

Animation and mixed imagery may be analyzed, but their corrections default to review-only. Progressive drift is also review-only because a single global timestamp shift cannot repair it safely.

## Crop analysis

The crop engine samples native-resolution luma frames throughout the entire video and measures unused outer rows and columns. It excludes black/fade frames, records variable-aspect evidence, and chooses only a crop common to all verified picture states.

The current lossless application backend supports H.264 by adding verified margins through FFmpeg's `h264_metadata` bitstream filter. It stream-copies the file and preserves an existing SPS crop. Other codecs are reported as not applicable rather than re-encoded.

Brief aspect-ratio changes can fall between baseline samples. Increase `crop.scan_fps` when catching very short full-frame inserts matters more than scan time.

## Subtitle synchronization

For each requested language, media-manager prefers:

1. a language-qualified sidecar;
2. a plain sidecar assigned to the configured language;
3. an embedded text subtitle stream; then
4. a validated OpenSubtitles result.

Ordinary full-runtime ffsubsync is the primary alignment path. Sparse multi-segment alignment is used only as a fallback after a low-quality or runtime-invalid primary result.

The source and synchronized SRT are validated independently. A candidate with too few cues or an implausible runtime cannot replace an existing subtitle. Low-quality alignments and probable wrong-release subtitles are preserved for review instead of being silently truncated or installed.

## OpenSubtitles credentials

The safest interactive setup is:

```bash
media-manager config opensubtitles
```

The username and API key are written to `.media-manager/config.toml`; the password is stored separately in `.media-manager/opensubtitles-password`. Both files are created with mode `0600`. The password must remain recoverable because the provider requires its original value during login.

Environment overrides are available for unattended execution:

```text
OPENSUBTITLES_USERNAME
OPENSUBTITLES_API_KEY
OPENSUBTITLES_PASSWORD
OPENSUBTITLES_TOKEN
```

API-key command-line flags remain available for automation but can be visible in process listings and shell history. Prefer interactive configuration or environment injection. The config command redacts the API key, and recorded run commands redact secret flags.

## Privacy and network behavior

The source repository contains no media, subtitles, model weights, user configuration, or telemetry. Runtime state can reveal absolute paths, account identifiers, media titles, and processing history, so never publish `.media-manager/`, `.media-manager.json`, logs, databases, checkpoints, or work files.

Network access occurs only for explicitly used features:

- OpenSubtitles API requests and subtitle downloads; and
- SyncNet weight downloads requested with `setup-syncnet`.

Review `git status` and `git diff --cached` before every public push. The included `.gitignore` is a safety net, not a substitute for checking staged content.

## Development and tests

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
```

The suite includes unit tests and FFmpeg integration tests. A/V policy tests can run without downloading model weights; real SyncNet inference requires the optional stack and weights.

Build release artifacts with:

```bash
python -m pip install build
python -m build
```

Generated `build/`, `dist/`, `*.egg-info/`, caches, media, subtitles, logs, models, and runtime state are excluded from Git.

## Current limitations

- Linux is the supported platform.
- Only configured media extensions are scanned; `.mp4` is the default.
- Lossless crop application currently targets H.264.
- A/V correction supports a verified global offset, not time-varying drift repair.
- SyncNet analysis is computationally expensive and speaking faces are not available in every title.
- Synchformer and soundtrack-based dubbed-audio alignment are not implemented.
- OpenSubtitles matching and ffsubsync can still produce uncertain results, which remain review-only.

The governing rule is simple: uncertainty must not become an automatic destructive edit.

## License

MIT. See [LICENSE](LICENSE).
