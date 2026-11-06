"""Fit a clip into a slot of a song so its events land on beats.

Everything is in integer frames at ``Params.fps`` (default 60), on two timelines: source
frames (the recording) and output frames (the song). A fit is a list of constant-speed
:class:`Piece` s.

Layout of one clip on the output timeline::

    [lead-in][event 1][gap][event 2] ... [event n][tail]
              ^ beat       ^ beat                ^ slot end

* Real time is the default. Only a gap between events longer than ``speed_gap`` gets its
  middle played faster: an eased ramp up to at most ``max_speed``, a plateau, an eased ramp
  down, with ``pre_hold`` / ``post_hold`` of 1x around each event. Shorter gaps play at 1x.
* Beat landing is a soft goal. The lead-in is chosen so the first event lands on a beat
  (preferably a downbeat); a sped-up gap takes the length that puts the next event on a
  beat; events after 1x gaps land where they land, and lead-ins that bring them near a beat
  are preferred.
* A gap still longer than ``max_gap_out`` of output at that speed gets a jump cut in the
  middle of its sped-up part: dead time is cut rather than rushed.
* Protected ranges (around an event, or given on the clip) always play in real time: no
  speed-up and no jump cut inside them.
* The tail after the last event is 1x and ends at the slot end.

The solver is a small dynamic program over (event, landing frame).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field

from .ramp import max_src, min_out, ramp


@dataclass
class Piece:
    """``src_frames`` source frames from ``src`` played over ``out_frames`` output frames."""

    src: int  # first source frame
    src_frames: int
    out_frames: int
    jump: bool = False  # the source jumps forward before this piece (a jump cut)

    @property
    def speed(self) -> float:
        return self.src_frames / self.out_frames

    @property
    def src_end(self) -> int:
        return self.src + self.src_frames


@dataclass
class Event:
    """A moment that should land on a beat, at source frame ``frame``."""

    frame: int
    soft: bool = False  # coarse timing: landing on a beat is nice to have
    protect: tuple[float, float] | None = None  # seconds of real time (before, after) the event
    id: str = ""


@dataclass
class Lead:
    """How a clip's lead-in (the part before the first event) prefers to look, in seconds."""

    pref_min: float = 2.5  # preferred length
    pref_max: float = 4.0
    max_realtime: float = 5.0  # longest lead-in played entirely at 1x
    hold: float = 2.0  # longer lead-ins: 1x for this long before the event, a gentle approach before that


@dataclass
class Clip:
    """Events from one stretch of a recording, in source frames."""

    events: list[Event]
    src_start: int  # earliest usable source frame
    src_end: int  # usable source ends here (exclusive)
    lead_from: int | None = None  # the lead-in should start here (e.g. where the suspense begins)
    lead: Lead | None = None  # defaults to Params.lead
    tail_min: int | None = None  # real-time tail after the last event: at least this many frames
    protect: list[tuple[int, int]] = field(default_factory=list)  # more [start, end) source ranges kept in real time


