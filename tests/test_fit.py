from __future__ import annotations

import random

import pytest

from beatfit import Clip, Event, Lead, Params, fit_clip, locate, max_src, min_out, ramp, realtime_ok, source_at
from beatfit.fit import _gap, _gap_holds

FPS = 60
BEAT = 0.5  # 120 BPM: a beat is exactly 30 frames
BEATS = [round(0.5 + BEAT * k, 6) for k in range(118)]
DOWNS = BEATS[::4]
BEAT_FRAMES = {round(b * FPS) for b in BEATS}
SUSPENSE = Lead(2.5, 5.0, 5.5, 2.5)


def clip(events_s: list[float], lead_from: float | None = None, lead: Lead | None = None, **kw) -> Clip:
    evs = [Event(round(t * FPS), id=f"e{i}") for i, t in enumerate(events_s)]
    return Clip(
        evs,
        max(0, evs[0].frame - 40 * FPS),
        evs[-1].frame + 8 * FPS,
        lead_from=round(lead_from * FPS) if lead_from is not None else None,
        lead=lead,
        **kw,
    )


def slot(b0: int, b1: int) -> tuple[int, int]:
    start = round(DOWNS[b0] * FPS)
    return start, round(DOWNS[b1] * FPS) - start


def check(c: Clip, start: int, n: int, f, p: Params) -> None:
    assert f is not None
    assert f.out_frames == n  # exact duration
    for pc in f.pieces:
        assert pc.out_frames > 0 and pc.src_frames > 0
        assert 1.0 - 1e-9 <= pc.speed <= p.max_speed + 1e-9  # never slow motion, never past the cap
        assert c.src_start <= pc.src and pc.src_end <= c.src_end
    for a, b in zip(f.pieces, f.pieces[1:]):  # continuous except at jump cuts, always forward
        assert (a.src_end == b.src) if not b.jump else (b.src > a.src_end)
    for e, o in zip(c.events, f.event_out):  # every event at 1x, where the solver says
        assert locate(f.pieces, e.frame, start) == start + o
        assert source_at(f.pieces, o) == e.frame
        assert next(pc for pc in f.pieces if pc.src <= e.frame < pc.src_end).speed == 1.0


def test_ramp_is_exact_eased_and_clamped():
    for M, D in [(400, 120), (90, 60), (250, 75), (61, 60), (400, 121)]:
        pcs = ramp(M, D, 4.0)
        assert sum(s for s, _ in pcs) == M and sum(o for _, o in pcs) == D
        speeds = [s / o for s, o in pcs]
        assert all(1.0 <= v <= 4.0 + 1e-9 for v in speeds)
        if len(pcs) >= 3:  # rises to the plateau, then falls
            k = speeds.index(max(speeds))
            assert speeds[:k] == sorted(speeds[:k]) and speeds[k + 1 :] == sorted(speeds[k + 1 :], reverse=True)
    with pytest.raises(ValueError):
        ramp(50, 60, 4.0)  # would be slow motion
    with pytest.raises(ValueError):
        ramp(1000, 60, 4.0)  # faster than the cap


def test_min_out_inverts_max_src():
    for M in (30, 100, 257, 500):
        D = min_out(M, 4.0, True, True, 5)
        assert max_src(D, 4.0, True, True, 5) >= M
        assert D == 1 or max_src(D - 1, 4.0, True, True, 5) < M


def test_spread_events_all_land_exactly_on_beats():
    p = Params()
    c = clip([100.0, 107.0, 115.0])  # gaps longer than speed_gap: both sped up
    start, n = slot(4, 12)
    f = fit_clip(c, start, n, BEATS, DOWNS, p)
    check(c, start, n, f, p)
    assert all(start + o in BEAT_FRAMES for o in f.event_out)
    assert all(f.event_on_beat) and max(f.event_err_ms) < 1.0
    assert 1.2 < f.max_speed <= p.max_speed


