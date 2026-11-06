"""Render a fit with ffmpeg: one input per continuous stretch (split at jump cuts), each
retimed with a piecewise-linear ``setpts``, resampled to a constant frame rate and
concatenated. Video only: retiming audio is left to the caller."""

from __future__ import annotations

from dataclasses import dataclass

from .fit import Piece


@dataclass
class Segment:
    """A continuous stretch of source (no jump cut inside)."""

    pieces: list[Piece]

    @property
    def src(self) -> int:
        return self.pieces[0].src

    @property
    def src_frames(self) -> int:
        return sum(p.src_frames for p in self.pieces)

    @property
    def out_frames(self) -> int:
        return sum(p.out_frames for p in self.pieces)


def segments(pieces: list[Piece]) -> list[Segment]:
    """Split pieces at jump cuts (and anywhere else the source is not continuous)."""
    out: list[Segment] = []
    for pc in pieces:
        if out and not pc.jump and out[-1].pieces[-1].src_end == pc.src:
            out[-1].pieces.append(pc)
        else:
            out.append(Segment([pc]))
    return out


def _num(x: float) -> str:
    return f"{x:.9f}".rstrip("0").rstrip(".") if x != int(x) else str(int(x))


def setpts_expr(pieces: list[Piece], fps: float) -> str:
    """Expression mapping segment-relative source time ``T`` (seconds) to output seconds."""
    terms = []
    a = 0
    for p in pieces:
        terms.append(f"clip(T-{_num(a / fps)},0,{_num(p.src_frames / fps)})*{_num(p.out_frames / p.src_frames)}")
        a += p.src_frames
    return "+".join(terms)


def filter_graph(pieces: list[Piece], fps: float, out_label: str = "v") -> str:
    """``-filter_complex`` graph for the inputs made by :func:`input_args` (input k is
    segment k), ending in ``[out_label]``."""
    segs = segments(pieces)
    chains = []
    for k, seg in enumerate(segs):
        ch = f"[{k}:v:0]setpts=PTS-STARTPTS"
        if any(p.src_frames != p.out_frames for p in seg.pieces):
            ch += f",setpts='({setpts_expr(seg.pieces, fps)})/TB'"
        # a constant frame rate, padded and trimmed to exactly the planned number of frames
        ch += f",fps=fps={_num(fps)},tpad=stop_mode=clone:stop=8,trim=end_frame={seg.out_frames},setpts=PTS-STARTPTS[s{k}]"
        chains.append(ch)
    chains.append("".join(f"[s{k}]" for k in range(len(segs))) + f"concat=n={len(segs)}:v=1:a=0[{out_label}]")
    return ";\n".join(chains)


def input_args(source: str, pieces: list[Piece], fps: float) -> list[str]:
    """``-ss``/``-t``/``-i`` arguments, one input per segment. Source frames are taken to be
    ``1 / fps`` seconds from the start of the file, whatever its own frame rate."""
    args: list[str] = []
    for seg in segments(pieces):
        args += ["-ss", _num(seg.src / fps), "-t", _num((seg.src_frames + 2) / fps), "-i", source]
    return args


def command(
    source: str, pieces: list[Piece], fps: float, output: str, ffmpeg: str = "ffmpeg", encode: list[str] | None = None
) -> list[str]:
    """A complete ffmpeg command rendering the pieces of ``source`` to ``output`` (no audio)."""
    enc = encode if encode is not None else ["-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p"]
    return [
        ffmpeg,
        "-hide_banner",
        "-y",
        *input_args(source, pieces, fps),
        "-filter_complex",
        filter_graph(pieces, fps),
        "-map",
        "[v]",
        "-an",
        *enc,
        output,
    ]