@dataclass
class Params:
    fps: int = 60  # frames per second of both timelines
    pre_hold: float = 1.2  # 1x before each event
    post_hold: float = 1.0  # 1x after each event
    pre_hold_min: float = 0.8  # holds may shrink to these to make a speed-up possible
    post_hold_min: float = 0.6
    max_speed: float = 1.5  # gentle; 2.0 at most is recommended
    speed_gap: float = 5.5  # gaps between events shorter than this always play at 1x
    min_mid: float = 1.5  # shortest middle (source s) worth speeding up
    ramp_step: int = 8  # output frames per ramp step (3 steps each side: ~0.4 s ramps at 60 fps)
    max_gap_out: float = 2.2  # longest sped-up middle (output s) before a jump cut
    jump_keep_speed: float = 1.4  # source kept around a jump cut = max_gap_out * this
    tail_min: float = 0.6
    tail_max: float = 3.5
    tail_pref: tuple[float, float] = (1.0, 2.2)
    last_tail_max: float = 7.0  # the final clip may ring out over the song's ending
    last_tail_pref: tuple[float, float] = (1.5, 5.0)
    lead_min: float = 2.5  # a clip never starts right on top of its first event
    lead_max: float = 10.0
    lead: Lead = field(default_factory=Lead)
    start_hold: float = 1.0  # 1x at the start of a sped-up lead-in (from ``lead_from``)
    lead_jump_min: float = 6.0  # a lead-in shorter than this has no jump cut
    approach_speed: float = 1.4  # very long lead-ins: gentle approach before the 1x hold
    offbeat_weight: float = 0.45  # cost of an event after a 1x gap landing half a beat off
    beats_per_bar: int = 4
    # with a clip's ``tail_min`` the tail may run on by up to ``tail_slack_bars`` bars (to
    # reach a phrase boundary), each extra second costing ``tail_extra_cost``
    tail_slack_bars: float = 1.6
    tail_extra_cost: float = 0.35

    @property
    def keep_speed(self) -> float:
        return min(self.jump_keep_speed, self.max_speed)

    def f(self, seconds: float) -> int:
        """Seconds to frames."""
        return int(round(seconds * self.fps))


@dataclass
class Fit:
    pieces: list[Piece]
    event_out: list[int]  # landing frame of each event, relative to the slot start
    event_on_beat: list[bool]
    event_err_ms: list[float]  # distance to the nearest beat (exact beat times)
    event_downbeat: list[bool]
    cost: float
    lead_out: int
    tail_out: int
    max_speed: float
    jumps: int

    @property
    def out_frames(self) -> int:
        return sum(p.out_frames for p in self.pieces)


# --------------------------------------------------------------------------------------
# protected ranges
# --------------------------------------------------------------------------------------


def protected(clip: Clip, p: Params) -> list[tuple[int, int]]:
    """Source ranges [start, end) that must play in real time, merged and sorted."""
    rs = [(int(s), int(e)) for s, e in clip.protect if e > s]
    for ev in clip.events:
        if ev.protect is not None:
            before, after = ev.protect
            rs.append((ev.frame - p.f(before), ev.frame + p.f(after)))
    out: list[tuple[int, int]] = []
    for s, e in sorted(rs):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _with_ranges(events: list[Event], prot: list[tuple[int, int]]) -> list[tuple[Event, bool]]:
    """The events plus a stand-in event at the start of each protected range lying wholly
    between two events: the gap is split there, so the dead time on either side can still
    be sped up. (event, real) pairs in source order."""
    out = [(e, True) for e in events]
    for s, e in prot:
        if any(a.frame < s and e < b.frame for a, b in zip(events, events[1:])):
            out.append((Event(s, soft=True), False))
    out.sort(key=lambda x: x[0].frame)
    return out


def realtime_ok(pieces: list[Piece], prot: Iterable[tuple[int, int]]) -> bool:
    """Whether every protected range, where the pieces show it, plays at 1x without a cut."""
    if not pieces:
        return True
    lo, hi = pieces[0].src, pieces[-1].src_end
    for s, e in prot:
        s, e = max(s, lo), min(e, hi)
        if s >= e:
            continue
        cov = s
        for pc in pieces:
            if pc.src <= cov < pc.src_end:
                if pc.src_frames != pc.out_frames:
                    return False
                cov = pc.src_end
                if cov >= e:
                    break
        if cov < e:
            return False
    return True


def _gap_holds(A: int, B: int, prot: list[tuple[int, int]]) -> tuple[int, int]:
    """Minimum 1x holds after A and before B so a gap (A, B) never speeds up or cuts inside
    a protected range."""
    h1 = max([e - A for s, e in prot if s <= A < e] or [0])
    h2 = max([B - s for s, e in prot if s < B <= e] or [0])
    for s, e in prot:
        if A < s and e < B:  # wholly inside the gap: extend the cheaper hold over it
            if e - A <= B - s:
                h1 = max(h1, e - A)
            else:
                h2 = max(h2, B - s)
    return h1, h2


