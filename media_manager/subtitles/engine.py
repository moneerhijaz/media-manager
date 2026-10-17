from __future__ import annotations

import difflib
import getpass
import gzip
import hashlib
import io
import json
import math
import os
import re
import shutil
import struct
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from ..models import MediaType, StageStatus, SubtitleCandidate
from ..probe import audio_role_text, probe_media, stream_language
from ..utils import MediaManagerError, require_tool, run

APP_NAME = "MediaManager"
APP_VERSION = __version__
SUBTITLE_ALGORITHM_VERSION = "subtitle-v7-structural-recovery"
SRT_RUNTIME_TOLERANCE_SECONDS = 120.0
SRT_BACKWARD_JUMP_TOLERANCE_SECONDS = 10.0


class QuotaExhausted(MediaManagerError):
    pass


class AuthenticationFailed(MediaManagerError):
    pass


def normalize_name(text: str) -> str:
    text = re.sub(r"[\[\]\(\)\{\}]", " ", text)
    text = re.sub(r"[._]+", " ", text)
    text = re.sub(r"\s*-\s*", " ", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def similarity(a: str, b: str) -> float:
    aa = re.sub(r"[^a-z0-9]+", " ", normalize_name(a)).strip()
    bb = re.sub(r"[^a-z0-9]+", " ", normalize_name(b)).strip()
    if not aa or not bb:
        return 0.0
    return difflib.SequenceMatcher(None, aa, bb).ratio()


def opensubtitles_hash(path: Path) -> tuple[str, int]:
    size = path.stat().st_size
    chunk = 65536
    if size < chunk * 2:
        raise MediaManagerError("File too small for OpenSubtitles hash")
    checksum = size & 0xFFFFFFFFFFFFFFFF
    with path.open("rb") as fh:
        first = fh.read(chunk)
        fh.seek(-chunk, os.SEEK_END)
        last = fh.read(chunk)
    for part in (first, last):
        checksum = (checksum + sum(struct.unpack("<8192Q", part))) & 0xFFFFFFFFFFFFFFFF
    return f"{checksum:016x}", size


class OpenSubtitlesClient:
    def __init__(
        self,
        cfg: dict,
        *,
        password: str | None = None,
        password_prompt: Callable[[str], str] | None = None,
        verbose: bool = False,
    ):
        self.api_key = str(cfg.get("api_key") or "")
        self.username = str(cfg.get("username") or "")
        self.api_root = str(cfg.get("api_root") or "https://api.opensubtitles.com/api/v1").rstrip("/")
        env_password = os.environ.get("OPENSUBTITLES_PASSWORD")
        self.password = env_password if env_password is not None else password
        self.password_prompt = password_prompt
        self.token = os.environ.get("OPENSUBTITLES_TOKEN")
        self.verbose = verbose
        self.login_error: Exception | None = None
        self.remaining_downloads: int | None = None

    @property
    def user_agent(self) -> str:
        return f"{APP_NAME} v{APP_VERSION}"

    def configured(self) -> bool:
        return bool(self.api_key and self.username)

    def _headers(self, auth: bool = False) -> dict[str, str]:
        h = {"Api-Key": self.api_key, "User-Agent": self.user_agent, "Accept": "application/json"}
        if auth:
            if not self.token:
                self.login()
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _request(self, method: str, endpoint: str, *, params=None, body=None, auth=False) -> Any:
        url = f"{self.api_root}/{endpoint.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        headers = self._headers(auth)
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            msg = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(msg)
                detail = payload.get("message") or payload.get("error") or msg
            except Exception:
                detail = msg
            if exc.code == 429:
                raise QuotaExhausted(f"OpenSubtitles HTTP 429: {detail}") from exc
            raise MediaManagerError(f"OpenSubtitles HTTP {exc.code} on {endpoint}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise MediaManagerError(f"OpenSubtitles network error: {exc.reason}") from exc
        return json.loads(raw.decode("utf-8"))

    def login(self) -> None:
        if self.token:
            return
        if self.login_error:
            raise AuthenticationFailed(str(self.login_error))
        if not self.username or not self.api_key:
            raise AuthenticationFailed("OpenSubtitles username/API key not configured")
        if self.password is None:
            if not os.isatty(0):
                raise AuthenticationFailed("Set OPENSUBTITLES_PASSWORD for unattended runs")
            prompt = self.password_prompt or getpass.getpass
            self.password = prompt("OpenSubtitles password: ")
        try:
            payload = self._request(
                "POST", "/login", body={"username": self.username, "password": self.password}, auth=False
            )
            self.token = payload.get("token")
            if not self.token:
                raise AuthenticationFailed("OpenSubtitles login returned no token")
            base_url = payload.get("base_url")
            if base_url:
                base = str(base_url).rstrip("/")
                if not base.startswith(("http://", "https://")):
                    base = "https://" + base
                if not base.endswith("/api/v1"):
                    base += "/api/v1"
                self.api_root = base
        except Exception as exc:
            self.login_error = exc
            raise

    def search(self, *, language: str, media_type: str, moviehash=None, moviebytesize=None,
               query=None, year=None, season=None, episode=None, hearing_impaired="exclude") -> list[dict]:
        params: dict[str, Any] = {
            "languages": language,
            "type": media_type,
            "order_by": "download_count",
            "order_direction": "desc",
            "hearing_impaired": hearing_impaired,
        }
        if season is not None:
            params["season_number"] = season
        if episode is not None:
            params["episode_number"] = episode
        if moviehash:
            params["moviehash"] = moviehash
            params["moviebytesize"] = moviebytesize
        else:
            params["query"] = query
            if media_type == "movie":
                params["year"] = year
        payload = self._request("GET", "/subtitles", params=params)
        data = payload.get("data") or []
        return data if isinstance(data, list) else []

    def download(self, file_id: int) -> tuple[bytes, str | None, int | None]:
        payload = self._request("POST", "/download", body={"file_id": file_id, "sub_format": "srt"}, auth=True)
        link = payload.get("link")
        remaining = payload.get("remaining")
        if remaining is not None:
            try:
                self.remaining_downloads = int(remaining)
            except (TypeError, ValueError):
                pass
        if not link:
            if self.remaining_downloads == 0:
                raise QuotaExhausted("OpenSubtitles daily download quota exhausted")
            raise MediaManagerError(f"OpenSubtitles returned no download link: {payload.get('message')}")
        req = urllib.request.Request(str(link), headers={"User-Agent": self.user_agent})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
        return raw, payload.get("file_name"), self.remaining_downloads


def _int(v: object) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def choose_candidate(results: list[dict], row, video: Path) -> SubtitleCandidate | None:
    candidates: list[SubtitleCandidate] = []
    is_episode = row["media_type"] == MediaType.EPISODE.value
    requested_title = row["show_title"] if is_episode else row["title"]
    for result in results:
        a = result.get("attributes") or {}
        files = a.get("files") or []
        if not files:
            continue
        fi = files[0] or {}
        if fi.get("file_id") is None:
            continue
        feature = a.get("feature_details") or {}
        ftype = str(feature.get("feature_type") or "") or None
        season = _int(feature.get("season_number"))
        episode = _int(feature.get("episode_number"))
        parent = str(feature.get("parent_title") or "") or None
        title = feature.get("movie_name") or feature.get("title") or feature.get("original_title")
        year = _int(feature.get("year"))
        release = str(a.get("release") or fi.get("file_name") or "")
        hash_match = bool(a.get("moviehash_match"))
        trusted = bool(a.get("from_trusted"))
        if is_episode:
            if ftype and ftype.casefold() != "episode":
                continue
            if season != row["season"] or episode != row["episode"]:
                continue
            show_sim = max(similarity(str(requested_title), parent or ""), similarity(str(requested_title), release))
            if show_sim < (0.35 if hash_match else 0.55):
                continue
            sim = max(show_sim, similarity(video.stem, release))
        else:
            if ftype and ftype.casefold() == "episode":
                continue
            sim = max(similarity(video.stem, release), similarity(str(requested_title), str(title or "")), similarity(str(requested_title), release))
        downloads = int(a.get("download_count") or a.get("new_download_count") or 0)
        rating = float(a.get("ratings") or 0)
        score = sim * 100 + min(math.log10(downloads + 1) * 5, 30) + rating * 3
        if trusted:
            score += 12
        if hash_match:
            score += 1000
        if not is_episode and row["year"] and year:
            score += 35 if int(row["year"]) == year else -75
        candidates.append(SubtitleCandidate(
            int(fi["file_id"]), str(fi.get("file_name") or ""), str(a.get("language") or ""),
            release, score, hash_match, trusted, bool(a.get("hearing_impaired")),
            str(title) if title else None, year, ftype, season, episode, parent,
        ))
    return max(candidates, key=lambda x: x.score) if candidates else None


def _unpack(raw: bytes, preferred: str | None = None) -> bytes:
    if raw.startswith(b"\x1f\x8b"):
        return gzip.decompress(raw)
    if raw.startswith(b"PK\x03\x04"):
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            if not names:
                raise MediaManagerError("Downloaded subtitle ZIP was empty")
            names.sort(key=lambda n: (Path(n).suffix.casefold() != ".srt", 0 if preferred and Path(n).name.casefold() == Path(preferred).name.casefold() else 1))
            return z.read(names[0])
    return raw


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _candidate_summary(choice: SubtitleCandidate) -> dict[str, Any]:
    return {
        "file_id": choice.file_id,
        "file_name": choice.file_name,
        "release": choice.release,
        "hash_match": choice.hash_match,
        "trusted": choice.trusted,
        "score": choice.score,
    }


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json_object(path: Path, value: dict[str, Any]) -> None:
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def _cached_candidate_metadata_path(work: Path, language: str) -> Path:
    return work / f"downloaded.{language}.json"


def _rejected_candidates_path(work: Path, language: str) -> Path:
    return work / f"rejected.{language}.json"


def _write_cached_candidate_metadata(
    work: Path, language: str, choice: SubtitleCandidate, source: Path
) -> None:
    value = _candidate_summary(choice)
    value["sha256"] = _sha256_file(source)
    _write_json_object(_cached_candidate_metadata_path(work, language), value)


def _load_rejected_candidates(work: Path, language: str) -> tuple[set[int], set[str]]:
    payload = _read_json_object(_rejected_candidates_path(work, language))
    file_ids: set[int] = set()
    hashes: set[str] = set()
    for item in payload.get("rejections", []):
        if not isinstance(item, dict):
            continue
        try:
            if item.get("file_id") is not None:
                file_ids.add(int(item["file_id"]))
        except (TypeError, ValueError):
            pass
        sha256 = item.get("sha256")
        if isinstance(sha256, str) and sha256:
            hashes.add(sha256)
    return file_ids, hashes


def _quarantine_provider_candidate(
    source: Path,
    work: Path,
    language: str,
    choice: SubtitleCandidate | None,
    reason: str,
) -> dict[str, Any]:
    """Preserve a rejected provider file while preventing automatic reuse."""
    sha256 = _sha256_file(source)
    cached_meta_path = _cached_candidate_metadata_path(work, language)
    cached_meta = _read_json_object(cached_meta_path)
    summary = _candidate_summary(choice) if choice is not None else cached_meta
    file_id = summary.get("file_id")
    try:
        file_id = int(file_id) if file_id is not None else None
    except (TypeError, ValueError):
        file_id = None

    rejected_path = work / f"rejected.{language}.{sha256[:12]}.srt"
    if source.parent == work:
        if rejected_path.exists():
            if source != rejected_path:
                source.unlink(missing_ok=True)
        elif source != rejected_path:
            source.replace(rejected_path)
    else:
        shutil.copyfile(source, rejected_path)

    payload_path = _rejected_candidates_path(work, language)
    payload = _read_json_object(payload_path)
    rejections = payload.get("rejections")
    if not isinstance(rejections, list):
        rejections = []
    entry = {
        "sha256": sha256,
        "file_id": file_id,
        "file_name": summary.get("file_name"),
        "release": summary.get("release"),
        "reason": reason,
        "path": str(rejected_path),
    }
    rejections = [
        item for item in rejections
        if not isinstance(item, dict)
        or not (
            item.get("sha256") == sha256
            or (file_id is not None and item.get("file_id") == file_id)
        )
    ]
    rejections.append(entry)
    _write_json_object(payload_path, {"version": 1, "rejections": rejections})
    cached_meta_path.unlink(missing_ok=True)
    return entry


def _exclude_provider_results(results: list[dict], file_ids: set[int]) -> list[dict]:
    if not file_ids:
        return results
    filtered: list[dict] = []
    for result in results:
        files = (result.get("attributes") or {}).get("files") or []
        candidate_ids: set[int] = set()
        for item in files:
            try:
                candidate_ids.add(int((item or {}).get("file_id")))
            except (TypeError, ValueError):
                continue
        if not candidate_ids.intersection(file_ids):
            filtered.append(result)
    return filtered



def _language_aliases(language: str) -> set[str]:
    table = {
        "en": {"en", "eng"}, "eng": {"en", "eng"},
        "ar": {"ar", "ara"}, "ara": {"ar", "ara"},
        "es": {"es", "spa"}, "spa": {"es", "spa"},
        "fr": {"fr", "fra", "fre"}, "de": {"de", "deu", "ger"},
    }
    return table.get(language.casefold(), {language.casefold()})


def _reference_audio_index(video: Path, language: str) -> tuple[int | None, str]:
    probe = probe_media(video)
    aliases = _language_aliases(language)
    matches = []
    for i, stream in enumerate(probe.audio_streams):
        role = audio_role_text(stream)
        if any(x in role for x in ("commentary", "description", "descriptive")):
            continue
        if stream_language(stream) in aliases:
            matches.append((i, int((stream.get("disposition") or {}).get("default", 0))))
    if matches:
        matches.sort(key=lambda x: x[1], reverse=True)
        return matches[0][0], "matching_audio_language"
    # Translated subtitles without a matching dub should follow the authoritative
    # English/original timeline when available.
    english = _language_aliases("en")
    for i, stream in enumerate(probe.audio_streams):
        role = audio_role_text(stream)
        if any(x in role for x in ("commentary", "description", "descriptive")):
            continue
        if stream_language(stream) in english:
            return i, "fallback_english_audio"
    if len(probe.audio_streams) == 1:
        return 0, "only_audio_stream"
    return None, "no_unambiguous_reference_audio"


def _extract_embedded_text_subtitle(video: Path, language: str, work: Path) -> tuple[Path | None, dict[str, Any] | None]:
    probe = probe_media(video)
    aliases = _language_aliases(language)
    text_codecs = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
    choices = []
    for relative_index, stream in enumerate(probe.subtitle_streams):
        if stream_language(stream) not in aliases:
            continue
        if str(stream.get("codec_name") or "").casefold() not in text_codecs:
            continue
        forced = int((stream.get("disposition") or {}).get("forced", 0))
        choices.append((forced, relative_index, stream))
    if not choices:
        return None, None
    choices.sort(key=lambda x: x[0])  # prefer a full track over forced-only
    forced, relative_index, stream = choices[0]
    output = work / f"embedded.{language}.srt"
    ffmpeg = require_tool("ffmpeg")
    proc = run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
        "-map", f"0:s:{relative_index}", "-c:s", "srt", str(output),
    ], check=False)
    if proc.returncode != 0 or not output.exists() or output.stat().st_size == 0:
        return None, None
    return output, {
        "stream_relative_index": relative_index,
        "codec": stream.get("codec_name"),
        "forced": bool(forced),
    }

