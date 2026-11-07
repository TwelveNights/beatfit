"""Render several fitted clips one after another, with their own audio and soft cuts.

A *shot* is one fitted clip (its pieces) from one source file. Shots play back to back.
Inside a shot, a jump cut also starts a new continuous stretch of source, a *run*; each run
is one ffmpeg input.

Cuts are soft: both sides of a cut run on past it (*handles*: real source frames at 1x,
inside the shot's usable source range) and overlap, centred on the cut, so everything away
from a cut stays frame-exact. Between shots that is a ``clip_fade`` dissolve with a
``clip_audio_fade`` equal-power audio crossfade; at a jump cut a shorter ``jump_fade`` /
``jump_audio_fade``. All four at 0 give hard cuts (with a very short audio crossfade, so
the cut does not click).

Video: each run is seeked accurately, retimed with a piecewise-linear ``setpts`` (1x
pieces map frame to frame), put on the output frame grid by ``fps``, trimmed to an exact
frame count, and joined with ``xfade``. Audio: each run's audio is cut sample-exactly
(``sample_rate / fps`` samples per frame), sped-up pieces are time-stretched with
``atempo`` (pitch preserved), and the runs are placed with ``adelay`` and summed.

Frame numbers are ``1 / fps`` seconds into the source file, as everywhere in beatfit.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ffmpeg import _num
from .fit import Piece

SAMPLE_RATE = 48000
_AFMT = "aformat=sample_fmts=fltp:sample_rates={sr}:channel_layouts=stereo"


@dataclass
class Shot:
    """One fitted clip from one source file."""

    source: str
    pieces: list[Piece]
    src_lo: int | None = None  # handles never reach before this source frame
    src_hi: int | None = None  # ... or past this one (exclusive)
    video_filter: str = ""  # ffmpeg filters for this shot's frames, e.g. "crop=1920:1080:320:0,scale=1280:720"


@dataclass
class Transitions:
    """Lengths in seconds of the overlaps at cuts."""

    clip_fade: float = 0.5  # dissolve between shots
    clip_audio_fade: float = 0.6
    jump_fade: float = 0.25  # dissolve at a jump cut inside a shot
    jump_audio_fade: float = 0.3
    min_audio_fade: float = 0.03  # shortest audio crossfade, used at hard cuts too

    @classmethod
    def hard(cls) -> Transitions:
        return cls(0.0, 0.0, 0.0, 0.0)


@dataclass
class Run:
    """Pieces of one shot with continuous source: one ffmpeg input."""

    shot: int
    source: str
    src_start: int
    pieces: list[Piece] = field(default_factory=list)
    first_in_shot: bool = False
    last_in_shot: bool = False
    pre: int = 0  # video handle frames before the run
    post: int = 0  # ... and after it
    a_pre: int = 0  # audio handle samples before the run
    a_post: int = 0  # ... and after it

    @property
    def src_frames(self) -> int:
        return sum(p.src_frames for p in self.pieces)

    @property
    def out_frames(self) -> int:
        return sum(p.out_frames for p in self.pieces)


def samples_per_frame(fps: int, sample_rate: int = SAMPLE_RATE) -> int:
    if sample_rate % fps:
        raise ValueError(f"sample rate {sample_rate} is not a whole number of samples per frame at {fps} fps")
    return sample_rate // fps


def runs(shots: list[Shot], fps: int, transitions: Transitions | None = None, sample_rate: int = SAMPLE_RATE) -> list[Run]:
    """Split the shots into runs and size the handles at every cut."""
    out: list[Run] = []
    for k, s in enumerate(shots):
        cur: Run | None = None
        for p in s.pieces:
            if cur is None or p.jump or cur.src_start + cur.src_frames != p.src:
                cur = Run(k, s.source, p.src, first_in_shot=(cur is None))
                out.append(cur)
            cur.pieces.append(p)
        if cur is not None:
            cur.last_in_shot = True
    _handles(shots, out, fps, transitions or Transitions(), sample_rate)
    return out


def _handles(shots: list[Shot], rs: list[Run], fps: int, t: Transitions, sr: int) -> None:
    """Size each cut's transition. Handles stay inside their shot's source range, and a
    dissolve never takes more than its run has."""
    spf = samples_per_frame(fps, sr)
    ext = max(1, int(round(t.min_audio_fade / 2 * sr)))
    for r in rs:
        r.pre, r.post, r.a_pre, r.a_post = 0, 0, 0, ext
    for a, b in zip(rs, rs[1:]):
        fade, afade = (t.clip_fade, t.clip_audio_fade) if b.first_in_shot else (t.jump_fade, t.jump_audio_fade)
        room_a = 10**9
        hi = shots[a.shot].src_hi
        if hi is not None:
            room_a = max(0, hi - (a.src_start + a.src_frames))
        lo = shots[b.shot].src_lo
        room_b = max(0, b.src_start - (lo if lo is not None else 0))
        half = int(round(fade * fps / 2))
        a.post, b.pre = min(half, room_a), min(half, room_b)
        ahalf = max(ext, int(round(afade / 2 * sr)))
        a.a_post = min(ahalf, room_a * spf, a.out_frames * spf // 2) or 1
        b.a_pre = min(ahalf, room_b * spf, b.out_frames * spf // 2)
    # the two dissolves touching a run must not overlap inside it
    for k in range(1, len(rs) - 1):
        over = rs[k - 1].post + rs[k + 1].pre - rs[k].out_frames
        if over > 0:
            cut = (over + 1) // 2
            rs[k - 1].post = max(0, rs[k - 1].post - cut)
            rs[k + 1].pre = max(0, rs[k + 1].pre - cut)


def out_frames(rs: list[Run]) -> int:
    return sum(r.out_frames for r in rs)


# --------------------------------------------------------------------------------------
# video
# --------------------------------------------------------------------------------------


def run_setpts_expr(run: Run, fps: int) -> str | None:
    """setpts expression (seconds) mapping run-relative source time T to output time,
    handles included. None when everything is 1x."""
    if all(p.src_frames == p.out_frames for p in run.pieces):
        return None
    segs = [(run.pre, run.pre)] if run.pre else []  # the handles are 1x
    segs += [(p.src_frames, p.out_frames) for p in run.pieces]
    if run.post:
        segs.append((run.post, run.post))
    terms = []
    a = 0
    for n_src, n_out in segs:
        terms.append(f"clip(T-{_num(a / fps)},0,{_num(n_src / fps)})*{_num(n_out / n_src)}")
        a += n_src
    return "+".join(terms)


def blur_ranges(run: Run, fps: int, min_speed: float) -> list[tuple[float, float]]:
    """Source-time ranges (run-relative seconds) of pieces at ``min_speed`` or faster."""
    out = []
    a = run.pre
    for p in run.pieces:
        if p.src_frames / p.out_frames >= min_speed:
            out.append((a / fps - 0.25 / fps, (a + p.src_frames) / fps - 0.5 / fps))
        a += p.src_frames
    return out


def video_graph(
    shots: list[Shot],
    rs: list[Run],
    fps: int,
    blur_min_speed: float | None = None,
    finish: str = "null",
    out_label: str = "v",
    first_input: int = 0,
) -> str:
    """Filter graph for the video inputs made by :func:`video_inputs` (run k is input
    ``first_input + k``). ``blur_min_speed``: frame-blend motion blur on pieces at least
    this fast. ``finish``: filters applied to the joined result (e.g. fades)."""
    parts = []
    for r, run in enumerate(rs):
        vf = shots[run.shot].video_filter.strip(",")
        chain = f"[{first_input + r}:v:0]setpts=PTS-STARTPTS,{vf + ',' if vf else ''}format=yuv420p"
        ranges = blur_ranges(run, fps, blur_min_speed) if blur_min_speed is not None else []
        if ranges:
            # tmix over the whole run, the sharp frames overlaid wherever the speed is
            # below the threshold (tmix's own timeline switch replays stale frames)
            fast = max(p.src_frames / p.out_frames for p in run.pieces)
            k = 4 if fast >= 3.25 else (3 if fast >= 2.5 else 2)
            sped = "+".join(f"between(t,{_num(a)},{_num(b)})" for a, b in ranges)
            parts.append(f"{chain},split=2[s{r}a][s{r}b]")
            parts.append(f"[s{r}a]tmix=frames={k}[s{r}bl]")
            parts.append(f"[s{r}bl][s{r}b]overlay=x=0:y=0:format=yuv420:eof_action=pass:enable='not({sped})'[s{r}m]")
            chain = f"[s{r}m]null"
        expr = run_setpts_expr(run, fps)
        # a fine timebase, then rounding (+0.5) so 1x frames land exactly on their slot; one
        # cloned frame past the end, so fps always emits the last real frame (frames coming
        # out of overlay carry no duration, and fps would drop the final slot)
        chain += ",tpad=stop_mode=clone:stop=1,settb=1/60000"
        if expr:
            chain += f",setpts='({expr})/TB+0.5'"
        n = run.pre + run.out_frames + run.post
        chain += f",fps=fps={fps},tpad=stop_mode=clone:stop=8,trim=end_frame={n},setpts=PTS-STARTPTS[v{r}]"
        parts.append(chain)
    # join: xfade overlaps a run's post handle with the next run's pre handle
    tail = "[v0]"
    pos = rs[0].out_frames
    for r in range(1, len(rs)):
        w = rs[r - 1].post + rs[r].pre
        if w:
            parts.append(
                f"{tail}[v{r}]xfade=transition=fade:duration={_num(w / fps)}:offset={_num((pos - rs[r].pre) / fps)}[x{r}]"
            )
        else:
            parts.append(f"{tail}[v{r}]concat=n=2:v=1:a=0[x{r}]")
        tail = f"[x{r}]"
        pos += rs[r].out_frames
    parts.append(f"{tail}{finish}[{out_label}]")
    return ";\n".join(parts)


def video_inputs(rs: list[Run], fps: int) -> list[str]:
    """``-ss``/``-t``/``-i`` arguments, one input per run, handles included."""
    args: list[str] = []
    for run in rs:
        args += [
            "-ss",
            _num((run.src_start - run.pre) / fps),
            "-t",
            _num((run.src_frames + run.pre + run.post) / fps),
            "-i",
            run.source,
        ]
    return args


# --------------------------------------------------------------------------------------
# audio
# --------------------------------------------------------------------------------------


def _run_audio(run: Run, inp: int, r: int, fps: int, first_out: int, gain_db: float, sr: int) -> tuple[list[str], str]:
    """Filter chains for one run's audio, with its crossfade handles. Returns (chains, label)."""
    spf = samples_per_frame(fps, sr)
    fmt = _AFMT.format(sr=sr)
    pre, post = run.a_pre, run.a_post
    segs: list[tuple[int, int, float, int]] = []  # (src start, src samples, speed, out samples), run-relative
    if pre:
        segs.append((0, pre, 1.0, pre))
    a = pre
    for pc in run.pieces:
        segs.append((a, pc.src_frames * spf, pc.src_frames / pc.out_frames, pc.out_frames * spf))
        a += pc.src_frames * spf
    segs.append((a, post, 1.0, post))
    labels = [f"[a{r}_{q}]" for q in range(len(segs))]
    chains = [f"[{inp}:a:0]asetpts=PTS-STARTPTS,aresample={sr},{fmt},asplit={len(segs)}{''.join(labels)}"]
    outs = []
    for q, (s0, n, sp, n_out) in enumerate(segs):
        ch = f"{labels[q]}atrim=start_sample={s0}:end_sample={s0 + n},asetpts=PTS-STARTPTS"
        if abs(sp - 1.0) > 1e-9:
            ch += f",atempo={_num(sp)}"  # time-stretch, pitch preserved
        ch += f",apad=whole_len={n_out},atrim=end_sample={n_out},{fmt}[s{r}_{q}]"
        chains.append(ch)
        outs.append(f"[s{r}_{q}]")
    total = sum(x[3] for x in segs)
    fade = [
        f"afade=t=in:ss=0:ns={2 * pre}:curve=qsin" if pre else "afade=t=in:ss=0:ns=96:curve=qsin",
        f"afade=t=out:ss={total - 2 * post}:ns={2 * post}:curve=qsin",
    ]
    delay = first_out * spf - pre
    chains.append(
        f"{''.join(outs)}concat=n={len(outs)}:v=0:a=1,{','.join(fade)},volume={_num(gain_db)}dB,"
        f"adelay=delays={delay}S:all=1[run{r}]"
    )
    return chains, f"[run{r}]"


