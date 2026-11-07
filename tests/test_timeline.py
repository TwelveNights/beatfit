"""Render shots with real ffmpeg: frame-exact video, soft cuts only around the cuts, audio in
sync and time-stretched with its pitch kept.

The source's luma stores its frame index (low byte in the left half, high byte in the right),
and its audio is a quiet 500 Hz tone with a 3 kHz click at every whole second."""

from __future__ import annotations

import math
import shutil
import subprocess
from array import array

import pytest

from beatfit import Piece
from beatfit.timeline import Shot, Transitions, command, runs

FFMPEG = shutil.which("ffmpeg")
W, H, FPS, SR = 32, 16, 60, 48000
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not on PATH")


def P(s: int, n_src: int, n_out: int, jump: bool = False) -> Piece:
    return Piece(s, n_src, n_out, jump)


def shots(src: str) -> list[Shot]:
    return [
        Shot(src, [P(60, 90, 90), P(150, 120, 40), P(270, 60, 60)]),
        Shot(src, [P(600, 60, 60), P(660, 50, 20), P(900, 40, 10, jump=True), P(940, 100, 100)]),
        Shot(src, [P(1200, 100, 100)]),
    ]


def expected() -> list[tuple[int, int | None, tuple[int, int]]]:
    """(output frame, exact source frame for 1x pieces or None, source range of its piece)."""
    out, o = [], 0
    for s in shots(""):
        for p in s.pieces:
            for k in range(p.out_frames):
                out.append((o, p.src + k if p.src_frames == p.out_frames else None, (p.src, p.src_end)))
                o += 1
    return out


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    if FFMPEG is None:
        pytest.skip("ffmpeg not on PATH")
    d = tmp_path_factory.mktemp("timeline")
    src, song = d / "src.mkv", d / "song.wav"
    lum = f"if(lt(X,{W // 2}),mod(N,256),floor(N/256))"
    tone = "0.1*sin(2*PI*500*t)+0.8*sin(2*PI*3000*t)*lt(mod(t,1),0.01)"
    subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s={W}x{H}:r={FPS}:d=30,format=yuv420p,geq=lum='{lum}':cb=128:cr=128",
            "-f",
            "lavfi",
            "-i",
            f"aevalsrc='{tone}':s={SR}:c=stereo:d=30",
            "-c:v",
            "ffv1",
            "-c:a",
            "pcm_s16le",
            str(src),
        ],
        check=True,
    )
    subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=100:sample_rate={SR}:duration=8",
            "-ac",
            "2",
            str(song),
        ],
        check=True,
    )
    return d, str(src), str(song)


def render(d, src: str, name: str, t: Transitions, song: str | None = None) -> tuple[list[int], array]:
    out = d / f"{name}.mkv"
    enc = ["-c:v", "ffv1", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le"]
    subprocess.run(command(shots(src), str(out), FPS, t, song=song, ffmpeg=FFMPEG, encode=enc), check=True, capture_output=True)
    raw = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(out), "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"], check=True, capture_output=True
    ).stdout
    size = W * H * 3 // 2
    idx = [raw[k * size + 2] + 256 * raw[k * size + W - 3] for k in range(len(raw) // size)]
    pcm = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(out), "-ac", "1", "-f", "s16le", "-"], check=True, capture_output=True
    ).stdout
    return idx, array("h", pcm)


def clicks(y: array) -> list[float]:
    """Onsets (s) of loud bursts after at least 0.2 s without one."""
    out, last = [], -SR
    for i, v in enumerate(y):
        if abs(v) > 0.5 * 32767:
            if i - last > 0.2 * SR:
                out.append(i / SR)
            last = i
    return out


def power(y: array, a: int, b: int, hz: float) -> float:
    """Goertzel power of ``hz`` in samples [a, b)."""
    w = 2 * math.cos(2 * math.pi * hz / SR)
    s1 = s2 = 0.0
    for v in y[a:b]:
        s1, s2 = v + w * s1 - s2, s1
    return s1 * s1 + s2 * s2 - w * s1 * s2


def test_runs_split_at_jump_cuts_and_get_handles():
    rs = runs(shots("s"), FPS)
    assert [(r.shot, r.src_start, r.out_frames) for r in rs] == [(0, 60, 190), (1, 600, 80), (1, 900, 110), (2, 1200, 100)]
    assert (rs[0].post, rs[1].pre) == (15, 15)  # 0.5 s between shots
    assert (rs[1].post, rs[2].pre) == (8, 8)  # 0.25 s at the jump cut
    hard = runs(shots("s"), FPS, Transitions.hard())
    assert all(r.pre == r.post == 0 for r in hard)


def test_handles_stay_inside_the_usable_source():
    s = shots("s")
    s[0].src_hi = 335  # 5 frames past the first shot's end
    s[1].src_lo = 590  # 10 before the second shot's start
    rs = runs(s, FPS)
    assert (rs[0].post, rs[1].pre) == (5, 10)


@needs_ffmpeg
def test_hard_cuts_are_frame_exact_and_audio_in_sync(media):
    d, src, _song = media
    idx, y = render(d, src, "hard", Transitions.hard())
    assert len(idx) == 480
    for o, exact, (lo, hi) in expected():
        if exact is not None:
            assert idx[o] == exact, (o, idx[o], exact)
        else:
            assert lo <= idx[o] < hi, (o, idx[o], lo, hi)
    assert abs(len(y) / SR - 480 / FPS) < 0.01
    # source seconds 2, 16 and 21 are in 1x pieces: output frames 60, 300 and 440
    got = clicks(y)
    for want in (60 / FPS, 300 / FPS, 440 / FPS):
        assert min(abs(c - want) for c in got) < 0.004, (want, got)


@needs_ffmpeg
def test_sped_up_audio_keeps_its_pitch(media):
    d, src, _song = media
    _idx, y = render(d, src, "pitch", Transitions.hard())
    # the 3x piece is output frames 90..130; avoid its clicks (source seconds 3 and 4)
    a, b = 103 * 800, 117 * 800
    assert power(y, a, b, 500) > 20 * power(y, a, b, 1500)  # not shifted up to 3 x 500 Hz


@needs_ffmpeg
def test_soft_cuts_dissolve_only_around_the_cuts(media):
    d, src, _song = media
    idx, y = render(d, src, "soft", Transitions())
    assert len(idx) == 480
    cuts = {190: 15, 270: 8, 380: 15}  # output frame of the cut -> half the dissolve

    def near(o: int) -> bool:
        return any(c - w <= o < c + w for c, w in cuts.items())

    for o, exact, _r in expected():
        if exact is not None and not near(o):
            assert idx[o] == exact, (o, idx[o], exact)
    # in the middle of a dissolve the frame is a blend: neither side's frame
    assert idx[190] not in (330, 600)  # shot 1 just past its end (handle), shot 2 at its start
    assert abs(len(y) / SR - 480 / FPS) < 0.01
    got = clicks(y)
    for want in (60 / FPS, 300 / FPS, 440 / FPS):
        assert min(abs(c - want) for c in got) < 0.004, (want, got)


@needs_ffmpeg
def test_a_song_plays_under_the_shots(media):
    d, src, song = media
    _idx, y = render(d, src, "song", Transitions(), song=song)
    assert abs(len(y) / SR - 480 / FPS) < 0.01
    a, b = 10 * 800, 50 * 800  # a stretch with the source's tone, no clicks
    assert power(y, a, b, 100) > 0.1 * power(y, a, b, 500)  # the song's 100 Hz is in the mix
