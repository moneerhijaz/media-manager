# Changelog

## 0.5.3

- Make ordinary full-runtime ffsubsync the primary subtitle alignment method.
- Use sparse multi-segment synchronization only when full-runtime alignment rejects itself or produces a runtime-invalid SRT.
- Inspect the source and synchronized SRT runtimes separately, preventing a bad `1.0427x` timing conversion from replacing an already plausible sidecar.
- Record runtime-invalid or wrong-release subtitles as actionable `NEEDS_REVIEW` outcomes while preserving existing sidecars and cached downloads.
- Keep a current-version low-quality subtitle review terminal so `resume` cannot repeat the same rejected alignment indefinitely.

## 0.5.2

- Start SyncNet with 8 runtime-distributed candidates instead of forcing 20, while preserving adaptive expansion up to 64 until three strong observations exist.
- Reduce lightweight face/mouth-motion ranking from 96 ten-second windows at 5 fps to 48 windows at 2 fps.
- Cap SyncNet preprocessing at 1280 pixels wide. On a controlled 4K benchmark segment, this preserved the 2-frame offset and confidence (5.94 vs. 5.95) while reducing candidate evaluation from 98.7 to 53.3 seconds.
- Include face-ranking rate and SyncNet preprocessing width in checkpoint compatibility so interrupted work cannot mix evidence from different performance profiles.
- Disable FFmpeg stdin interaction in A/V face ranking and candidate extraction.

## 0.5.1

- Reduce the default full-runtime crop baseline from one sample per second to one sample every five seconds (`crop.scan_fps = 0.2`).
- Keep 5 fps refinement around aspect-ratio transitions detected by the baseline.
- Disable FFmpeg stdin interaction for native-pixel crop scans, preventing terminal color responses from activating FFmpeg's interactive command mode.

## 0.5.0

- Add explicit, automatic OpenSubtitles password storage in the per-library `.media-manager/opensubtitles-password` credential file.
- Create and enforce credential-file mode `0600`, use an atomic durable write, and never include the password in normal configuration output, command arguments, SQLite run history, or logs.
- Make `media-manager config opensubtitles` collect username, API key, and password together; subsequent runs authenticate without another prompt.
- Keep `OPENSUBTITLES_PASSWORD` and `OPENSUBTITLES_TOKEN` as higher-priority runtime overrides.

## 0.4.9

- Suspend the live progress renderer while lazily prompting for the OpenSubtitles password, so the prompt remains visible and a run cannot look hung while waiting for terminal input.
- Route OpenSubtitles password collection through an injectable prompt callback while preserving environment-token/password behavior for unattended runs.

## 0.4.8

- Recognize ffsubsync low-quality safety messages even when Rich wraps the message and inserts a source-location column, preserving unchanged subtitles as review outcomes instead of hard failures.
- If sparse multi-segment synchronization rejects its own proposal, retry ffsubsync's full-runtime alignment before requiring review.
- Reconsider prior low-quality subtitle reviews once for the new fallback without invalidating already-complete subtitle stages.

## 0.4.7

- Prevent indefinite SyncNet candidate hangs by enforcing separate per-FFmpeg and whole-candidate deadlines.
- Run syncnet-python's FFmpeg children in dedicated process sessions and terminate the entire child group on timeout or interruption, including SIGKILL escalation for tools that ignore SIGTERM.
- Remove syncnet-python 0.2.2's obsolete global `-async 1` argument from its intermediate AVI conversion; explicit candidate audio remains authoritative.
- Cap SyncNet's intermediate video at 1920 pixels wide before S3FD processing; the real 4K hang candidate retained the same offset/confidence while evaluation time fell from 408 seconds to 114 seconds.
- Make SyncNet progress distinguish strong, usable, no-face, and failed results instead of comparing a result count to the confidence threshold.
- Make `media-manager pause` wait for a clean stop, detect and remove orphaned child tools, validate PID ownership, and offer explicit `--force` recovery when a run is unresponsive.
- Fsync checkpoint directories after atomic replacement so completed candidate checkpoints survive sudden power loss more reliably.
- Keep the existing A/V algorithm version so already checkpointed candidate results remain reusable; this release changes execution reliability, not SyncNet scoring policy.

## 0.4.6

