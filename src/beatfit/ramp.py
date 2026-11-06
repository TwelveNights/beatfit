"""Eased speed ramps made of a few constant-speed steps.

A sped-up stretch eases from 1x up to a plateau and back down in at most three steps per
side, so a renderer only ever deals with constant-speed pieces.
"""

from __future__ import annotations

import math
from functools import lru_cache

_EASE = {0: (), 1: (0.5,), 2: (0.3, 0.75), 3: (0.18, 0.5, 0.82)}


def _nsteps(D: int, sides: int, step: int) -> int:
    for n in (3, 2, 1):
        if D - n * sides * step >= max(6, 2 * step):
            return n
    return 0


def _layout(D: int, up: bool, down: bool, step: int, n: int | None = None) -> tuple[list[int], list[float]]:
    """Output frames and ease fraction (0 = 1x, 1 = plateau) of each piece of a ramp."""
    sides = int(up) + int(down)
    if n is None:
        n = _nsteps(D, sides, step) if sides else 0
    e = _EASE[n]
    outs: list[int] = []
    eases: list[float] = []
    if up:
        outs += [step] * n
        eases += list(e)
    outs.append(D - step * n * sides)
    eases.append(1.0)
    if down:
        outs += [step] * n
        eases += list(reversed(e))
    return outs, eases


def _cap(outs: list[int], eases: list[float], smax: float) -> int:
    return int(math.floor(sum(n * (1 + (smax - 1) * e) for n, e in zip(outs, eases)) + 1e-9))


@lru_cache(maxsize=200_000)
def max_src(D: int, smax: float, up: bool = True, down: bool = True, step: int = 5) -> int:
    """Most source frames a fully eased stretch of ``D`` output frames can play at no more
    than ``smax``. (:func:`ramp` itself falls back to fewer steps, down to a constant
    speed, when it has to.)"""
    if D <= 0:
        return 0
    return _cap(*_layout(D, up, down, step), smax)


@lru_cache(maxsize=200_000)
def min_out(M: int, smax: float, up: bool = True, down: bool = True, step: int = 5) -> int:
    """Fewest output frames an eased stretch needs to play ``M`` source frames."""
    if M <= 0:
        return 0
    D = max(1, math.ceil(M / smax))
    while max_src(D, smax, up, down, step) < M:
        D += 1
    return D


def ramp(M: int, D: int, smax: float, up: bool = True, down: bool = True, step: int = 5) -> list[tuple[int, int]]:
    """Split ``M`` source frames into eased constant-speed pieces filling ``D`` output
    frames: ``[(src_frames, out_frames), ...]``.

    Requires ``D <= M <= D * smax`` (never slow motion, never faster than ``smax``). Uses
    the most ramp steps (``step`` output frames each) that still reach the needed speed;
    ``up`` / ``down`` choose which ends ease from 1x."""
    if D <= 0:
        return []
    if M < D:
        raise ValueError(f"ramp would slow down: {M} src frames into {D} out frames")
    if M > math.floor(D * smax + 1e-9):
        raise ValueError(f"ramp too fast: {M} src frames into {D} out frames at <= {smax}x")
    if M == D or M / D < 1.05 or not (up or down):
        return [(M, D)]
    sides = int(up) + int(down)
    for n in range(_nsteps(D, sides, step), -1, -1):
        outs, eases = _layout(D, up, down, step, n)
        if M <= _cap(outs, eases, smax):
            break
    if len(outs) == 1:
        return [(M, D)]
    a = sum(n * (1 - e) for n, e in zip(outs, eases))
    b = sum(n * e for n, e in zip(outs, eases))
    sp = (M - a) / b
    speeds = [1 + (sp - 1) * e for e in eases]
    # integer source frames, cumulative rounding so they sum to M exactly
    srcs: list[int] = []
    acc = 0.0
    prev = 0
    for n, s in zip(outs, speeds):
        acc += n * s
        cur = int(round(acc))
        srcs.append(cur - prev)
        prev = cur
    srcs[-1] += M - sum(srcs)
    pi = eases.index(1.0)
    for i, (sv, n) in enumerate(zip(srcs, outs)):
        if sv < n:  # never below 1x: take the difference from the plateau
            srcs[pi] -= n - sv
            srcs[i] = n
    # rounding must never push a piece past the speed cap: move the excess to pieces with
    # headroom (fastest-allowed first: the plateau, then the inner ramp steps)
    caps = [int(math.floor(n * smax + 1e-9)) for n in outs]
    excess = sum(max(0, sv - c) for sv, c in zip(srcs, caps))
    srcs = [min(sv, c) for sv, c in zip(srcs, caps)]
    for i in sorted(range(len(outs)), key=lambda i: -eases[i]):
        if excess <= 0:
            break
        take = min(caps[i] - srcs[i], excess)
        srcs[i] += take
        excess -= take
    return [(s, n) for s, n in zip(srcs, outs) if n > 0]