def test_short_gaps_stay_real_time_and_the_first_event_is_on_a_beat():
    p = Params()
    c = clip([50.0, 50.37, 54.1])
    start, n = slot(8, 12)
    f = fit_clip(c, start, n, BEATS, DOWNS, p)
    check(c, start, n, f, p)
    assert start + f.event_out[0] in BEAT_FRAMES
    assert f.event_out[2] - f.event_out[0] == c.events[2].frame - c.events[0].frame
    assert f.max_speed == 1.0


def test_holds_around_events_are_real_time():
    p = Params()
    c = clip([200.0, 207.0])
    start, n = slot(4, 9)
    f = fit_clip(c, start, n, BEATS, DOWNS, p)
    check(c, start, n, f, p)
    for e in c.events:
        pc = next(pc for pc in f.pieces if pc.src <= e.frame < pc.src_end)
        assert e.frame - pc.src >= p.f(p.pre_hold_min) or pc is f.pieces[0]
        assert pc.src_end - e.frame >= p.f(p.post_hold_min) or pc is f.pieces[-1]


def test_a_very_long_gap_gets_a_jump_cut():
    p = Params()
    c = clip([300.0, 360.0], lead=SUSPENSE)  # a minute apart
    start, n = slot(4, 8)
    f = fit_clip(c, start, n, BEATS, DOWNS, p)
    check(c, start, n, f, p)
    assert f.jumps == 1 and all(f.event_on_beat)


def test_lead_from_starts_the_clip_there_and_speeds_up_the_wait():
    p = Params(lead_jump_min=0.0)
    c = clip([500.0, 509.0], lead_from=488.0, lead=SUSPENSE)
    start, n = slot(12, 17)
    f = fit_clip(c, start, n, BEATS, DOWNS, p)
    check(c, start, n, f, p)
    assert f.pieces[0].src == c.lead_from and f.pieces[0].speed == 1.0
    assert any(pc.speed > 1.2 for pc in f.pieces if pc.src_end <= c.events[0].frame)


def test_a_short_lead_in_starts_later_instead_of_jump_cutting_at_once():
    p = Params()
    c = clip([500.0, 509.0], lead_from=488.0, lead=SUSPENSE)
    start, n = slot(12, 17)
    f = fit_clip(c, start, n, BEATS, DOWNS, p)
    check(c, start, n, f, p)
    assert f.lead_out < p.f(p.lead_jump_min)
    assert not any(pc.jump for pc in f.pieces[:2])


def test_an_impossible_slot_returns_none():
    c = clip([10.0, 25.0, 40.0, 55.0])
    start = round(DOWNS[4] * FPS)
    assert fit_clip(c, start, round(4 * BEAT * FPS), BEATS, DOWNS) is None


def test_events_must_be_in_order():
    with pytest.raises(ValueError):
        fit_clip(clip([10.0, 9.0]), *slot(4, 8), BEATS, DOWNS)


def test_event_protection_keeps_real_time_around_it():
    p = Params()
    base = clip([300.0, 330.0])  # 30 s apart: normally sped up and jump cut
    start, n = slot(4, 11)
    f = fit_clip(base, start, n, BEATS, DOWNS, p)
    assert f is not None and f.jumps == 1
    c = clip([300.0, 330.0])
    c.events[1].protect = (6.0, 1.0)  # the 6 s before the second event play in real time
    f = fit_clip(c, start, n, BEATS, DOWNS, p)
    check(c, start, n, f, p)
    assert realtime_ok(f.pieces, [(c.events[1].frame - 360, c.events[1].frame + 60)])
    pc = next(pc for pc in f.pieces if pc.src <= c.events[1].frame - 360 < pc.src_end)
    assert pc.speed == 1.0 and not pc.jump


def test_a_protected_range_inside_a_gap_is_kept_whole():
    p = Params()
    c = clip([300.0, 330.0])
    r = (round(312 * FPS), round(316 * FPS))  # something worth seeing in the middle of dead time
    c.protect = [r]
    start, n = slot(4, 11)
    f = fit_clip(c, start, n, BEATS, DOWNS, p)
    check(c, start, n, f, p)
    assert realtime_ok(f.pieces, [r])
    assert any(pc.src <= r[0] and r[1] <= pc.src_end for pc in f.pieces)  # shown, not cut away
    # the dead time on either side is still shortened
    assert f.jumps >= 1 and f.max_speed > 1.2


