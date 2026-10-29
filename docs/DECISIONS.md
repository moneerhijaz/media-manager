# Design decisions visible in code

- **Verify before replacing:** executor candidate checks and commit journaling put explicit gates between analysis and file mutation. Atomic replacement protects the rename boundary, not every storage failure.
- **Keep video stream-copy for A/V correction:** correction avoids a video re-encode; offset verification still needs sufficient speech/face evidence. Unsupported drift is not silently stretched.
- **Retain uncertain outcomes:** unavailable/insufficient evidence remains visible in review and stage state. Missing detector/model evidence is not a successful measurement.
- **Persist stages and checkpoints separately:** SQLite supports queries and review; checkpoint files resume expensive intra-stage analysis and use source signatures to detect stale work.
- **Conservative crop analysis:** native-frame and variable-aspect checks avoid treating briefly occupied pixels as permanent borders. FFmpeg integration tests exercise synthetic boundary cases.

These describe implementation choices, not a guarantee that all media or model predictions are correct.