def audio_graph(
    rs: list[Run],
    fps: int,
    total_frames: int | None = None,
    gain_db: float = 0.0,
    sample_rate: int = SAMPLE_RATE,
    first_input: int = 0,
    out_label: str = "a",
) -> str:
    """Filter graph for the audio inputs made by :func:`audio_inputs` (run k is input
    ``first_input + k``): every run retimed, crossfaded and summed, ``total_frames`` long
    (default: the shots' length)."""
    total = (out_frames(rs) if total_frames is None else total_frames) * samples_per_frame(fps, sample_rate)
    parts: list[str] = []
    labels: list[str] = []
    pos = 0
    for r, run in enumerate(rs):
        chains, lab = _run_audio(run, first_input + r, r, fps, pos, gain_db, sample_rate)
        parts += chains
        labels.append(lab)
        pos += run.out_frames
    parts.append(
        f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:duration=longest:dropout_transition=0,"
        f"apad=whole_len={total},atrim=end_sample={total}[{out_label}]"
    )
    return ";\n".join(parts)


def audio_inputs(rs: list[Run], fps: int, sample_rate: int = SAMPLE_RATE) -> list[str]:
    """``-ss``/``-t``/``-i`` arguments, one input per run, audio handles included."""
    spf = samples_per_frame(fps, sample_rate)
    args: list[str] = []
    for run in rs:
        start = (run.src_start * spf - run.a_pre) / sample_rate
        dur = (run.src_frames * spf + run.a_pre + run.a_post) / sample_rate
        args += ["-ss", _num(start), "-t", _num(dur), "-i", run.source]
    return args


