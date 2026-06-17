# .pipeline — the hand-off bus

Each agent reads the previous agent's file here and writes its own. This is how context
flows down the chain (`/ship`) without re-prompting.

| Stage    | Reads                                     | Writes                 |
|----------|-------------------------------------------|------------------------|
| planner  | the feature request                       | `spec.md`              |
| coder    | `spec.md`                                 | `changes.md`           |
| tester   | `spec.md`, `changes.md`, `git diff`       | `tests.md`             |
| reviewer | `spec.md`, `changes.md`, `tests.md`, diff | (verdict → `review.md`, written by the orchestrator) |

The transient artifacts (`spec.md`/`changes.md`/`tests.md`/`review.md`) are **gitignored**
(`.pipeline/*.md` except this README) so they don't clutter `main`; the audit trail lives in
the feature PR. This dir + README + `.gitkeep` are tracked so the bus exists on a fresh clone.
