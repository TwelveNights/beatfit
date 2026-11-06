"""Render a fit with real ffmpeg and check every output frame shows the planned source frame."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from beatfit import Clip, Event, Params, Piece, fit_clip, source_at
from beatfit.ffmpeg import command, filter_graph, segments, setpts_expr

FFMPEG = shutil.which("ffmpeg")
W, H = 32, 8


def test_segments_split_at_jump_cuts():
    pcs = [Piece(0, 60, 60), Piece(60, 90, 60), Piece(400, 60, 60, jump=True), Piece(460, 30, 30)]
    segs = segments(pcs)
    assert [(s.src, s.src_frames, s.out_frames) for s in segs] == [(0, 150, 120), (400, 90, 90)]
    assert setpts_expr(segs[0].pieces, 60) == "clip(T-0,0,1)*1+clip(T-1,0,1.5)*0.666666667"
    g = filter_graph(pcs, 60)
    assert "[0:v:0]" in g and "[1:v:0]" in g and "concat=n=2" in g
    assert "setpts='(" not in g.split(";")[1]  # an all-1x segment is not retimed


def _numbered_source(path, frames: int, fps: int) -> None:
    """Lossless video whose frame N stores N in its pixels (low byte left, high byte right)."""
    lum = f"if(lt(X,{W // 2}),mod(N,256),floor(N/256))"
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s={W}x{H}:r={fps}:d={frames / fps}",
            "-vf",
            f"format=gray,geq=lum='{lum}'",
            "-c:v",
            "ffv1",
            str(path),
        ],
        check=True,
    )


def _frames(path) -> list[int]:
    raw = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        check=True,
        capture_output=True,
    ).stdout
    out = []
    for k in range(len(raw) // (W * H)):
        row = raw[k * W * H : k * W * H + W]  # first row
        out.append(row[2] + 256 * row[W - 3])
    return out


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not on PATH")
@pytest.mark.parametrize("src_fps", [60, 30])
def test_render_shows_exactly_the_planned_frames(tmp_path, src_fps):
    fps = 60
    beats = [round(0.5 + 0.5 * k, 6) for k in range(80)]
    # events 2 s, 12 s and 55 s into the source: one sped-up gap, one jump cut
    evs = [Event(round(t * fps)) for t in (2.0, 12.0, 55.0)]
    c = Clip(evs, 0, round(60 * fps))
    start, n = round(beats[0] * fps), round((beats[24] - beats[0]) * fps)
    f = fit_clip(c, start, n, beats, beats[::4], Params(lead_min=1.5))
    assert f is not None and f.jumps >= 1 and f.max_speed > 1.0
    src, out = tmp_path / "src.mkv", tmp_path / "out.mkv"
    _numbered_source(src, 60 * src_fps, src_fps)
    cmd = command(str(src), f.pieces, fps, str(out), ffmpeg=FFMPEG, encode=["-c:v", "ffv1", "-pix_fmt", "gray"])
    subprocess.run(cmd, check=True, capture_output=True)
    got = _frames(out)
    assert len(got) == n
    scale = src_fps / fps  # source frames are 1/fps s; the file holds one frame per 1/src_fps s
    for o in range(n):
        want = source_at(f.pieces, o)
        pc_speed = next(p.speed for p, a in _with_out(f.pieces) if a <= o < a + p.out_frames)
        tol = 0 if pc_speed == 1.0 and scale == 1 else 1
        assert abs(got[o] - int(want * scale)) <= tol, (o, got[o], want)
    for e, o in zip(evs, f.event_out):  # events are exact
        assert got[o] == int(e.frame * scale)


def _with_out(pieces):
    a = 0
    for p in pieces:
        yield p, a
        a += p.out_frames