def _find_existing(video: Path, language: str, plain_language: str) -> Path | None:
    lang = video.with_name(f"{video.stem}.{language}.srt")
    if lang.exists():
        return lang
    plain = video.with_suffix(".srt")
    if language == plain_language and plain.exists():
        return plain
    return None


def _run_ffsubsync(
    video: Path,
    source_srt: Path,
    output_srt: Path,
    cfg: dict,
    reference_audio_index: int | None,
    *,
    candidate_validator: Callable[[Path], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    binary = shutil.which("ffsubsync") or shutil.which("ffs")
    if not binary:
        raise MediaManagerError("ffsubsync/ffs not found in PATH")
    base_cmd = [binary, str(video), "-i", str(source_srt), "-o", str(output_srt)]
    if reference_audio_index is not None:
        base_cmd += ["--reference-stream", f"a:{reference_audio_index}"]
    # Ordinary full-runtime ffsubsync is the trusted primary path. Sparse
    # multi-segment analysis remains available only as a fallback when the
    # primary attempt rejects itself or produces an implausible SRT.
    help_proc = run([binary, "--help"], check=False)
    help_text = (help_proc.stdout or "") + (help_proc.stderr or "")
    multi_args: list[str] = []
    if cfg.get("multi_segment_sync", True) and "--multi-segment-sync" in help_text:
        multi_args = [
            "--multi-segment-sync", "--segment-count", str(int(cfg.get("segment_count", 8))),
            "--parallel-workers", "1", "--skip-intro-outro",
        ]
    safety_args: list[str] = []
    if "--skip-sync-on-low-quality" in help_text:
        safety_args = ["--skip-sync-on-low-quality"]

    def invoke(extra_args: list[str]) -> dict[str, Any]:
        output_srt.unlink(missing_ok=True)
        proc = run(base_cmd + extra_args + safety_args, check=False)
        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if proc.returncode != 0 or not output_srt.exists() or output_srt.stat().st_size == 0:
            raise MediaManagerError(f"ffsubsync failed: {combined[-3000:]}")

        def find_float(pattern: str):
            match = re.search(pattern, combined, re.I)
            return float(match.group(1)) if match else None

        score = find_float(r"score:\s*([-+0-9.eE]+)")
        offset = find_float(r"offset seconds:\s*([-+0-9.eE]+)")
        scale = find_float(r"framerate scale factor:\s*([-+0-9.eE]+)")
        skipped = len(re.findall(r"Skipped subtitle", combined))
        # Rich may wrap this warning and insert its source-location column in
        # the middle of both the parenthesized reason and the final phrase.
        low_quality_match = re.search(
            r"low-quality alignment\s*\((.*?)\)\s*;\s*leaving\s+subtitles\s+unmodified",
            combined,
            re.I | re.S,
        )
        low_quality_reason = None
        if low_quality_match:
            low_quality_reason = re.sub(
                r"(?:\S*/)?ffsubsync\.py:\d+", " ", low_quality_match.group(1), flags=re.I,
            )
            low_quality_reason = re.sub(r"\s+", " ", low_quality_reason).strip()
        quality_reasons = [part.strip() for part in (low_quality_reason or "").split(";") if part.strip()]
        if not low_quality_reason:
            if score is not None and score < float(cfg.get("ffsubsync_min_score", 0.0)):
                raise MediaManagerError(f"ffsubsync score {score} below configured minimum")
            if offset is not None and abs(offset) > float(cfg.get("max_abs_offset_seconds", 600.0)):
                raise MediaManagerError(f"ffsubsync offset {offset}s is implausibly large")
            if scale is not None and not (float(cfg.get("min_scale", 0.90)) <= scale <= float(cfg.get("max_scale", 1.10))):
                raise MediaManagerError(f"ffsubsync scale factor {scale} outside accepted range")
        result = {
            "score": score,
            "offset_seconds": offset,
            "scale": scale,
            "skipped_cues": skipped,
            "applied": not bool(low_quality_reason),
            "low_quality": bool(low_quality_reason),
            "low_quality_reason": low_quality_reason,
            "sync_applied": not bool(low_quality_reason),
            "quality_rejected": bool(low_quality_reason),
            "quality_reasons": quality_reasons,
        }
        if candidate_validator is not None and not low_quality_reason:
            try:
                result["candidate_validation"] = candidate_validator(output_srt)
                result["candidate_validation_error"] = None
            except MediaManagerError as exc:
                result["candidate_validation"] = None
                result["candidate_validation_error"] = str(exc)
        return result

    result = invoke([])
    result["method"] = "full_runtime"
    result["multi_segment_fallback_attempted"] = False
    result["full_runtime_fallback_attempted"] = False  # legacy diagnostic key
    fallback_reason = None
    if result["low_quality"]:
        fallback_reason = "full-runtime alignment was rejected as low quality"
    elif result.get("candidate_validation_error"):
        fallback_reason = result["candidate_validation_error"]
    if not multi_args or fallback_reason is None:
        return result

    full_runtime_attempt = dict(result)
    try:
        fallback = invoke(multi_args)
    except MediaManagerError as exc:
        shutil.copyfile(source_srt, output_srt)
        result["multi_segment_fallback_attempted"] = True
        result["multi_segment_fallback_reason"] = fallback_reason
        result["multi_segment_fallback_error"] = str(exc)
        return result
    fallback["method"] = "multi_segment_fallback"
    fallback["multi_segment_fallback_attempted"] = True
    fallback["multi_segment_fallback_reason"] = fallback_reason
    fallback["full_runtime_attempt"] = full_runtime_attempt
    return fallback


def _inspect_srt(
    path: Path,
    duration: float,
    *,
    backward_jump_tolerance: float = SRT_BACKWARD_JUMP_TOLERANCE_SECONDS,
) -> dict[str, Any]:
    data = path.read_text(encoding="utf-8", errors="replace")
    cues = len(re.findall(r"(?m)^\d+\s*$", data))
    timestamps = re.findall(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s+-->\s+(\d{2}):(\d{2}):(\d{2})[,.](\d{3})", data)
    starts = []
    ends = []
    for ts in timestamps:
        values = list(map(int, ts))
        starts.append(values[0] * 3600 + values[1] * 60 + values[2] + values[3] / 1000)
        ends.append(values[4] * 3600 + values[5] * 60 + values[6] + values[7] / 1000)
    last = max(ends) if ends else 0
    invalid_duration_count = sum(end <= start for start, end in zip(starts, ends))
    backward_jumps = [
        {
            "after_timestamp": index,
            "from_seconds": round(previous, 3),
            "to_seconds": round(current, 3),
            "jump_seconds": round(previous - current, 3),
        }
        for index, (previous, current) in enumerate(zip(starts, starts[1:]), start=1)
        if current < previous - backward_jump_tolerance
    ]
    largest_backward_jump = max(
        (float(item["jump_seconds"]) for item in backward_jumps), default=0.0
    )
    structural_error = None
    if invalid_duration_count:
        structural_error = f"Subtitle has {invalid_duration_count} cue(s) whose end is not after its start"
    elif backward_jumps:
        structural_error = (
            "Subtitle timeline jumps backward "
            f"{largest_backward_jump:.1f}s; it may contain concatenated or duplicated releases"
        )
    error = None
    if structural_error:
        error = structural_error
    elif cues < 3 or len(timestamps) < 3:
        error = "Subtitle has too few valid cues"
    elif last > duration + SRT_RUNTIME_TOLERANCE_SECONDS:
        error = f"Subtitle extends {last-duration:.1f}s beyond media duration"
    return {
        "cue_count": cues,
        "timestamp_count": len(timestamps),
        "last_cue_seconds": last,
        "media_duration_seconds": duration,
        "overrun_seconds": round(last - duration, 3),
        "invalid_duration_count": invalid_duration_count,
        "backward_jump_count": len(backward_jumps),
        "largest_backward_jump_seconds": round(largest_backward_jump, 3),
        "backward_jumps": backward_jumps[:10],
        "structural_error": structural_error,
        "valid": error is None,
        "error": error,
    }


def _validate_srt(path: Path, duration: float) -> dict[str, Any]:
    inspection = _inspect_srt(path, duration)
    if inspection["error"]:
        raise MediaManagerError(str(inspection["error"]))
    return inspection




def _parse_srt_intervals(path: Path) -> list[tuple[float, float]]:
    data = path.read_text(encoding="utf-8", errors="replace")
    out: list[tuple[float, float]] = []
    pat = re.compile(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s+-->\s+(\d{2}):(\d{2}):(\d{2})[,.](\d{3})")
    for m in pat.finditer(data):
        x = list(map(int, m.groups()))
        a = x[0] * 3600 + x[1] * 60 + x[2] + x[3] / 1000.0
        b = x[4] * 3600 + x[5] * 60 + x[6] + x[7] / 1000.0
        if b > a:
            out.append((a, b))
    return out


def _audio_activity_segments(video: Path, audio_index: int, duration: float) -> list[tuple[float, float]]:
    ffmpeg = require_tool("ffmpeg")
    proc = run([
        ffmpeg, "-hide_banner", "-loglevel", "info", "-i", str(video),
        "-map", f"0:a:{audio_index}", "-af", "silencedetect=noise=-35dB:d=0.35",
        "-f", "null", "/dev/null",
    ], check=False)
    silence_start = re.compile(r"silence_start:\s*([0-9.]+)")
    silence_end = re.compile(r"silence_end:\s*([0-9.]+)")
    silences: list[tuple[float, float]] = []
    pending: float | None = None
    for line in (proc.stderr or "").splitlines():
        m = silence_start.search(line)
        if m:
            pending = float(m.group(1))
        m = silence_end.search(line)
        if m and pending is not None:
            silences.append((pending, float(m.group(1))))
            pending = None
    if pending is not None:
        silences.append((pending, duration))
    active: list[tuple[float, float]] = []
    cur = 0.0
    for a, b in sorted(silences):
        if a > cur:
            active.append((cur, a))
        cur = max(cur, b)
    if cur < duration:
        active.append((cur, duration))
    return active


def _overlap_seconds(interval: tuple[float, float], regions: list[tuple[float, float]]) -> float:
    a, b = interval
    return sum(max(0.0, min(b, y) - max(a, x)) for x, y in regions)


def _regional_subtitle_validation(video: Path, srt: Path, audio_index: int, duration: float) -> dict[str, Any]:
    cues = _parse_srt_intervals(srt)
    active = _audio_activity_segments(video, audio_index, duration)
    regions = []
    for i in range(3):
        lo, hi = duration * i / 3.0, duration * (i + 1) / 3.0
        chosen = [(a, b) for a, b in cues if lo <= (a + b) / 2.0 < hi]
        total = sum(b - a for a, b in chosen)
        overlap = sum(_overlap_seconds((a, b), active) for a, b in chosen)
        regions.append({
            "region": ("beginning", "middle", "late")[i],
            "cue_count": len(chosen),
            "cue_seconds": round(total, 3),
            "audio_activity_overlap": round(overlap / total, 4) if total > 0 else None,
        })
    available = [r["audio_activity_overlap"] for r in regions if r["audio_activity_overlap"] is not None]
    return {"regions": regions, "minimum_overlap": min(available) if available else None, "method": "subtitle-vs-audio-activity"}


def _known_timing_conversion(scale: float | None, tolerance: float) -> str | None:
    if scale is None:
        return None
    pal_to_film = 25.0 / (24000.0 / 1001.0)
    film_to_pal = 1.0 / pal_to_film
    if abs(scale - pal_to_film) <= tolerance:
        return "probable 23.976 -> 25 fps timing conversion"
    if abs(scale - film_to_pal) <= tolerance:
        return "probable 25 -> 23.976 fps timing conversion"
    return None


def _subtitle_adjustment_severity(video: Path, srt: Path, sync_info: dict[str, Any], audio_index: int, duration: float, cfg: dict) -> tuple[str, dict[str, Any]]:
    offset = sync_info.get("offset_seconds")
    scale = sync_info.get("scale")
    large = (offset is not None and abs(float(offset)) >= float(cfg.get("large_offset_seconds", 15.0))) or (scale is not None and abs(float(scale) - 1.0) >= float(cfg.get("large_scale_delta", 0.015)))
    conversion = _known_timing_conversion(float(scale) if scale is not None else None, float(cfg.get("known_rate_ratio_tolerance", 0.003)))
    if not large:
        return "PASS", {"large_adjustment": False, "timing_conversion": conversion}
    regional = _regional_subtitle_validation(video, srt, audio_index, duration)
    minimum = regional.get("minimum_overlap")
    threshold = float(cfg.get("regional_activity_min_overlap", 0.25))
    enough_regions = sum(1 for r in regional["regions"] if r["cue_count"] >= 3) >= 2
    if minimum is not None and minimum >= threshold and enough_regions:
        return "PASS_WITH_LARGE_ADJUSTMENT", {
            "large_adjustment": True, "timing_conversion": conversion,
            "regional_validation": regional,
            "reason": conversion or "large ffsubsync adjustment passed beginning/middle/late audio-activity validation",
        }
    return "NEEDS_REVIEW", {
        "large_adjustment": True, "timing_conversion": conversion,
        "regional_validation": regional,
        "reason": "large ffsubsync adjustment lacks strong multi-region validation",
    }


def process_subtitle_language(
    video: Path,
    row,
    language: str,
    cfg: dict,
    client: OpenSubtitlesClient | None,
    work: Path,
    *,
    _forced_source: tuple[Path, str, SubtitleCandidate, int | None] | None = None,
    _recovery_context: dict[str, Any] | None = None,
) -> tuple[StageStatus, dict[str, Any], Path | None]:
    final = video.with_name(f"{video.stem}.{language}.srt")
    existing = _find_existing(video, language, str(cfg.get("plain_srt_language", "en"))) if cfg.get("prefer_existing", True) else None
    source = _forced_source[0] if _forced_source else existing
    source_kind = _forced_source[1] if _forced_source else ("existing" if existing else "opensubtitles")
    embedded_info = None
    if source is None and cfg.get("prefer_existing", True) and _forced_source is None:
        embedded, embedded_info = _extract_embedded_text_subtitle(video, language, work)
        if embedded is not None:
            source = embedded
            source_kind = "embedded"
    choice: SubtitleCandidate | None = _forced_source[2] if _forced_source else None
    remaining = _forced_source[3] if _forced_source else None
    cached_candidate_metadata: dict[str, Any] = {}

    # Preserve a successfully downloaded OpenSubtitles file across Ctrl+C,
    # SSH disconnects, or reboots. This is especially important because a
    # repeated download would consume quota even though ffsubsync had not yet
    # finished.
    cached_download = work / f"downloaded.{language}.srt"
    if source is None and _forced_source is None and cached_download.exists() and cached_download.stat().st_size > 0:
        source = cached_download
        source_kind = "opensubtitles_cached"
        cached_candidate_metadata = _read_json_object(
            _cached_candidate_metadata_path(work, language)
        )

    if source is None:
        if client is None or not client.configured():
            return StageStatus.UNAVAILABLE, {"reason": "OpenSubtitles is not configured and no existing subtitle was found"}, None
        try:
            h, size = opensubtitles_hash(video)
            rejected_file_ids, _ = _load_rejected_candidates(work, language)
            results = client.search(
                language=language, media_type=row["media_type"], moviehash=h, moviebytesize=size,
                season=row["season"], episode=row["episode"], hearing_impaired=str(cfg.get("hearing_impaired", "exclude")),
            )
            choice = choose_candidate(
                _exclude_provider_results(results, rejected_file_ids), row, video
            )
            if choice is None:
                query = row["show_title"] if row["media_type"] == "episode" else row["title"]
                results = client.search(
                    language=language, media_type=row["media_type"], query=query, year=row["year"],
                    season=row["season"], episode=row["episode"], hearing_impaired=str(cfg.get("hearing_impaired", "exclude")),
                )
                choice = choose_candidate(
                    _exclude_provider_results(results, rejected_file_ids), row, video
                )
            if choice is None:
                return StageStatus.UNAVAILABLE, {"reason": "No validated OpenSubtitles match"}, None
            raw, name, remaining = client.download(choice.file_id)
            source = work / f"downloaded.{language}.srt"
            source.write_bytes(_unpack(raw, name or choice.file_name))
            _write_cached_candidate_metadata(work, language, choice, source)
        except QuotaExhausted:
            raise

    assert source is not None
    duration = float(row["duration"])
    backward_jump_tolerance = float(
        cfg.get("max_backward_jump_seconds", SRT_BACKWARD_JUMP_TOLERANCE_SECONDS)
    )
    source_validation = _inspect_srt(
        source, duration, backward_jump_tolerance=backward_jump_tolerance
    )
    if source_validation.get("structural_error"):
        existing_preserved = source_kind == "existing"
        downloaded = source_kind.startswith("opensubtitles")
        rejection = None
        if downloaded:
            rejection = _quarantine_provider_candidate(
                source,
                work,
                language,
                choice,
                str(source_validation["structural_error"]),
            )
        details: dict[str, Any] = {
            "language": language,
            "source": source_kind,
            "source_path": str(source) if existing_preserved else None,
            "source_validation": source_validation,
            "validation": None,
            "remaining_downloads": remaining,
            "severity": "PRESERVED_UNVERIFIED" if existing_preserved else "CANDIDATE_REJECTED",
            "classification": "SUBTITLE_STRUCTURAL_CORRUPTION",
            "original_preserved": existing_preserved,
            "source_preserved": existing_preserved or rejection is not None,
            "candidate_installed": False,
            "output_committed": False,
            "retained_candidate_path": rejection.get("path") if rejection else None,
            "rejected_candidate": rejection,
            "reason": str(source_validation["structural_error"]),
            "adjustment_validation": {
                "reason": "Structurally invalid subtitles are rejected before ffsubsync"
            },
            "algorithm_version": SUBTITLE_ALGORITHM_VERSION,
        }
        provider_details = (
            _candidate_summary(choice)
            if choice is not None
            else cached_candidate_metadata
        )
        if provider_details:
            details["opensubtitles"] = provider_details
        if _recovery_context:
            details["alternative_recovery"] = _recovery_context
            details["original_preserved"] = True
        recovered = _attempt_alternative_recovery(
            video, row, language, cfg, client, work, source_kind, details
        )
        return recovered if recovered is not None else (StageStatus.NEEDS_REVIEW, details, None)

    temp_out = work / f"synced.{language}.srt"
    reference_audio_index, reference_audio_reason = _reference_audio_index(video, language)
    if reference_audio_index is None:
        return StageStatus.NEEDS_REVIEW, {"reason": reference_audio_reason, "language": language}, None
    sync_info = _run_ffsubsync(
        video,
        source,
        temp_out,
        cfg,
        reference_audio_index,
        candidate_validator=lambda candidate: _validate_srt(candidate, duration),
    )
    srt_info = _inspect_srt(temp_out, duration)
    runtime_rejected = (
        not srt_info["valid"]
        and (
            bool(sync_info.get("candidate_validation_error"))
            or (not source_validation["valid"] and int(srt_info["timestamp_count"]) >= 3)
        )
    )
    if runtime_rejected:
        existing_preserved = source_kind == "existing"
        downloaded = source_kind.startswith("opensubtitles")
        rejection = None
        if downloaded:
            rejection = _quarantine_provider_candidate(
                source,
                work,
                language,
                choice,
                str(srt_info["error"]),
            )
        source_was_invalid = not source_validation["valid"]
        classification = (
            "SUBTITLE_RELEASE_RUNTIME_MISMATCH"
            if source_was_invalid
            else "FFSUBSYNC_RUNTIME_REGRESSION"
        )
        if source_was_invalid:
            reason = (
                "Subtitle source appears to target a different media release: "
                f"{source_validation['error']}; synchronization did not produce a plausible runtime"
            )
        else:
            reason = (
                "ffsubsync candidates were rejected without replacing the source: "
                f"{srt_info['error']}"
            )
        details: dict[str, Any] = {
            "language": language,
            "source": source_kind,
            "source_path": str(source) if existing_preserved else None,
            "source_validation": source_validation,
            "ffsubsync": sync_info,
            "validation": srt_info,
            "remaining_downloads": remaining,
            "reference_audio_index": reference_audio_index,
            "reference_audio_reason": reference_audio_reason,
            "embedded": embedded_info,
            "severity": "PRESERVED_UNVERIFIED" if existing_preserved else "CANDIDATE_REJECTED",
            "classification": classification,
            "original_preserved": existing_preserved,
            "source_preserved": True,
            "candidate_installed": False,
            "output_committed": False,
            "retained_candidate_path": rejection.get("path") if rejection else None,
            "rejected_candidate": rejection,
            "reason": reason,
            "adjustment_validation": {
                "reason": "No runtime-invalid synchronization candidate may be committed",
            },
            "algorithm_version": SUBTITLE_ALGORITHM_VERSION,
        }
        if choice:
            details["opensubtitles"] = {
                "file_id": choice.file_id, "release": choice.release, "hash_match": choice.hash_match,
                "trusted": choice.trusted, "score": choice.score,
            }
        if _recovery_context:
            details["alternative_recovery"] = _recovery_context
            details["original_preserved"] = True
        temp_out.unlink(missing_ok=True)
        recovered = _attempt_alternative_recovery(
            video, row, language, cfg, client, work, source_kind, details
        )
        return recovered if recovered is not None else (StageStatus.NEEDS_REVIEW, details, None)
    if sync_info.get("quality_rejected") or sync_info.get("low_quality"):
        existing_preserved = source_kind == "existing"
        downloaded = source_kind.startswith("opensubtitles")
        rejection = None
        if downloaded:
            rejection = _quarantine_provider_candidate(
                source,
                work,
                language,
                choice,
                str(sync_info.get("low_quality_reason") or "ffsubsync rejected the alignment"),
            )
        if existing_preserved:
            action = "Existing subtitle was preserved unchanged"
        elif downloaded:
            action = "Downloaded subtitle was quarantined in work storage and not installed"
        else:
            action = "Embedded subtitle was left unchanged and no external sidecar was installed"
        reason = (
            f"{action} because ffsubsync rejected the proposed alignment as low quality: "
            f"{sync_info.get('low_quality_reason') or 'unspecified quality check'}"
        )
        details = {
            "language": language,
            "source": source_kind,
            "source_path": str(source) if existing_preserved else None,
            "source_validation": source_validation,
            "ffsubsync": sync_info,
            "validation": srt_info,
            "remaining_downloads": remaining,
            "reference_audio_index": reference_audio_index,
            "reference_audio_reason": reference_audio_reason,
            "embedded": embedded_info,
            "severity": "PRESERVED_UNVERIFIED" if existing_preserved else "CANDIDATE_REJECTED",
            "classification": "FFSUBSYNC_LOW_QUALITY_NOOP",
            "original_preserved": existing_preserved,
            "source_preserved": True,
            "candidate_installed": False,
            "output_committed": False,
            "retained_candidate_path": rejection.get("path") if rejection else None,
            "rejected_candidate": rejection,
            "reason": reason,
            "adjustment_validation": {
                "reason": "No timing adjustment was evaluated because ffsubsync rejected the alignment",
            },
            "algorithm_version": SUBTITLE_ALGORITHM_VERSION,
        }
        if choice:
            details["opensubtitles"] = {
                "file_id": choice.file_id, "release": choice.release, "hash_match": choice.hash_match,
                "trusted": choice.trusted, "score": choice.score,
            }
        if _recovery_context:
            details["alternative_recovery"] = _recovery_context
            details["original_preserved"] = True
        # ffsubsync's safety mode writes a copy of the unchanged input to its
        # requested output path.  It is not a synchronization candidate and
        # must never be mistaken for one by a later interrupted run.
        temp_out.unlink(missing_ok=True)
        recovered = _attempt_alternative_recovery(
            video, row, language, cfg, client, work, source_kind, details
        )
        return recovered if recovered is not None else (StageStatus.NEEDS_REVIEW, details, None)

    severity, adjustment_validation = _subtitle_adjustment_severity(
        video, temp_out, sync_info, reference_audio_index, float(row["duration"]), cfg
    )
    details: dict[str, Any] = {
        "language": language,
        "source": source_kind,
        "source_path": str(source) if source_kind == "existing" else None,
        "source_validation": source_validation,
        "ffsubsync": sync_info,
        "validation": srt_info,
        "remaining_downloads": remaining,
        "reference_audio_index": reference_audio_index,
        "reference_audio_reason": reference_audio_reason,
        "embedded": embedded_info,
        "severity": severity,
        "adjustment_validation": adjustment_validation,
        "algorithm_version": SUBTITLE_ALGORITHM_VERSION,
    }
    if choice:
        details["opensubtitles"] = {
            "file_id": choice.file_id, "release": choice.release, "hash_match": choice.hash_match,
            "trusted": choice.trusted, "score": choice.score,
        }
    if _recovery_context:
        details["alternative_recovery"] = _recovery_context
        details["original_preserved"] = True
    if severity == "NEEDS_REVIEW":
        details["reason"] = adjustment_validation.get("reason")
        return StageStatus.NEEDS_REVIEW, details, temp_out
    return StageStatus.COMPLETE, details, temp_out


def _attempt_alternative_recovery(
    video: Path,
    row,
    language: str,
    cfg: dict,
    client: OpenSubtitlesClient | None,
    work: Path,
    source_kind: str,
    initial_details: dict[str, Any],
) -> tuple[StageStatus, dict[str, Any], Path | None] | None:
    """Try another provider subtitle, excluding every candidate already rejected."""
    if (
        not bool(cfg.get("alternative_recovery", True))
        or client is None
        or not client.configured()
    ):
        return None
    try:
        prior_context = initial_details.get("alternative_recovery")
        if not isinstance(prior_context, dict):
            prior_context = {}
        attempted_file_ids: set[int] = set()
        for value in prior_context.get("attempted_file_ids", []):
            try:
                attempted_file_ids.add(int(value))
            except (TypeError, ValueError):
                pass
        current_provider = initial_details.get("opensubtitles")
        if isinstance(current_provider, dict) and current_provider.get("file_id") is not None:
            try:
                attempted_file_ids.add(int(current_provider["file_id"]))
            except (TypeError, ValueError):
                pass

        candidate_limit = max(1, int(cfg.get("alternative_candidate_limit", 3)))
        if len(attempted_file_ids) >= candidate_limit:
            initial_details["alternative_recovery"] = {
                "attempted": True,
                "result": "candidate_limit_reached",
                "attempted_file_ids": sorted(attempted_file_ids),
                "candidate_limit": candidate_limit,
            }
            return StageStatus.NEEDS_REVIEW, initial_details, None

        rejected_file_ids, rejected_hashes = _load_rejected_candidates(work, language)
        excluded_file_ids = rejected_file_ids | attempted_file_ids
        moviehash, size = opensubtitles_hash(video)
        results = client.search(
            language=language,
            media_type=row["media_type"],
            moviehash=moviehash,
            moviebytesize=size,
            season=row["season"],
            episode=row["episode"],
            hearing_impaired=str(cfg.get("hearing_impaired", "exclude")),
        )
        choice = choose_candidate(
            _exclude_provider_results(results, excluded_file_ids), row, video
        )
        if choice is None:
            query = row["show_title"] if row["media_type"] == "episode" else row["title"]
            results = client.search(
                language=language,
                media_type=row["media_type"],
                query=query,
                year=row["year"],
                season=row["season"],
                episode=row["episode"],
                hearing_impaired=str(cfg.get("hearing_impaired", "exclude")),
            )
            choice = choose_candidate(
                _exclude_provider_results(results, excluded_file_ids), row, video
            )
        if choice is None:
            initial_details["alternative_recovery"] = {
                "attempted": True,
                "result": "no_untried_provider_match",
                "attempted_file_ids": sorted(attempted_file_ids),
                "excluded_file_ids": sorted(rejected_file_ids),
                "candidate_limit": candidate_limit,
            }
            return StageStatus.NEEDS_REVIEW, initial_details, None
        raw, name, remaining = client.download(choice.file_id)
        alternative = work / f"alternative.{language}.srt"
        alternative.write_bytes(_unpack(raw, name or choice.file_name))
        attempted_file_ids.add(choice.file_id)
        alternative_sha256 = _sha256_file(alternative)
        if alternative_sha256 in rejected_hashes:
            _quarantine_provider_candidate(
                alternative,
                work,
                language,
                choice,
                "Provider returned bytes matching an already rejected subtitle",
            )
            initial_details["alternative_recovery"] = {
                "attempted": True,
                "result": "duplicate_rejected_candidate",
                "attempted_file_ids": sorted(attempted_file_ids),
                "candidate_limit": candidate_limit,
            }
            return _attempt_alternative_recovery(
                video, row, language, cfg, client, work, source_kind, initial_details
            )
        result = process_subtitle_language(
            video,
            row,
            language,
            cfg,
            client,
            work,
            _forced_source=(alternative, "opensubtitles_alternative", choice, remaining),
            _recovery_context={
                "attempted": True,
                "result": "provider_candidate_evaluated",
                "initial_source": source_kind,
                "initial_classification": initial_details.get("classification"),
                "initial_reason": initial_details.get("reason"),
                "attempted_file_ids": sorted(attempted_file_ids),
                "candidate_limit": candidate_limit,
                "candidate": _candidate_summary(choice),
            },
        )
        return result
    except QuotaExhausted:
        raise
    except Exception as exc:
        initial_details["alternative_recovery"] = {
            "attempted": True,
            "result": "provider_error",
            "error": f"{type(exc).__name__}: {exc}",
        }
        return StageStatus.NEEDS_REVIEW, initial_details, None
