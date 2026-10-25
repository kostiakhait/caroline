---
name: analyzing-video
description: How to understand what happens in a video file over time -- your own screen recording or any video the user points you at -- by sampling frames and transcribing the audio track, instead of a single static screenshot. Use this whenever a video's motion/dynamics or spoken content matters, not just one still moment.
---

# Analyzing video

A single screenshot only shows one instant. When you need to see something change over TIME --
an animation, a multi-step UI flow, a game, a recording of something happening, or any video file
the user shares with you -- use `sample_video_frames` (and, if the video has speech worth
understanding, `speech_to_text`) instead.

This works on **any video file**, not just your own recordings -- `sample_video_frames` and
`speech_to_text` both just take a file path. If the user attaches or points you at a video, you
can analyze it the exact same way described here.

## Recording your own screen

If you need to capture something happening live (not an existing file):

1. `start_screen_recording` -- begins capturing the full desktop.
2. Do/wait for whatever you need to observe.
3. `stop_screen_recording` -- finalizes the MP4 and gives you its path.

Then analyze the resulting file exactly as below.

## Seeing the visuals: sample_video_frames

Extracts a handful of frames at a fixed interval and returns them as images, in order, so you can
compare frame-to-frame changes directly instead of guessing from one still.

- `everyNthFrame` controls how far apart the sampled frames are (source video is 30fps -- 30 =
  ~1 frame/sec, 15 = ~2 frames/sec, 90 = ~1 frame every 3 sec). Pick this based on how fast the
  thing you care about changes -- fast motion needs a smaller number, a slow multi-step process
  can use a larger one.
- `maxFrames` is capped at 20 per call to keep this cheap -- for a longer video, call again with a
  higher `startFrame` to page through it rather than asking for everything at once.
- Start with a coarse sample (e.g. every 25th-30th frame) to get the overall shape of what's
  happening, then narrow in (smaller `everyNthFrame`, a specific `startFrame` range) on whichever
  part actually needs closer inspection.

## Hearing what's said: speech_to_text

Transcribes the audio track. Pass the video file's path directly -- audio extraction happens
automatically for video files (mp4/mkv/mov/webm/avi/m4v/wmv/flv), no separate ffmpeg step needed
on your part.

## Putting it together

For "what is this video about" or similar, combine both: sample frames to see what's visually
happening, transcribe the audio for what's being said, and describe the video using both --
neither alone tells the whole story for a video with both meaningful visuals and speech.
