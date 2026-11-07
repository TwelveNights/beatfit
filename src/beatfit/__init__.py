"""Retime video clips so chosen moments land on the beats of a song."""

from .fit import Clip, Event, Fit, Lead, Params, Piece, fit_clip, locate, protected, realtime_ok, source_at
from .ramp import max_src, min_out, ramp

__version__ = "0.2.0"

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