# --------------------------------------------------------------------------------------
# gaps and lead-ins
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Gap:
    forced: bool  # too short to speed up: plays at 1x
    G: int  # source frames between the events
    h1: int  # 1x after the previous event
    h2: int  # 1x before the next event
    M: int  # middle source frames (before any jump cut)
    jump: bool  # too long even when sped up: the middle gets a jump cut
    Dmin: int
    Dmax: int

    def mid(self, D: int, p: Params) -> tuple[int, int]:
        """(source frames kept, source frames skipped) for a gap of D output frames."""
        if not self.jump:
            return self.M, 0
        Dm = D - self.h1 - self.h2
        keep = min(self.M, max(Dm, int(round(Dm * p.keep_speed))))
        keep = min(keep, max_src(Dm, p.max_speed, True, True, p.ramp_step))
        return keep, self.M - keep


def _gap(G: int, p: Params, first: bool = False, h1_min: int = 0, h2_min: int = 0) -> _Gap:
    h1 = max(p.f(p.start_hold if first else p.post_hold), h1_min)
    h2 = max(p.f(p.pre_hold), h2_min)
    M = G - h1 - h2
    if G < p.f(p.speed_gap) or p.max_speed <= 1.0:
        return _Gap(True, G, G, 0, 0, False, G, G)  # not dead time: real time
    if M < p.f(p.min_mid):
        # try the minimum holds before giving up on a speed-up
        h1m = max(min(h1, p.f(p.post_hold_min)), h1_min)
        h2m = max(min(h2, p.f(p.pre_hold_min)), h2_min)
        if G - h1m - h2m >= p.f(p.min_mid) + 6:
            h1, h2, M = h1m, h2m, G - h1m - h2m
        else:
            return _Gap(True, G, G, 0, 0, False, G, G)
    cap_out = p.f(p.max_gap_out)
    if M > int(cap_out * p.keep_speed):
        # a jump cut removes the middle; how much is kept follows the chosen length
        return _Gap(False, G, h1, h2, M, True, h1 + h2 + p.f(0.8), h1 + h2 + cap_out)
    Dmin = h1 + h2 + min_out(M, p.max_speed, True, True, p.ramp_step)
    return _Gap(False, G, h1, h2, M, False, Dmin, max(Dmin, h1 + h2 + M))


def _gap_pieces(src0: int, g: _Gap, D: int, p: Params) -> list[Piece]:
    """Pieces for a gap starting at source frame src0 (the previous event)."""
    if g.forced:
        return [Piece(src0, g.G, g.G)]
    out = [Piece(src0, g.h1, g.h1)]
    keep, skip = g.mid(D, p)
    mid = ramp(keep, D - g.h1 - g.h2, p.max_speed, True, True, p.ramp_step)
    s = src0 + g.h1
    jump_at = max(range(len(mid)), key=lambda i: mid[i][1]) if skip else -1  # cut in the plateau

    def cap(n: int) -> int:
        return int(math.floor(n * p.max_speed + 1e-9))

    for i, (sf, of) in enumerate(mid):
        if i == jump_at:
            # split near the middle where both halves stay within the speed cap
            a_out = min(range(1, of), key=lambda a: (cap(a) + cap(of - a) < sf, abs(a - of / 2))) if of > 1 else 0
            a_src = int(round(sf * a_out / of))
            a_src = max(sf - cap(of - a_out), min(a_src, cap(a_out)))
            if a_out > 0:
                out.append(Piece(s, a_src, a_out))
            s += a_src + skip
            out.append(Piece(s, sf - a_src, of - a_out, jump=True))
            s += sf - a_src
        else:
            out.append(Piece(s, sf, of))
            s += sf
    out.append(Piece(s, g.h2, g.h2))
    return out


