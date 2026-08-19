# Claude Code guide

This file exists only because Claude Code loads it automatically. It is a
pointer, not a second rulebook.

**Read [`AGENTS.md`](AGENTS.md) at the start of any session before changing
anything.** It is the canonical shared agent instruction set: repository map,
non-negotiable engineering rules, task routing, and test commands. Everything
that used to be duplicated here now lives there or in the document `AGENTS.md`
routes you to.

Claude-specific notes:

- Do not create case-variant instruction files (`agents.local.md`,
  `CLAUDE.*.md`, `claude (2).md`). The repository is on a case-insensitive
  filesystem, so `AGENTS.md` and `agents.md` are the same file.
- When a repository rule changes, update `AGENTS.md` and keep this pointer
  short.
