**STOP. Read `RULES.md` before editing anything in this repository.** Every rule in it
encodes a failure that was measured on this system, and editing without them re-breaks
bugs that have already been found, fixed and paid for.

# Claude Code adapter

marginalia is a Python CLI. It runs on any machine with `yt-dlp`, `openai` and a config
file, and it does not need Claude Code to function. Its rules, its reasoning and every
finding live in `RULES.md` and `PLAN.md`, which belong to the agent.

This file describes only how one tool — Claude Code — is wired to it. Nothing here is
part of the agent, and nothing here changes how it behaves.

## The `guard-write` hook

`hooks/hooks.json` runs `marg guard-write` as a `PreToolUse` hook and blocks the tool
call, in support of rule 2 (*never write into the library directory directly — always
go through the CLI*).

**It is a convenience, not an enforcement boundary.** The hook matches
`Write|Edit|MultiEdit|NotebookEdit` and does not match shell commands, so a script or
a shell redirection writing into the library path is not blocked. Rule 2 holds because
the agent follows it, not because this hook makes it impossible to break.

## Skills

`skills/ask/SKILL.md` and `skills/ingest/SKILL.md` are thin wrappers: they call `marg`
and report the line it returns. Logic never lives in a `SKILL.md` — the ingest skill
routes on whether the argument parses as a video ID, and does nothing else.
