---
description: Answer a question from the marginalia library of video notes. Use when the user asks what the library says, what a video claims or gets wrong, or where something was said.
---

Run the command and print what it returns. Add nothing.

```
marg ask "<question>"
```

## Why there is nothing else to do here

The answering happens inside Python — the notes are loaded (all of them, or a
selection of whole notes once the library passes the context budget), the
question is answered against them, transcripts are opened when the question
needs exact wording, and every quote in the answer is checked against the
transcript of the video it cites. All of that is one command. There is no step
where you read a note, choose which videos are relevant, or assemble an answer
yourself.

That is not a style preference. A library is hundreds of thousands of tokens of
notes. Pulling them into this conversation to answer one question rebuilds the
cost curve the whole system exists to avoid, and escalating from here would put
transcript text into the conversation, which the hard rules forbid. **Never
read, open, print or summarise a `note.md` or a `transcript.txt`.**

## What comes back

- the answer, in prose, with citations like `[dQw4w9WgXcQ 09:31]`, and a marker
  beside any quote the cited transcript does not hold — `[loosely quoted]`,
  `[not spoken]` (the note's words, not the speaker's) or `[unverified]`
- a `links:` block — one YouTube deep link per citation, landing at the moment cited
- an `opened transcripts:` block, when the question needed exact wording
- a final `ask:` line with notes loaded, citation count, cost and elapsed time, and
  a `grounding:` line saying how many quotes were checked and found

Print all of it, markers included. The links are the point: every claim is
checkable in one click.

If the command exits non-zero, there is no answer: the model call failed, or the
model asked for transcripts instead of answering. Report the error line as an
error, never as what the library says.

If the answer says the library does not cover the question, report that as it
stands. Do not fill the gap from your own knowledge — the value of this library
is that everything in an answer is traceable to a video, and an unsourced
sentence beside sourced ones is indistinguishable from them.

## Flags

- `--deep <video_id>` forces a transcript open before answering. Offer it when
  the user asks about exact wording in a video they have named, or is unhappy
  that an answer stayed at the level of the notes.
- `--json` for structured output. Only when something will parse it.

## When there is no answer

`0 notes` means the library has transcripts but no notes yet. Say so and offer:

```
marg note --status
marg note --limit 3
```

If the citations look a few seconds late, `marg snap-timestamps` realigns them
against the transcripts. It costs nothing and makes no model calls.