def _gap_cost(g: _Gap, D: int, p: Params) -> float:
    keep, skip = g.mid(D, p)
    s = keep / max(1, D - g.h1 - g.h2)
    return 0.4 * math.log(max(s, 1.0) / p.max_speed) ** 2 + (0.3 if skip else 0.0)


def _range_cost(x: float, lo: float, hi: float, per_s: float) -> float:
    return per_s * (lo - x if x < lo else (x - hi if x > hi else 0.0))


@dataclass
class _Lead:
    L: int
    cost: float
    pieces: list[Piece]


def _lead(c: Clip, L: int, p: Params, prot: list[tuple[int, int]]) -> _Lead | None:
    """Lead-in of L output frames ending exactly at the first event."""
    ld = _lead_raw(c, L, p, prot)
    if ld is None or not prot:
        return ld
    A1 = c.events[0].frame
    # a range around the first event is shown from its start
    if any(ld.pieces[0].src > s for s, e in prot if s < A1 <= e):
        return None
    return ld if realtime_ok(ld.pieces, prot) else None


def _lead_raw(c: Clip, L: int, p: Params, prot: list[tuple[int, int]]) -> _Lead | None:
    A1 = c.events[0].frame
    lp = c.lead or p.lead
    lo, hi = lp.pref_min, lp.pref_max
    if c.lead_from is not None and A1 - c.lead_from > p.f(p.start_hold + p.pre_hold + p.min_mid):
        g = _gap(A1 - c.lead_from, p, True, *_gap_holds(c.lead_from, A1, prot))
        # (a short lead-in does not jump cut right after the clip has started: it starts later)
        if not g.forced and g.Dmin <= L <= g.Dmax and (not g.jump or L >= p.f(p.lead_jump_min)):
            pieces = _gap_pieces(c.lead_from, g, L, p)
            return _Lead(L, _gap_cost(g, L, p) + _range_cost(L / p.fps, lo, hi, 0.35), pieces)
    cost = _range_cost(L / p.fps, lo, hi, 0.35)
    if c.lead_from is not None and A1 - L > c.lead_from:
        cost += 1.0  # the lead-in does not show where the suspense starts
    need = max([A1 - s for s, e in prot if s < A1 <= e] or [0])  # real time protected before the event
    if L <= max(p.f(lp.max_realtime), need):
        s0 = A1 - L
        if s0 < c.src_start:
            return None
        return _Lead(L, cost, [Piece(s0, L, L)])
    # long build-up: a sped approach easing into a 1x hold before the event
    h = max(p.f(lp.hold), need)
    Da = L - h
    Ma = min(int(Da * min(p.approach_speed, p.max_speed)), A1 - h - c.src_start)
    if Ma < Da:
        return None
    Ma = min(Ma, max_src(Da, p.max_speed, False, True, p.ramp_step))
    s = A1 - h - Ma
    pieces = []
    for sf, of in ramp(Ma, Da, p.max_speed, False, True, p.ramp_step):
        pieces.append(Piece(s, sf, of))
        s += sf
    pieces.append(Piece(s, h, h))
    return _Lead(L, cost + 0.1, pieces)


# --------------------------------------------------------------------------------------
# the solver
# --------------------------------------------------------------------------------------


def _merge(pieces: list[Piece]) -> list[Piece]:
    out: list[Piece] = []
    for pc in pieces:
        if pc.out_frames <= 0:
            continue
        if (
            out
            and not pc.jump
            and out[-1].src_end == pc.src
            and out[-1].src_frames == out[-1].out_frames
            and pc.src_frames == pc.out_frames
        ):
            out[-1] = Piece(out[-1].src, out[-1].src_frames + pc.src_frames, out[-1].out_frames + pc.out_frames, out[-1].jump)
        else:
            out.append(Piece(pc.src, pc.src_frames, pc.out_frames, pc.jump))
    return out