def test_a_range_around_the_first_event_is_shown_from_its_start():
    p = Params()
    c = clip([300.0])
    c.events[0].protect = (6.0, 0.5)  # longer than the default real-time lead-in (5 s)
    start, n = slot(4, 9)
    f = fit_clip(c, start, n, BEATS, DOWNS, p)
    check(c, start, n, f, p)
    assert f.pieces[0].src < c.events[0].frame - p.f(6.0) and f.max_speed > 1.2  # a sped approach, then 6 s of 1x
    assert realtime_ok(f.pieces, [(c.events[0].frame - p.f(6.0), c.events[0].frame)])


def test_gap_holds_cover_ranges_touching_or_inside_a_gap():
    assert _gap_holds(100, 1000, [(50, 160)]) == (60, 0)
    assert _gap_holds(100, 1000, [(900, 1010)]) == (0, 100)
    assert _gap_holds(100, 1000, [(200, 300)]) == (200, 0)  # nearer the start
    assert _gap_holds(100, 1000, [(700, 800)]) == (0, 300)  # nearer the end


def test_tail_min_keeps_the_aftermath_and_ends_on_a_bar_line():
    p = Params()
    c = clip([100.0, 100.5], tail_min=p.f(3.0))
    start, n = slot(4, 7)
    f = fit_clip(c, start, n, BEATS, DOWNS, p)
    check(c, start, n, f, p)
    assert f.tail_out >= p.f(3.0)


def test_random_clips_respect_all_constraints():
    rng = random.Random(7)
    p = Params()
    n_fit = 0
    for _ in range(300):
        k = rng.randint(1, 5)
        t = 100.0 + rng.uniform(0, 5)
        ts = [t]
        for _ in range(k - 1):
            t += rng.choice([rng.uniform(0.05, 1.2), rng.uniform(1.2, 8), rng.uniform(8, 40)])
            ts.append(t)
        c = clip(ts, lead=rng.choice([None, SUSPENSE, Lead(3.0, 5.5, 6.0, 2.5)]))
        if rng.random() < 0.3:  # sometimes a protected range somewhere in the clip
            a = rng.uniform(ts[0] - 3, ts[-1] + 1)
            c.protect = [(round(a * FPS), round((a + rng.uniform(0.5, 4)) * FPS))]
        if rng.random() < 0.2:
            c.events[rng.randrange(k)].protect = (rng.uniform(1, 6), rng.uniform(0, 2))
        b0, bars = rng.randint(0, 20), rng.randint(1, 6)
        start, n = slot(b0, b0 + bars)
        f = fit_clip(c, start, n, BEATS, DOWNS, p)  # raises if a protected range were broken
        if f is None:
            continue
        n_fit += 1
        check(c, start, n, f, p)
        assert start + f.event_out[0] in BEAT_FRAMES
        if c.protect or any(e.protect for e in c.events):
            continue
        # an event reached through a sped-up gap lands on a beat whenever one is reachable
        for j in range(1, k):
            g = _gap(c.events[j].frame - c.events[j - 1].frame, p)
            t0 = f.event_out[j - 1]
            reachable = [b for b in BEAT_FRAMES if g.Dmin <= b - start - t0 <= g.Dmax and b - start <= n - p.f(p.tail_min)]
            if not g.forced and reachable:
                assert start + f.event_out[j] in BEAT_FRAMES
    assert n_fit > 100


def test_other_frame_rates():
    p = Params(fps=30)
    evs = [Event(round(t * 30)) for t in (100.0, 107.0, 115.0)]
    c = Clip(evs, evs[0].frame - 1200, evs[-1].frame + 240)
    start = round(DOWNS[4] * 30)
    n = round(DOWNS[12] * 30) - start
    f = fit_clip(c, start, n, BEATS, DOWNS, p)
    assert f is not None and f.out_frames == n and all(f.event_on_beat)
