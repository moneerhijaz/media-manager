"""Local, bounded language sampling and explicit track selection."""
import json
import os
import signal
import subprocess
import sys
from collections import Counter

VERSION = "audio-language-v1"


def canonical(value):
    value = str(value or "").lower()
    return {"eng": "en", "jpn": "ja", "ara": "ar", "fra": "fr", "fre": "fr",
            "deu": "de", "ger": "de", "spa": "es", "ita": "it", "kor": "ko",
            "zho": "zh", "chi": "zh", "rus": "ru", "por": "pt", "hin": "hi"}.get(value, value)


def inventory(probe):
    tracks = []
    for i, stream in enumerate(probe.audio_streams):
        tags, disposition = stream.get("tags", {}), stream.get("disposition", {})
        title = str(tags.get("title", ""))
        role = (title + " " + str(tags.get("handler_name", ""))).lower()
        excluded = any(word in role for word in ("commentary", "description", "descriptive")) or bool(disposition.get("comment") or disposition.get("visual_impaired"))
        tracks.append({"audio_index": i, "stream_index": stream.get("index"), "title": title,
                       "declared_language": canonical(tags.get("language")), "default": bool(disposition.get("default")),
                       "excluded_role": excluded, "status": "unverified", "samples": []})
    return tracks


def summarize(samples, declared, threshold=0.8):
    counts = Counter(s["language"] for s in samples if s.get("speech") and s.get("probability", 0) >= threshold)
    if not counts:
        return {"status": "uncertain", "detected_language": None}
    winner, count = counts.most_common(1)[0]
    status = "multilingual" if len(counts) > 1 else "verified" if count >= 3 else "uncertain"
    return {"status": status, "detected_language": winner if status == "verified" else None,
            "observed_languages": sorted(counts), "tag_conflict": bool(declared and declared != winner),
            "coverage": "distributed samples only; not exhaustive language-change detection"}


def detect(path, probe, cfg):
    tracks = inventory(probe)
    request = {"path": str(path), "duration": probe.duration, "tracks": tracks, "cfg": cfg}
    proc = subprocess.Popen([sys.executable, "-m", __name__], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, _ = proc.communicate(json.dumps(request), timeout=min(900, max(10, float(cfg.get("timeout_seconds", 300)))))
        if proc.returncode == 0:
            try:
                return json.loads(stdout)
            except (ValueError, TypeError):
                pass
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
    except BaseException:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
        raise
    return {"version": VERSION, "tracks": tracks, "reason": "Language detector unavailable or timed out"}


def selections(data, metadata, preferred):
    usable = [t for t in data["tracks"] if not t["excluded_role"]]
    def language(t):
        return t.get("detected_language") or t["declared_language"]
    ranked = sorted(usable, key=lambda t: (next((i for i, lang in enumerate(preferred) if canonical(lang) == language(t)), len(preferred)), not t["default"], t["audio_index"]))
    original = canonical(metadata.get("original_language")) if metadata.get("verified") else None
    references = [t for t in usable if original and t.get("status") == "verified" and t.get("detected_language") == original
                  and "dub" not in t["title"].lower()]
    return {"playback_audio_index": ranked[0]["audio_index"] if ranked else None,
            "av_reference_audio_index": references[0]["audio_index"] if references else None,
            "reference_reason": "Metadata original language confirmed by sampled speech" if references else "Original speech reference unverified; dub correction withheld"}


def worker(request):
    from faster_whisper import WhisperModel
    import numpy as np
    cfg = request["cfg"]
    model = WhisperModel(cfg.get("model", "tiny"), device="cpu", compute_type="int8", cpu_threads=2,
                         local_files_only=True)
    for track in request["tracks"]:
        if track["excluded_role"]:
            continue
        # Start at three widely separated windows; expand uncertainty/conflict.
        for fraction in (0.15, 0.5, 0.85, 0.3, 0.65, 0.95):
            sample = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(max(0, request["duration"] * fraction - 15)),
                                     "-i", request["path"], "-map", f"0:a:{track['audio_index']}", "-t", "30",
                                     "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1"], capture_output=True, timeout=40, check=True)
            audio = np.frombuffer(sample.stdout, dtype=np.float32)
            segments, info = model.transcribe(audio, beam_size=1, vad_filter=True, condition_on_previous_text=False)
            speech = any(segment.no_speech_prob < 0.5 for segment in segments)
            track["samples"].append({"fraction": fraction, "language": info.language,
                                     "probability": info.language_probability, "speech": speech})
            track.update(summarize(track["samples"], track["declared_language"]))
            if len(track["samples"]) >= 3 and track["status"] == "verified" and not track.get("tag_conflict"):
                break
    return {"version": VERSION, "tracks": request["tracks"]}


if __name__ == "__main__":
    print(json.dumps(worker(json.load(sys.stdin))))
