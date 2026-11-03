---
name: quant-trading-rules
description: This skill enforces the collaboration discipline and hard engineering rules for the quant-trading cryptocurrency quant platform. Use it at the start of any task in this repo — before writing code, follow the think-first / test-after / document-after workflow and the project's hard rules (Decimal money, idempotent schema migrations, write-guarded APIs, Python↔Pine alignment, crash-recoverable writes, and detailed per-file/method comments).
---

# quant-trading-rules

## Purpose
Provide the standing collaboration discipline and hard engineering rules for the
`quant-trading` repository. The platform trades on OKX (paper mode live, real mode
code-ready), so a single careless change can move real money. Every task must follow
the same safe, testable, documented workflow and respect the existing design
invariants. This skill is the behavioral companion to the repo-root `AGENTS.md`
(which remains the authoritative first-entry document).

## When to use
Load this skill whenever a task touches this repository: planning a change after
reading code, implementing a feature or fix, modifying schema / APIs / strategies /
the dashboard, or preparing a commit.

## Workflow (mandatory order)

1. **Think before coding.** Locate the change in the lifecycle chain
   (data → strategy → backtest → signal → risk → order → reconcile → monitor →
   dashboard). Identify files to touch and existing modules to reuse. Do not start
   writing until the plan is clear. If the requirement is ambiguous or has multiple
   valid approaches — especially anything involving money or live-trade parameters —
   clarify with the user before acting.
2. **Reuse, do not bypass, existing invariants.** Money is `Decimal` (never float).
   Persistence precedes memory. Signals and orders are idempotent. Risk control has
   four layers: default → global → strategy (②③ take the stricter) → online override
   (④ overrides directly). Backtest forbids look-ahead (a fill must occur at the next
   bar's open; the strategy reads `bars_window[-1]` as the current value).
3. **Schema changes only via idempotent migrations.** Append a new entry to
   `_MIGRATIONS` in `src/quant/state/sqlite_store.py`; never edit historical DDL
   (old databases have already executed it, so editing it means they stay forever
   missing a column). `SCHEMA_VERSION = 1 + len(_MIGRATIONS)` updates automatically.
   A new metric column requires BOTH the migration AND an `_METRIC_COLUMNS` whitelist
   entry, or `save_backtest_metrics` silently drops it.
4. **Keep the front/back contract in sync.** Guard any new write API with
   `_guard_write(request, token)` (localhost + `X-Dashboard-Request: 1`); the
   frontend must send that header via `writeToken()`. Always update
   `web/src/api.ts` together with `dashboard/app.py`, and sync
   `METRIC_LABELS` / `METRIC_ORDER` / `METRIC_HINTS` on metric/interface changes.
5. **Keep Python↔Pine reproducible alignment.** Strategy/backtest changes must stay
   within 5% of the Pine script (alignment tests exist on both the Python and
   frontend sides); larger drift blocks live trading. This is the project's
   "explainable & reproducible" foundation.
6. **Design crash-recoverable writes.** For any new persistence or enqueue path,
   consider dedup-key TTL, inflight→pending replay, and audit logging. Risk overrides
   and manual coverage must leave an audit trail (in `risk_events` / `risk_overrides`).
7. **Write detailed comments.** Every file gets a module docstring stating its
   responsibility, key invariants, external dependencies, and callers. Every
   public/functional method gets a docstring covering purpose, parameters, return
   value, invariants, edge cases, and known pitfalls. Add inline comments explaining
   *why* (design intent), not *what*. Keep comments in sync with the code.
8. **Test after coding.** Use the project's zero-dependency test stack (fakeredis +
   in-memory `SQLiteStore` + inline Fake Broker/Client + direct `create_app`). New
   behavior gets tests. Run `python3 -m ruff check <changed files>` and
   `python3 -m pytest -q` (baseline: 1644 passed / 0 failed). Frontend changes
   require `npm run typecheck` + `npm run build`. Schema changes require a "legacy DB
   upgrade" test (see `tests/test_sqlite_store.py::test_v8_db_gains_funding_cost_column`).
9. **Summarize and document after completion.** Summarize what was done, why, the
   pitfalls hit, and how it was verified. Update `docs/PROGRESS.md`, the relevant
   module spec, `PROJECT_STRUCTURE.md`, the feature-inventory completion status, and
   append a change log line to the PRD entry.

## Quick reference
- 14 hard rules, code map, known issues, completion checklist:
  `references/hard-rules.md` (authoritative source: repo-root `AGENTS.md`).
- Startup / paper-trade / emergency runbook:
  `docs/reference/RUNBOOK_PAPER_TRADING.md`.
- Feature completion status:
  `docs/requirements/2026-11-02-feature-inventory-and-completion.md`.
- PRD (153 requirements, single source of "what should exist"):
  `docs/requirements/2026-10-30-quant-trading-platform-requirements-v0.6.md`.
