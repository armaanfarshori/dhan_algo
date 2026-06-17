# Four-agent dev pipeline (`/ship`)

A `planner → coder → tester → reviewer` chain in `.claude/`. Each agent has its own
context window, model, and tool scope; they hand off through `.pipeline/`. One command —
`/ship <feature>` — runs the whole chain on a feature branch and stops at the reviewer's
verdict. **It never auto-merges.**

## Layout
```
.claude/agents/   planner.md (Opus, read-only) · coder.md (Sonnet) · tester.md (Sonnet) · reviewer.md (Opus, read-only)
.claude/commands/ ship.md   — /ship <feature>
.pipeline/        hand-off bus: spec.md → changes.md → tests.md → review.md  (artifacts gitignored)
```

## Use
```
/ship add a `data_age_s` field to /api/status
```
Agent files load at session start — restart Claude Code or run `/agents` to pick up edits.

## Model choices
- **Planner = Opus** — the plan sets the ceiling for everything downstream.
- **Coder / Tester = Sonnet** — cheap once the planner removed the ambiguity (bump tester to
  Opus for subtle edge cases). `CLAUDE_CODE_SUBAGENT_MODEL` can hard-cap models per session.
- **Reviewer = Opus** — judgment quality matters most at the gate.

## The read-only reviewer
`reviewer.md` has **no Edit/Write** and can't merge — only Read/Grep/Glob + read-only
`git diff/status/log` and read-only `pytest`/`ruff` (so it independently confirms the gates).
The gatekeeper can't be the thing that edits the code it's judging.

## How it fits OUR rules (the tweaks vs the stock pipeline)
- **Branch + PR; the AGENT merges** (memory `agent-handles-merges` — not the human), but only
  after **reviewer APPROVE + green CI + outside market hours (09:15–15:30 IST)**. Never commit
  straight to `main`. See `always-branch-for-changes` / `no-main-commits-during-trading`.
- **Public repo:** no real IPs/IDs/tokens in code OR `.pipeline/*.md` (placeholders only).
- **Safety invariants:** agents never flip `PAPER_TRADING`, edit `.env`, touch AWS/secrets,
  or restart `dhan-trader`/`dhan-api`.
- **Real gates:** tester + reviewer run `pytest -q` + `ruff check .` (CI is Py3.11 x86+ARM +
  coverage + ruff). Note: local default `python3` may be 3.9 — an *import* failure from
  3.10+ syntax isn't a real failure (CI is 3.11).
- **Not a replacement for deep QA:** for substantial changes the **≥15-agent QA stack** and/or
  `/code-review ultra` still run before merge. `/ship`'s reviewer is the first gate, not the only one.

## Honest caveats (from the source design)
- **Agent-merge is gated, not blind.** The stock pipeline never auto-merges; here the agent
  DOES merge on APPROVE — but only behind reviewer APPROVE + green CI + outside market hours,
  which are the safety rails that replace the human gate. (Merging unattended to `main` with
  no gates is how bad deploys happen — these gates are non-negotiable.)
- **Subagents multiply tokens** (~7× a single thread for subagent-heavy flows) — worth it for
  real features, overkill for typos.
- For genuine overnight runs you'd drive `/ship` from headless mode (`claude -p "…"`) on a
  scheduler — keep the reviewer gate + scoped permissions in place.