def fit_clip(
    clip: Clip,
    slot_start: int,
    slot_frames: int,
    beats: list[float],
    downbeats: Iterable[float] = (),
    p: Params | None = None,
    last: bool = False,
    music_end: float | None = None,
) -> Fit | None:
    """Best fit of ``clip`` into output frames [slot_start, slot_start + slot_frames).

    ``beats`` / ``downbeats``: the song's beat times in seconds (output time), sorted.
    ``last``: the final clip, whose tail may ring out longer.
    ``music_end``: events landing after the music has stopped are penalised.
    Returns None when the clip cannot fill the slot within the constraints."""
    p = p or Params()
    fps = p.fps
    if not clip.events:
        return None
    if any(b.frame <= a.frame for a, b in zip(clip.events, clip.events[1:])):
        raise ValueError("events must be in increasing source order")
    prot = protected(clip, p)
    marked = _with_ranges(clip.events, prot)
    ev = [e for e, _ in marked]
    real = [r for _, r in marked]
    down = {round(d, 4) for d in downbeats}
    bl: list[tuple[int, bool]] = []  # beats inside the slot: (relative frame, downbeat)
    for b in beats:
        rel = int(round(b * fps)) - slot_start
        if 0 < rel < slot_frames:
            bl.append((rel, round(b, 4) in down))
    beat_at = dict(bl)
    period = (beats[1] - beats[0]) if len(beats) > 1 else 0.5
    An = ev[-1].frame
    if clip.tail_min is not None:
        bar = p.f(p.beats_per_bar * period)
        avail = clip.src_end - An
        req = clip.tail_min
        if avail < req + bar // 2:  # limited by the footage: may end up to half a bar earlier
            req = min(req, max(p.f(p.tail_min), avail - bar // 2))
        tail_min = max(p.f(p.tail_min), req)
        tail_max = max(tail_min + p.f(p.tail_slack_bars * p.beats_per_bar * period), p.f(p.last_tail_max) if last else 0)
        tpref = (tail_min / fps, tail_min / fps)
        tail_cost = 0.1 if last else p.tail_extra_cost
    else:
        tail_min = p.f(p.tail_min)
        tail_max = p.f(p.last_tail_max if last else p.tail_max)
        tpref = p.last_tail_pref if last else p.tail_pref
        tail_cost = 0.5
    half = period * fps / 2

    def err_ms(rel: int) -> float:
        if not beats:
            return 0.0
        t = (slot_start + rel) / fps
        return min(abs(t - b) for b in beats) * 1000.0

    n = len(ev)

    def event_cost(j: int, rel: int) -> float:
        if not real[j]:
            return 0.0  # the start of a protected range: anywhere will do
        if music_end is not None and (slot_start + rel) / fps > music_end + 0.1:
            return 4.0  # in the silence after the song: no beat to hit
        if rel not in beat_at:
            w = 0.25 if ev[j].soft else (1.2 if j == 0 else p.offbeat_weight)
            return w * min(1.0, err_ms(rel) / 1000.0 * fps / half)
        if beat_at[rel] and (j == 0 or j == n - 1):
            return -0.45 if ev[j].soft else -0.3
        return 0.0

    lead_min, lead_max = p.f(p.lead_min), p.f(p.lead_max)
    # dp[j]: landing frame -> (cost, previous landing, payload)
    dp: list[dict[int, tuple[float, int | None, object]]] = [{} for _ in range(n)]
    for rel, _d in bl:
        if rel < lead_min or rel > lead_max or rel > slot_frames - tail_min:
            continue
        ld = _lead(clip, rel, p, prot)
        if ld is not None:
            dp[0][rel] = (ld.cost + event_cost(0, rel), None, ld)
    for j in range(1, n):
        A, B = ev[j - 1].frame, ev[j].frame
        g = _gap(B - A, p, False, *_gap_holds(A, B, prot))
        for T0, (c0, _pv, _pl) in dp[j - 1].items():
            opts: list[tuple[int, float]] = []
            if g.forced:
                opts.append((T0 + g.G, 0.0))
            else:
                for rel, _d in bl:
                    if g.Dmin <= rel - T0 <= g.Dmax:
                        opts.append((rel, _gap_cost(g, rel - T0, p)))
                if not opts or not real[j]:  # no beat reachable: fastest / slowest allowed, off-beat
                    for D in (g.Dmin, g.Dmax):
                        opts.append((T0 + D, _gap_cost(g, D, p) + (0.5 if real[j] else 0.0)))
            for T, sc in opts:
                if T > slot_frames - tail_min:
                    continue
                c = c0 + sc + event_cost(j, T)
                cur = dp[j].get(T)
                if cur is None or c < cur[0]:
                    dp[j][T] = (c, T0, g)
    best: tuple[float, int] | None = None
    for T, (c, _pv, _pl) in dp[n - 1].items():
        R = slot_frames - T
        if R < tail_min or R > tail_max or An + R > clip.src_end:
            continue
        tc = c + _range_cost(R / fps, tpref[0], tpref[1], tail_cost)
        if best is None or tc < best[0]:
            best = (tc, T)
    if best is None:
        return None

    lands = [0] * n
    payloads: list[object] = [None] * n
    T = best[1]
    for j in range(n - 1, -1, -1):
        _c, prev, pl = dp[j][T]
        lands[j], payloads[j] = T, pl
        if prev is not None:
            T = prev
    lead: _Lead = payloads[0]  # type: ignore[assignment]
    pieces: list[Piece] = list(lead.pieces)
    for j in range(1, n):
        g = payloads[j]  # type: ignore[assignment]
        pieces += _gap_pieces(ev[j - 1].frame, g, lands[j] - lands[j - 1], p)  # type: ignore[arg-type]
    R = slot_frames - lands[-1]
    pieces.append(Piece(An, R, R))
    pieces = _merge(pieces)
    # internal consistency guards
    if sum(pc.out_frames for pc in pieces) != slot_frames:
        raise AssertionError(f"fit produced {sum(pc.out_frames for pc in pieces)} frames for a {slot_frames}-frame slot")
    if not realtime_ok(pieces, prot):
        raise AssertionError("fit speeds up or cuts a protected range")
    lands = [t for t, r in zip(lands, real) if r]
    return Fit(
        pieces=pieces,
        event_out=lands,
        event_on_beat=[t in beat_at for t in lands],
        event_err_ms=[round(err_ms(t), 2) for t in lands],
        event_downbeat=[bool(beat_at.get(t, False)) for t in lands],
        cost=round(best[0], 4),
        lead_out=lands[0],
        tail_out=R,
        max_speed=round(max(pc.speed for pc in pieces), 3),
        jumps=sum(1 for pc in pieces if pc.jump),
    )


def locate(pieces: list[Piece], src_frame: int, out_start: int = 0) -> int:
    """Output frame showing source frame ``src_frame`` (the first piece containing it)."""
    o = out_start
    for pc in pieces:
        if pc.src <= src_frame < pc.src_end:
            return o + int(math.floor((src_frame - pc.src) / pc.speed + 1e-9))
        o += pc.out_frames
    raise ValueError(f"source frame {src_frame} is not in the clip")


def source_at(pieces: list[Piece], out_frame: int) -> int:
    """Source frame shown at output frame ``out_frame`` (relative to the clip start)."""
    o = 0
    for pc in pieces:
        if out_frame < o + pc.out_frames:
            return pc.src + int(math.floor((out_frame - o) * pc.speed + 1e-9))
        o += pc.out_frames
    raise ValueError(f"output frame {out_frame} is past the clip ({o} frames)")
