from __future__ import annotations

import json
from pathlib import Path

from .models import ProbeInfo
from .utils import MediaManagerError, require_tool, run


def probe_media(path: Path) -> ProbeInfo:
    ffprobe = require_tool("ffprobe")
    proc = run([
        ffprobe, "-v", "error", "-show_format", "-show_streams", "-show_chapters",
        "-of", "json", str(path),
    ])
    try:
        raw = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise MediaManagerError(f"ffprobe returned invalid JSON for {path}") from exc
    streams = raw.get("streams") or []
    chapters = raw.get("chapters") or []
    fmt = raw.get("format") or {}
    try:
        duration = float(fmt.get("duration"))
    except (TypeError, ValueError):
        durations = []
        for s in streams:
            try:
                durations.append(float(s.get("duration")))
            except (TypeError, ValueError):
                pass
        if not durations:
            raise MediaManagerError(f"Could not determine duration: {path}")
        duration = max(durations)
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    subtitles = [s for s in streams if s.get("codec_type") == "subtitle"]
    v0 = videos[0] if videos else {}
    return ProbeInfo(
        duration=duration,
        format_name=str(fmt.get("format_name") or ""),
        streams=streams,
        chapters=chapters,
        video_codec=v0.get("codec_name"),
        width=int(v0["width"]) if v0.get("width") else None,
        height=int(v0["height"]) if v0.get("height") else None,
        audio_streams=audios,
        subtitle_streams=subtitles,
        raw=raw,
    )


def stream_language(stream: dict) -> str:
    tags = stream.get("tags") or {}
    return str(tags.get("language") or "").casefold()


def audio_role_text(stream: dict) -> str:
    tags = stream.get("tags") or {}
    pieces = [tags.get("title", ""), tags.get("handler_name", "")]
    return " ".join(str(x) for x in pieces if x).casefold()


def choose_authoritative_audio(probe: ProbeInfo, language: str = "eng") -> tuple[int | None, str]:
    aliases = {
        "eng": {"eng", "en"}, "en": {"eng", "en"},
        "ara": {"ara", "ar"}, "ar": {"ara", "ar"},
    }
    wanted = aliases.get(language.casefold(), {language.casefold()})
    candidates: list[tuple[int, dict]] = []
    for i, stream in enumerate(probe.audio_streams):
        role = audio_role_text(stream)
        if any(word in role for word in ("commentary", "description", "descriptive", "audio description")):
            continue
        if stream_language(stream) in wanted:
            candidates.append((i, stream))
    if candidates:
        candidates.sort(key=lambda pair: int((pair[1].get("disposition") or {}).get("default", 0)), reverse=True)
        return candidates[0][0], "language_match"
    if len(probe.audio_streams) == 1:
        return 0, "only_audio_stream"
    return None, "no_unambiguous_authoritative_audio"
