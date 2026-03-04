# Merchant Repo Control Tower

Last updated: 2026-03-02 (Africa/Lagos)

## Repo

- Path: `/Users/macbookairv2/Prinsip/merchant`
- Git remote: `baba415/doc_generator` (note: repo label vs remote name should be confirmed)
- Branch: `codex/pr5-release-ready` @ `5f71c6a`

## Thread Registry (deeplinks)

| Role | Deeplink | Workspace (as last seen) | Branch | SHA |
|---|---|---|---|---|
| Merchant planner | `codex://threads/019cad7e-0e9a-78a0-aab8-cf6e5753aff0` | `/Users/macbookairv2/Prinsip/merchant` | `codex/pr5-release-ready` | `5f71c6a` |
| Merchant implementer | `codex://threads/019cad88-d6f0-7030-9b21-8d306c179cf3` | `/Users/macbookairv2/Prinsip/merchant` | `codex/pr5-release-ready` | `5f71c6a` |
| Merchant verifier | `codex://threads/019cad89-782c-7e31-9c2d-fb6d6089c4d5` | `/Users/macbookairv2/Prinsip/merchant` | `codex/pr5-release-ready` | `5f71c6a` |
| Execute planner | `codex://threads/019cad7e-5ed6-72b1-9a83-40704c3a949f` | `/Users/macbookairv2/Prinsip/execute` | `codex/bootstrap-execute-v11-tracked` | `c9ea7ae` |
| Core planner | `codex://threads/019cad7e-aa58-77b1-97d9-c429f45b49d8` | `/Users/macbookairv2/Prinsip/core` | `main` | `f47c931` |
| Context (legacy, Execute) | `codex://threads/019c92e2-0cde-7c22-b829-a2c56399233b` | `/Users/macbookairv2/doc_generator` | *(n/a)* | *(n/a)* |
| Context (legacy, Execute implementer) | `codex://threads/019c8ab8-e6f9-75b2-a92e-5180c4f0b6a4` | `/Users/macbookairv2/doc_generator` | *(n/a)* | *(n/a)* |

## Folder Alignment Rules

- Prefer relative paths in docs/scripts (avoid hardcoding `/Users/macbookairv2/doc_generator/...`).
- Keep deeplinks stable even when repo paths change; add a note when a thread’s workspace is on an old path.

## Verifier Assignments (Critical Merchant Workflows)

Primary flows to gate:
- Phase 2 UI routes: `/v2/portfolio`, `/v2/intake`, `/v2/exceptions`, and contract flow (`plan` → `execute` → `settle`).
- DREP ledger invariants + core state machine (Phase 1).
- DG-1B shadow proof artifacts + strict Execute consumer validators.
- Legacy generator non-regression.

Suggested verifier command set (deterministic, local):

```bash
cd ananta_delivery_pilot

# Targeted “critical-path” tests
PYTHONPYCACHEPREFIX=/tmp/pycache python3 -m unittest -v \
  tests.test_phase2_ui \
  tests.test_phase2_pr8_intake_logic \
  tests.test_phase2_pr7_exceptions \
  tests.test_phase2_pr10_settlement_kpi \
  tests.test_dg1b_shadow_mode

# Full suite (repo baseline)
bash scripts/test_default.sh
```

Proof artifacts (DG-1B, no Rails writes):

```bash
cd ananta_delivery_pilot
python3 run.py dg1b-shadow-proof --as-of 2026-03-01 --out-dir .state/phase2-proof/dg1b/manual
```

Evidence pointers to attach to verifier reports:
- `ananta_delivery_pilot/.state/phase2-proof/pr5/host-smoke-*/` (if host smoke run)
- `ananta_delivery_pilot/.state/phase2-proof/dg1b/*/`

## PR / CI / Branch Protection Tracking

As of 2026-03-02:
- PRs for `codex/pr5-release-ready`: none found.
- GitHub Actions runs: none found (current workflow is PR-triggered).
- Branch protection (default branch: `codex/pr5-release-ready`):
  - Required PR review count: 1
  - Dismiss stale reviews: enabled
  - Enforce admins: enabled
  - Linear history: required
  - Conversation resolution: required
  - Required status checks: **NOT enabled** (tests are not enforced by protection rules today)

## Dependencies / Blockers (Execute + Core)

Merchant remains projection-first; Rails truth activation is gated by R-1 readiness (see `ananta_delivery_pilot/SPEC_DG1A_RAILS_TRUTH_INTEGRATION.md`):
- Rails event allowlist + stable mapping for trust-changing actions.
- Stable `decision_meta` + machine-readable reason codes (no ad-hoc strings).
- Rails apply-status surface for replay/conflict resolution.
- Protocol pin compatibility.
- Read-model surfaces sufficient to avoid client-side truth recomputation.

