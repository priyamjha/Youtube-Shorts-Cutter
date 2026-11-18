# Shorts Cutter

Paste a YouTube link, get a folder of ready-to-upload Shorts. Everything is
free and runs on your own computer: no accounts, no API keys, no uploads.

## Folders

| What | Where |
|---|---|
| Project (code) | `~/shorts-cutter/` |
| Finished shorts | `~/shorts-output/<video title> [id]/short_001.mp4 …` |
| Downloaded source videos | `~/shorts-cutter/data/downloads/` (kept so you can export more later) |

## Start

```bash
cd ~/shorts-cutter
./run.sh
```

Your browser opens at http://127.0.0.1:5055. Leave the terminal window open while you use it.
First run: `./setup.sh` installs FFmpeg (Homebrew) and the Python packages.

## Stop

Press **Ctrl + C** in the terminal where it is running.

If the terminal is already closed and the server is still running in the
background (or `./run.sh` says "address already in use"), stop it with:

```bash
lsof -ti :5055 | xargs kill
```

Then start it again with `cd ~/shorts-cutter && ./run.sh`.

## How it works

1. **Download** the video with `yt-dlp`.
2. **Find pauses** in speech with FFmpeg's silence detector (fast, audio only).
3. **Find scene changes** with PySceneDetect (optional, slow on long videos).
4. **Plan** shorts: each short is at most *Max length* seconds, cut at the latest
   pause inside the window, else at a scene change, else a hard cut.
5. **Export** only the shorts you choose: all, a range (e.g. 20–25), or the
   next 5/10/25. Every export re-encodes with FFmpeg into a vertical 9:16 video
   (blurred background by default) so YouTube treats it as a Short.

Planned shorts are remembered in `data/jobs.json`, so you can close the app and
export the next batch tomorrow without downloading or analyzing again.

## Livestreams

- Past livestreams (replays): work like a normal video.
- A stream that is still live: it records from the start, and the download only
  finishes when the stream ends. Some streams disable rewinding, so the start may be missing.
- Scheduled streams that have not started: cannot be downloaded yet.

## Settings

| Setting | Default | Notes |
|---|---|---|
| Max length | 60 s | YouTube Shorts allow up to 180 s |
| Min length | 15 s | Shorter leftovers are dropped |
| Silence threshold | -30 dB | Lower (e.g. -40) = only very quiet pauses count |
| Scene detection | on | Turn off for a much faster analysis on multi-hour videos |
| Download quality | 1080p | 720p is faster and plenty for 9:16 output |
| Output format | 9:16 blur | or 9:16 center crop, or original landscape |

## Rough timings (Apple Silicon, 1080p source)

- Download: depends on your internet (a 4-hour video is 4–8 GB at 1080p).
- Silence detection: about 1 minute per 2 hours of video.
- Scene detection: about 10–20 minutes per hour of video. Skip it if you are in a hurry.
- Export: a few seconds per short with the hardware encoder.

## Copyright

Only clip videos you own or have permission to reuse. Reposting someone else's
content without permission can get your channel struck or removed.
