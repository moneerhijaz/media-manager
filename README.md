# Media Manager

A Linux CLI and authenticated web dashboard for checking and repairing Plex-style media libraries. Version **0.6.0** tracks completed work and resumes interrupted processing.

- Detects black borders and applies H.264 crop metadata without re-encoding.
- Checks A/V lip sync with optional SyncNet models and validates constant-offset corrections before applying them.
- Finds local, embedded, or OpenSubtitles subtitles and aligns them with ffsubsync.
- Provides progress, review details, history, and selective retries.

Processing can replace media and subtitles after verification. Keep backups and inspect `media-manager plan` before running.

## Docker

From this repository:

```bash
docker compose up --build -d
```

Open `http://SERVER-IP:8787` and complete setup. Keep first-time setup on a trusted network; use Tailscale or HTTPS for remote access. Set `MEDIA_MANAGER_BIND=127.0.0.1` for host-only access.

### Multiple libraries in one dashboard

There is no primary media library. The named Docker volume `media-manager-state` stores dashboard settings, authentication, the library registry, and shared language-model cache under `/state`. Each media library keeps its own processing state.

1. Fill in credentials in your private `.env` using `.env.example` as a reference. Set `MEDIA_MANAGER_UID` and `MEDIA_MANAGER_GID` to the output of `id -u` and `id -g` for a user with access to all media mounts. Keep existing secrets; do not overwrite an existing `.env` with the template.
2. Create or edit the Git-ignored `compose.override.yaml`. Mount media disks or dedicated parent folders once, and list the library roots inside them using **container paths**:

   ```yaml
   services:
     media-manager:
       environment:
         MEDIA_LIBRARIES: '["/media/disk2/Archive", "/media/disk2/Family"]'
       volumes:
         - /mnt/your-second-media-disk:/media/disk2
   ```

   Replace the example paths with existing directories. Every library is an equal entry; no `MEDIA_LIBRARY` variable is required. You can alternatively use the separate-library mounts in `compose.multi-library.example.yaml`. With no media mounts, the dashboard can still start and complete authentication setup.

3. Pause any active processing in the dashboard, then apply the configuration from this repository:

   ```bash
   docker compose up --build -d
   ```

   Compose recreates the container when mount/environment settings change. `docker compose restart` alone does **not** apply new mounts.

4. Sign in to the dashboard and select a configured root marked **add library** in the Library selector. Confirm initialization, then configure its movie/TV subfolders in Settings. Registration is saved in the app-state volume and survives container recreation. Use **Dashboard / default view** for shared settings/authentication and combined status.

After signing in, use the Library selector to add mounted roots, switch libraries, or select **All libraries** for combined status and a sequential run of all registered libraries. Each root retains its own `.media-manager` configuration, database, checkpoints, and logs. Configure each library's movie/TV subfolders through its Settings. Pause stops the active run and cancels the remaining queue; queued work is not automatically resumed after a container restart.

Only configured roots can be added; overlapping roots are rejected. All mounts must be writable by the configured container user (default UID/GID 1000). The dashboard token is shared. Do not use `docker compose down -v` unless you intend to delete the app-state volume.

**Upgrading from the primary-library layout:** remove `MEDIA_LIBRARY` from `.env` and explicitly include that library's host mount and container path in your override. Existing `.media-manager` databases/checkpoints are reused; configured libraries with existing state are imported into the new registry on first startup. The old per-library dashboard token is left untouched, but the new app-state volume requires one-time dashboard authentication setup. No media files are moved or processed by this migration.

**Current limitation:** the dashboard lists roots from `MEDIA_LIBRARIES`; a folder browser for discovering arbitrary subfolders inside mounted parents is not implemented yet. To expose another library under an existing parent mount, update that list and repeat step 3. A new disk outside the mounted parents also needs a new bind mount. Do not mount `/` or the Docker socket to let the dashboard change its own volumes; keep container recreation under host-side control.

## CLI

Requires Python 3.11+, FFmpeg/ffprobe, and pipx. From this repository:

```bash
pipx install '.[avsync,languages]'
cd /path/to/Media
media-manager init
media-manager setup-syncnet
media-manager setup-languages     # Cache the local speech-language model
media-manager config opensubtitles  # Optional subtitle downloads
media-manager doctor
media-manager plan
media-manager run
```

For an installation without SyncNet, use `pipx install .`, skip model setup, and run with `--no-av-sync`.

## Common commands

Run from your media-library root:

```bash
media-manager resume              # Continue unfinished work
media-manager pause               # Pause from another terminal
media-manager progress --watch    # Monitor processing
media-manager review              # Show unresolved issues
media-manager rerun-reviewed      # Retry reviewed stages
media-manager web                 # Dashboard on port 8787
media-manager --help
```

Defaults scan `.mp4` files under `Movies/` and `TV Shows/`. Configuration, credentials, models, and checkpoints live in the library's private `.media-manager/` folder. Do not publish that folder.

## Limitations

Lossless cropping supports H.264. A/V verification needs suitable speaking-face evidence and may be inconclusive for animation or dubs. Audio speed/drift correction and content filtering are not implemented. Subtitle availability and alignment are not guaranteed.

Credible drift up to ±200 ms/hour is accepted without stretching audio. Unsuitable subtitles wait seven days between searches; provider errors back off for 24 hours. Use `resume --retry-subtitles-now` to bypass scheduling, not validation.

Put `TMDB_READ_TOKEN='your-token'` in the private, Git-ignored `.env` for guarded movie identity, original-language metadata, and regional digital-release dates (default region: US). The CLI automatically reads it, including from the original project directory for local pip/pipx installs launched elsewhere. Library-local `.env` files take priority over the project file; exported environment variables override both. No shell sourcing is required. Missing metadata or language models leave A/V explicitly unverified. Track preferences are recorded without dropping or rewriting audio tracks.

Optional daily retry templates are in `deploy/`. Edit the service paths and install/enable the user timer yourself; it runs only eligible waiting subtitles. No scheduler is enabled automatically.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
```

MIT license. See [LICENSE](LICENSE).
