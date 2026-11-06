"""Fit video clips to music: land chosen moments on beats, keep real time where it matters,
and speed up only long dead time, gently."""

from .fit import Clip, Event, Fit, Lead, Params, Piece, fit_clip, locate, protected, realtime_ok, source_at
from .ramp import max_src, min_out, ramp

__version__ = "0.1.0"

__all__ = [
    "Clip",
    "Event",
    "Fit",
    "Lead",
    "Params",
    "Piece",
    "fit_clip",
    "locate",
    "max_src",
    "min_out",
    "protected",
    "ramp",
    "realtime_ok",
    "source_at",
]