- Treat ffsubsync's explicit low-quality safety no-op as a preserved, machine-readable review outcome instead of a hard subtitle failure.
- Never replace an existing subtitle or install a candidate when ffsubsync reports that it left the input unmodified.
- Record the rejected score, offset, scale, quality reason, source disposition, and structural validation in the subtitle stage details.
- Preserve ffsubsync's own safety thresholds as well as media-manager's existing post-sync validation safeguards.
- Kept the existing subtitle algorithm version so completed subtitles are not reprocessed; previously failed low-score stages already retry normally.

## 0.4.5

- Fixed the real SyncNet startup failure: `syncnet-python 0.2.2` requires the legacy PySceneDetect `VideoManager` API, so the A/V extra now constrains PySceneDetect to the compatible 0.6 series instead of accepting broken 0.7 releases.
- Replaced retrying lazy pipeline construction with an explicit, cached one-time preparation step that exposes suppressed third-party import errors and validates non-empty, readable weight files.
- Added phase-aware SyncNet adapter errors, strict seven-element result parsing, machine-readable A/V outcome classifications, grouped representative tracebacks, and runtime/package diagnostics.
- Added a three-identical-inference-error circuit breaker so deterministic backend failures stop immediately instead of consuming all 64 adaptive candidates, and remain decisive even when earlier candidates produced usable observations.
- Distinguished backend failure, no-face results, valid low-confidence results, and genuinely insufficient evidence in checkpoints, `explain`, and final stage reasons.
- Restore PyTorch's process-wide CPU thread count after Silero VAD, preventing Silero's single-thread setting from making subsequent SyncNet inference several times slower.
- Contained SyncNet's raw FFmpeg, scene-detection, and frame-confidence chatter so the terminal keeps its single continuously refreshed progress line during inference.
- Bumped the A/V algorithm version to `avsync-v8-syncnet-adapter-fix`, selectively requeueing A/V analysis without invalidating crop or subtitle state.

## 0.4.4

- Fixed SyncNet candidate evaluation against current `syncnet-python` releases: media-manager now extracts each candidate's selected audio to an explicit 16 kHz mono WAV and passes `audio_path` rather than relying on the package's documented-but-broken `audio_path=None` behavior.
- Bumped the A/V algorithm version so prior 64/64 `NoneType` candidate failures are automatically reconsidered without invalidating crop or subtitle work.
- Added regression coverage ensuring SyncNet always receives a real, non-empty audio file.

## 0.4.3

- Make SyncNet candidate search adaptive: start with the best distributed clips and expand up to 64 candidates when fewer than three high-confidence observations are found.
- Generate multiple overlapping candidates from long speech regions instead of testing only the segment center.
- Preserve VAD speech checkpoints across this A/V algorithm upgrade when the source file is unchanged.
- Record raw SyncNet diagnostics (tested/usable/high-confidence count, threshold, best/median/p90 confidence, and failure reasons) and surface them in `explain`.
- Keep the automatic-correction confidence threshold at 4.5; this release improves evidence collection rather than weakening the safety threshold.

## 0.4.2

- Removed the TorchCodec requirement from the Silero VAD path. FFmpeg now streams the authoritative audio as mono 16 kHz float PCM directly into Silero's streaming iterator instead of calling `silero_vad.read_audio()` / `torchaudio.load()`.
- Stream speech detection in 32 ms chunks, avoiding a multi-hour decoded soundtrack being held in memory.
- Added live percentage progress during the Silero speech scan.
- If Silero fails, record the reason and fall back to FFmpeg non-silence candidate discovery instead of failing the entire A/V stage.
- Bumped the A/V algorithm version to selectively retry old TorchCodec-related review results without invalidating crop or subtitle stages.
- Added regressions ensuring the VAD path works without a `read_audio` symbol and old A/V results are requeued.

## 0.4.1

- Fixed a v0.4.0 A/V review-state bug that caused a completed checkpointed A/V analysis to be retried on every `resume` after correction validation failed.
- Preserve completed SyncNet observations/median/spread/drift in the review-stage record when automatic correction is withheld.
- Show the concrete correction error in completion summaries and `explain` instead of only `A/V processing failed after checkpointed analysis`.
- Migrate affected v0.4.0 review records from their existing checkpoints without rerunning SyncNet.

## 0.4.0

