# beatfit

Fit video clips to music. You say which moments in a clip matter (a goal, a trick, a
kill, a punchline) and which bars of the song the clip should fill. beatfit decides how
the clip is retimed so those moments land on beats:

- **Real time by default.** Only long dead time between moments is sped up, gently
  (1.5x by default), with eased ramps and real-time holds around each moment.
- **Moments on beats.** The lead-in is chosen so the first moment lands on a beat
  (a downbeat if possible); a sped-up gap takes the length that puts the next moment on
  one.
- **Cut, don't rush.** A gap too long even when sped up gets a jump cut in its middle.
  With the defaults that is every gap long enough to speed up (over 5.5 s); raise
  `max_gap_out` to speed up without cutting.
- **Protected ranges** always play in real time: no speed-up and no cut inside them.
- **Exact.** Everything is integer frames; a fit fills its slot to the frame, never plays
  slower than real time and never faster than the cap.

It does not analyse music. Get beats and downbeats from any beat tracker
([beat_this](https://github.com/CPJKU/beat_this), [All-In-One](https://github.com/mir-aidj/all-in-one),
[librosa](https://librosa.org), ...). It does not choose which clip goes in which slot either:
that is your arrangement. `fit_clip` returns `None` when a clip cannot fill a slot, so trying
clips against slots is cheap.

No dependencies. Python 3.10+.

## Example

```python
from beatfit import Clip, Event, Params, fit_clip

fps = 60
beats = [0.5 + 0.5 * k for k in range(200)]  # 120 BPM, from your beat tracker (seconds)
downbeats = beats[::4]

# moments at 12.0 s, 13.1 s and 31.5 s of the recording (source frames at `fps`)
clip = Clip(
    events=[Event(12 * fps), Event(round(13.1 * fps)), Event(round(31.5 * fps), protect=(4.0, 1.0))],
    src_start=0,             # usable footage
    src_end=40 * fps,
)
start = round(downbeats[4] * fps)          # fill bars 4-12 of the song
frames = round(downbeats[12] * fps) - start
fit = fit_clip(clip, start, frames, beats, downbeats, Params(fps=fps))

for p in fit.pieces:  # constant-speed pieces: source frames -> output frames
    print(p.src, p.src_frames, p.out_frames, round(p.speed, 3), "jump" if p.jump else "")
print(fit.event_out, fit.event_on_beat, fit.event_err_ms)
```

Render it with ffmpeg (video only):

```python
import subprocess
from beatfit.ffmpeg import command

subprocess.run(command("recording.mp4", fit.pieces, fps, "clip.mp4"), check=True)
```

`beatfit.ffmpeg.filter_graph` and `input_args` give the pieces of that command if you are
building a larger graph.

## Concepts

All times are integer frames at `Params.fps` (default 60) on two timelines: **source**
(the recording) and **output** (the song). Beats are seconds on the output timeline.
Source frames are `1 / fps` seconds whatever the recording's own frame rate, so a 30 fps
recording works with `fps=60`.

A `Clip` has `events` in increasing source order, the usable source range `[src_start,
src_end)`, and optionally:

| Field | Meaning |
|---|---|
| `lead_from` | The lead-in should start here: where the tension begins. A long wait is sped up, with a moment of real time at its start. |
| `lead` | `Lead(pref_min, pref_max, max_realtime, hold)`: preferred lead-in length (s), the longest lead-in played entirely in real time, and the real-time hold before the first event of a longer one. |
| `tail_min` | Frames of real time after the last event, e.g. a reaction. The tail then ends on a bar line, up to `tail_slack_bars` later. |
| `protect` | Source ranges `[start, end)` always played in real time. |

`Event.protect = (before, after)` protects seconds around one event; `Event.soft` marks
events whose timing is coarse, so landing them on a beat is optional.

A range between two events is always shown, in real time, and the dead time on either side of
it can still be sped up or cut. A range around the first event is shown from its start: a fit
never begins inside it, and the lead-in plays in real time for as long as the range needs.
A range before that is shown only if the lead-in reaches it, and a clip may end before a range
around its last event is over.

`Params` holds every other rule: holds around events (`pre_hold`, `post_hold`), the speed
cap (`max_speed`), the shortest gap worth speeding up (`speed_gap`), the longest sped-up
stretch before a jump cut (`max_gap_out`), lead-in and tail limits, and `beats_per_bar`.
`last=True` lets the final clip ring out over the song's ending, and `music_end` penalises
events after the music stops.

A `Fit` has the `pieces`, each event's output frame relative to the slot (`event_out`),
whether it is on a beat or downbeat and how far from the nearest beat (`event_err_ms`), and
summary numbers (`max_speed`, `jumps`, `lead_out`, `tail_out`, `cost`). `locate` and
`source_at` map between the timelines.

## How it works

A small dynamic program over (event, landing frame). Landing frames are beats inside the
slot. Gaps shorter than `speed_gap` play in real time, so the next event lands wherever that
puts it, and the solver prefers lead-ins that bring those events near a beat. Speed-ups are
up to three eased steps a side and a plateau, all constant-speed pieces, which keeps
renderers and editors simple.

## Limits

- One clip, one slot. Choosing and ordering clips is up to you.
- Integer frames: events land within half a frame of a beat (8 ms at 60 fps).
- The ffmpeg helper is video only. Retiming audio (or muting the sped-up parts) is up to you.
- Developed for game highlight montages at 60 fps. Other footage and tempos are tested only
  synthetically.

## Related work

- [visbeat](https://github.com/abedavis/visbeat) (Davis & Agrawala, "Visual Rhythm and
  Beat", SIGGRAPH 2018) warps video so visual motion beats match musical beats. beatfit
  takes the moments as given and keeps real time around them.
- [mugen](https://github.com/scherroman/mugen) and similar tools cut video to the beat:
  they place cuts on beats, not moments inside clips.
- [otio-music-arrangement](https://pypi.org/project/otio-music-arrangement/) builds
  beat-aligned OpenTimelineIO timelines from placeholder clips.

## License

MIT
