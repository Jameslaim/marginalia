---
description: Ingest a YouTube video into the marginalia library. Use when the user gives a YouTube URL and wants its transcript stored.
---

Run `marg ingest <url>` with the URL the user provided.

Report the single line the command returns. Do not read, open, print, or summarise
the transcript file. Do not write into the library directory yourself.

If the command reports `needs-asr`, tell the user the video has no captions and is
queued for transcription (not yet implemented).
