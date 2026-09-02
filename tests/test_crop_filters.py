from media_manager.crop.engine import _valid_samples
from media_manager.models import CropSample


def test_black_and_neighboring_fade_frames_are_ignored():
    samples = [
        CropSample(0.0, 0, 0, 20, 20, yavg=90, yhigh=180),
        CropSample(1.0, 0, 0, 20, 20, yavg=45, yhigh=90),
        CropSample(2.0, 0, 0, 0, 0, yavg=10, yhigh=20),
        CropSample(3.0, 0, 0, 20, 20, yavg=48, yhigh=95),
        CropSample(6.0, 0, 0, 20, 20, yavg=42, yhigh=90),
    ]
    valid, ignored = _valid_samples(
        samples, 22, 38, fade_guard_seconds=2.0, fade_yavg=55.0
    )
    assert [x.time for x in valid] == [0.0, 6.0]
    assert ignored == 3


def test_native_pixel_margin_keeps_real_two_pixel_border():
    import numpy as np
    from media_manager.crop.engine import _pixel_margins

    frame = np.zeros((100, 160), dtype=np.uint8)
    frame[2:98, :] = 140
    left, right, top, bottom = _pixel_margins(
        frame, black_luma=28, black_fraction=0.995, max_stddev=12.0
    )
    assert (left, right, top, bottom) == (0, 0, 2, 2)


def test_native_pixel_margin_does_not_hide_real_edge_content():
    import numpy as np
    from media_manager.crop.engine import _pixel_margins

    frame = np.zeros((100, 160), dtype=np.uint8)
    frame[2:98, :] = 140
    frame[0, 20:80] = 120  # genuine picture/overlay uses the outermost row
    _left, _right, top, _bottom = _pixel_margins(
        frame, black_luma=28, black_fraction=0.995, max_stddev=12.0
    )
    assert top == 0


def test_crop_ffmpeg_cannot_read_interactive_terminal_input(monkeypatch, tmp_path):
    import io
    import subprocess

    from media_manager.crop import engine

    captured = {}

    class FakeProcess:
        stdout = io.BytesIO(b"")
        stderr = io.BytesIO(b"")

        @staticmethod
        def wait():
            return 0

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(engine, "require_tool", lambda _name: "ffmpeg")
    monkeypatch.setattr(engine.subprocess, "Popen", fake_popen)

    engine._scan(tmp_path / "movie.mp4", source_width=2, source_height=2, fps=0.2)

    assert "-nostdin" in captured["cmd"]
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL
