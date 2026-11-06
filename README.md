# beatfit

Retime a video clip to fill part of a song so that chosen moments in it land on beats.

You give it the moments (as source frames), the song's beat times and the stretch of the
song the clip has to fill. It returns the clip as a list of constant-speed pieces.

Footage plays in real time unless the gap between two moments is longer than 5.5 s. Then
the middle of the gap is sped up to at most 1.5x, with short eased ramps and about a second
of real time either side of each moment. The lead-in is picked so the first moment falls on
a beat, a downbeat if possible, and each sped-up gap gets whatever length puts the next
moment on a beat. If a gap is still too long, its sped-up middle gets a jump cut. With the
default settings that happens to every gap long enough to speed up; raise `max_gap_out` to
speed up without cutting. Ranges you mark as protected are never sped up or cut.

The result fills the requested number of frames exactly and never plays slower than real
time or faster than `max_speed`.

beatfit doesn't analyse audio. Use a beat tracker such as
[beat_this](https://github.com/CPJKU/beat_this), [All-In-One](https://github.com/mir-aidj/all-in-one)
or [librosa](https://librosa.org) for beats and downbeats. It also doesn't decide which clip
goes where in the song; `fit_clip` returns `None` when a clip can't fill a slot, so you can
try clips against slots and keep the ones that fit.

Pure Python with no dependencies, 3.10 or newer.

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

To render it with ffmpeg (video only):

```python
import subprocess
from beatfit.ffmpeg import command

subprocess.run(command("recording.mp4", fit.pieces, fps, "clip.mp4"), check=True)
```

`beatfit.ffmpeg.filter_graph` and `input_args` return the parts of that command, for use
in a larger filter graph.

## Concepts

All times are integer frames at `Params.fps` (default 60) on two timelines: source (the
recording) and output (the song). Beats are in seconds on the output timeline. A source
frame is `1 / fps` seconds whatever the recording's own frame rate, so a 30 fps recording
works with `fps=60`.

A `Clip` has `events` in increasing source order and the usable source range
`[src_start, src_end)`. Optional fields:

| Field | Meaning |
|---|---|
| `lead_from` | Start the lead-in here, e.g. where the tension begins. A long wait is sped up after a moment of real time. |
| `lead` | `Lead(pref_min, pref_max, max_realtime, hold)`: preferred lead-in length in seconds, the longest lead-in played entirely in real time, and the real time kept before the first event when the lead-in is longer than that. |
| `tail_min` | Frames of real time after the last event, e.g. for a reaction. The tail then ends on a bar line, up to `tail_slack_bars` later. |
| `protect` | Source ranges `[start, end)` that always play in real time. |

`Event.protect = (before, after)` protects that many seconds around one event.
`Event.soft` marks an event whose timing is approximate, so putting it on a beat is
optional.

A protected range between two events is always shown in real time, and the dead time on
either side of it can still be sped up or cut. A range around the first event is shown from
its start: the clip never begins inside it, and the lead-in stays in real time as long as
the range needs. A range earlier than that is shown only if the lead-in reaches back to it.
A clip can end before a range around its last event is over.

`Params` holds the other settings: real time around events (`pre_hold`, `post_hold`), the
speed cap (`max_speed`), the shortest gap that gets sped up (`speed_gap`), the longest
sped-up stretch before a jump cut (`max_gap_out`), lead-in and tail limits, and
`beats_per_bar`. `last=True` allows a longer tail for the final clip of a song, and
`music_end` penalises events that would land after the music stops.

A `Fit` has the `pieces`; each event's output frame relative to the slot (`event_out`);
whether it is on a beat or downbeat and how far it is from the nearest beat
(`event_err_ms`); and `max_speed`, `jumps`, `lead_out`, `tail_out` and `cost`. `locate`
and `source_at` convert between the two timelines.

## How it works

A dynamic program over (event, landing frame), where landing frames are the beats inside
the slot. Gaps shorter than `speed_gap` play in real time, so the event after one lands
wherever that puts it; the solver favours lead-ins that bring those events close to a beat.
A speed-up is up to three eased steps on each side of a constant plateau, so renderers and
editors only ever see constant speeds.

## Limits

- It fits one clip into one slot. Choosing and ordering clips is up to you.
- Times are whole frames, so an event lands within half a frame of its beat (8 ms at 60 fps).
- The ffmpeg helper renders video only. Retiming or muting the audio is up to you.
- It was written for game highlight montages at 60 fps. Other footage and tempos have only
  been tested with synthetic data.

## Related work

- [visbeat](https://github.com/abedavis/visbeat) (Davis & Agrawala, "Visual Rhythm and
  Beat", SIGGRAPH 2018) warps video so visible motion lines up with musical beats. beatfit
  takes the moments as given and keeps real time around them.
- [mugen](https://github.com/scherroman/mugen) and similar tools cut video to the beat.
  They put cuts on beats rather than moving moments inside a clip.
- [otio-music-arrangement](https://pypi.org/project/otio-music-arrangement/) builds
  beat-aligned OpenTimelineIO timelines from placeholder clips.

## Releasing

Set the new version in `pyproject.toml` and `src/beatfit/__init__.py`, commit, then tag
and push:

```
git tag v0.1.1
git push origin v0.1.1
```

`.github/workflows/release.yml` runs the tests on Python 3.10 and 3.13, checks the tag
matches both version numbers, builds, and publishes to PyPI through trusted publishing.

## License

MIT