# --------------------------------------------------------------------------------------
# one command
# --------------------------------------------------------------------------------------


def command(
    shots: list[Shot],
    output: str,
    fps: int,
    transitions: Transitions | None = None,
    song: str | None = None,
    song_gain_db: float = 0.0,
    audio_gain_db: float = 0.0,
    ffmpeg: str = "ffmpeg",
    encode: list[str] | None = None,
) -> list[str]:
    """A complete ffmpeg command rendering the shots back to back with their audio. With
    ``song``, the song plays from its start under the shots' audio at ``song_gain_db``."""
    rs = runs(shots, fps, transitions)
    n = out_frames(rs)
    graph = (
        video_graph(shots, rs, fps) + ";\n" + audio_graph(rs, fps, gain_db=audio_gain_db, first_input=len(rs), out_label="clips")
    )
    inputs = video_inputs(rs, fps) + audio_inputs(rs, fps)
    total = n * samples_per_frame(fps, SAMPLE_RATE)
    if song:
        inputs += ["-i", song]
        graph += (
            f";\n[{2 * len(rs)}:a:0]{_AFMT.format(sr=SAMPLE_RATE)},volume={_num(song_gain_db)}dB,"
            f"apad=whole_len={total},atrim=end_sample={total}[song];\n"
            "[clips][song]amix=inputs=2:normalize=0:duration=first:dropout_transition=0[a]"
        )
    else:
        graph += ";\n[clips]anull[a]"
    enc = (
        encode if encode is not None else ["-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "256k"]
    )
    return [
        ffmpeg,
        "-hide_banner",
        "-y",
        *inputs,
        "-filter_complex",
        graph,
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-frames:v",
        str(n),
        *enc,
        output,
    ]
