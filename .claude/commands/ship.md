---
description: Run a feature request through the full planner -> coder -> tester -> reviewer pipeline, handing off via the .pipeline/ folder. Works on a feature branch; the agent merges on reviewer APPROVE + green CI + outside market hours.
argument-hint: <feature request, e.g. add a /api/health detail field>
---

You are the ORCHESTRATOR for a four-agent dev pipeline. The feature request is:

> $ARGUMENTS

Run these phases IN ORDER. Each phase reads the previous phase's `.pipeline/` artifact,
so do not skip ahead and do not parallelize them.

0. SETUP.
   - Ensure `.pipeline/` exists (create if missing).
   - Ensure work happens on a FEATURE BRANCH, never `main` (house rule). If currently on
     `main`, create/checkout `feat/<short-slug>` first. (Never commit/merge to `main`.)

1. PLAN — Invoke the `planner` subagent with the feature request above.
   Wait until `.pipeline/spec.md` exists. Print a short summary of the spec.

2. CODE — Invoke the `coder` subagent. It reads `.pipeline/spec.md`, implements it on the
   feature branch, then writes `.pipeline/changes.md`. Wait for that file before continuing.

3. TEST — Invoke the `tester` subagent. It reads the spec + changes + diff, writes + runs
   tests (`pytest -q` + `ruff check .`), then writes `.pipeline/tests.md`. Wait for it.

4. REVIEW — Invoke the `reviewer` subagent (read-only). It reads spec + diff + tests, re-runs
   the gates, and returns a verdict. Capture it verbatim and write it to `.pipeline/review.md`.

5. DECIDE. Present the reviewer's VERDICT line, blocking issues, and recommendation.
   - **REQUEST CHANGES** → loop back to the `coder` with the reviewer's notes; do NOT merge.
   - **APPROVE** → the AGENT merges (memory `agent-handles-merges` — don't wait for a human),
     but ONLY after ALL gates pass:
       a. push the branch + open a PR,
       b. **CI is green** (block on it; never merge a red/ pending CI),
       c. it is **outside market hours (09:15–15:30 IST)** — if inside, hold the merge until
          after close and say so,
       d. for SUBSTANTIAL changes, the ≥15-agent QA stack / `/code-review ultra` has also run.
     Then `gh pr merge --squash` to `main`. Never commit straight to `main` (always via the
     PR). The reviewer stays read-only — only the orchestrator merges, on the reviewer's APPROVE.

> For SUBSTANTIAL features, also run the heavy QA stack (≥15 narrow agents → synthesis)
> and/or `/code-review ultra` before merging — `/ship`'s single reviewer is the first gate,
> not the only one.