- Replaced crop-size heuristics with a native-pixel evidence mask: genuine permanent 1–2 px borders are distinguished from detector jitter by direct full-resolution luma evidence rather than an arbitrary minimum crop threshold.
- Full-runtime 1 fps crop baseline and 5 fps transition refinement now derive geometry from source-resolution pixels; black/fade frames remain excluded.
- Added raw, normalized, and persistent/meaningful crop-state evidence. `variable=true` now reflects persistent geometry changes instead of tiny transient state jitter.
- Added codec/chroma crop-unit reporting; verified borders are rounded down only at the final H.264 metadata step.
- Increased default visual classification to 96 distributed frames, speech/face candidate pool to 48 windows, and SyncNet target to 20 distributed candidates.
- Visual type is informational rather than a SyncNet gate. Animation/mixed media can be analyzed; recommended corrections remain review-only by default.
- Improved lightweight face hinting with frontal + profile detection at higher temporal/spatial sampling; SyncNet/S3FD remains the actual speaking-face authority.
- Added large ffsubsync adjustment severity (`PASS`, `PASS_WITH_LARGE_ADJUSTMENT`, `NEEDS_REVIEW`), common 25↔23.976 ratio recognition, and beginning/middle/late audio-activity validation.
- Added per-stage algorithm version invalidation. Upgrading crop/A/V/subtitle logic queues only the affected stage and preserves unrelated completed work.
- A real A/V timeline correction now invalidates subtitle synchronization/final verification while preserving crop state.
- Outdated pre-upgrade commit journals are discarded instead of landing an old algorithm's transformation after upgrade.
- Added `media-manager retry --stage STAGE [PATH]` with stage-checkpoint reset.
- Added `media-manager progress` and `media-manager progress --watch`; `status --stages` now includes active file/checkpoint percentages.
- Improved one-line live crop progress with in-chunk native-frame progress and checkpoint/sample information while throttling SQLite UI writes.
- Added secure `media-manager config opensubtitles` prompting and warnings for API keys passed on command-line flags; run-history commands redact API-key values.
- Improved per-file completion/review summaries.
- Expanded regression suite for real 2-pixel borders, brief full-frame states, stage-version requeueing, timing-conversion recognition, and secret redaction.

## 0.3.0

- Added a continuously refreshed single-line TTY progress UI with current file, stage, stage number, bar/spinner, elapsed time, and operation details.
- Added `--no-progress` for non-interactive/logging workflows.
- Increased visual classification to at least 72 distributed frames.
- Unknown visual classification no longer blocks A/V evidence gathering; confidently animated content remains protected from automatic lip shifting.
- Expanded speech/face candidate discovery to up to 36 windows and SyncNet evaluation to at least the best 16 distributed candidates.
- Automatically retries legacy `insufficient_evidence: Visual type is unknown` A/V results from v0.2 while preserving unrelated crop/subtitle checkpoints.
- Rebinds existing crop checkpoints after a verified A/V stream-copy remux, because the video pixels are unchanged; a new A/V correction therefore does not throw away an in-progress full-resolution crop scan.
- Added progress callbacks for visual sampling, speech selection, face scoring, SyncNet evaluation, full-resolution crop scanning, transition refinement, crop verification, subtitles, and final verification.

## 0.2.0

- Added persistent intra-stage checkpoints and explicit pause/resume support.
- Added `media-manager pause` and `media-manager resume`.
- Added SQLite run history and recovery of abandoned RUNNING stages.
- Full-resolution crop scans checkpoint in one-minute chunks by default.
- SyncNet candidate scoring/evaluation is checkpointed incrementally.
- OpenSubtitles downloads survive interruption and are reused on resume.
- Added atomic media commit journals to prevent double-applying a crop/A/V shift after a crash at the replacement boundary.
- Added a per-library advisory lock to prevent concurrent mutating runs.
- Preserved incomplete work directories by default for reentrance.

## 0.1.0

- Per-library `.media-manager/` config and SQLite state.
- Recursive Plex-style Movies / TV Shows scanning and identity validation.
- Quick fingerprints, external-change invalidation, historical missing records, and move reconnection.
- Whole-runtime 1-fps variable-aspect crop analysis at native displayed resolution, with fade/black filtering, transition refinement, independent full-resolution boundary verification, and H.264 metadata crop application without video re-encoding.
- Conservative visual live-action/animation heuristic with manual override.
- Optional CPU SyncNet A/V analysis using speech/face candidate selection, robust offset consensus, drift detection, and sign-verified stream-copy correction.
- Existing/embedded/OpenSubtitles subtitle discovery with strict TV episode validation and language-specific reference audio selection.
- ffsubsync multi-segment integration and subtitle sanity validation.
- Transactional work files, disk-space checks, post-remux ffprobe verification, and atomic source replacement.
- Review/retry/explain/status/doctor CLI workflows.
- Synchformer backend reserved for a future release.
