# PR Review Checklist

**Used by:** Codex CLI (merchant/ PRs), ChatGPT Plus (workbench/ PRs), Claude Code (core/ PRs)
**Rule:** Reviewer must not be the same model that wrote the code.

---

## Gate 1: SCOPE

- [ ] PR title references exactly one v3.5 step (e.g., "Phase 0: update COA profiles for vegetable oil")
- [ ] Files changed are only those expected for this step
- [ ] No unrelated changes bundled in
- [ ] No CI fixes mixed into feature PRs (CI fixes get their own PR)

**PASS / FAIL:**
**Notes:**

---

## Gate 2: CANON

- [ ] No new write surfaces introduced (all mutations through evented path)
- [ ] No new event types invented (pilot validates against core_event_requirements.json only)
- [ ] Follows existing code patterns (uses `_add_column_if_missing`, `SQLiteRepo.transaction()`, etc.)
- [ ] If UI code: no server-side mutations from UI; data_source badge preserved
- [ ] If event code: three-tier classification respected (TRANSITION / PREP_EVIDENCE / PREP_NOTE)

**PASS / FAIL:**
**Notes:**

---

## Gate 3: SECURITY

- [ ] Fail-closed: schema validation failures result in rollback, not partial writes
- [ ] No existence leaks (errors don't reveal whether entities exist to unauthorized actors)
- [ ] Auth checks present where required
- [ ] Idempotency key handling correct (same key + same hash = dedup; same key + different hash = error)
- [ ] Debug/reject logs written outside transaction boundary (JSONL append)

**PASS / FAIL:**
**Notes:**

---

## Gate 4: TESTS

- [ ] New code has corresponding test(s)
- [ ] Existing tests still pass
- [ ] If Phase 1C: verification tests cover the specific invariant being implemented
- [ ] If config change: sample data updated to match

**PASS / FAIL:**
**Notes:**

---

## Gate 5: MERGE RECOMMENDATION

- [ ] CI green
- [ ] All gates above PASS
- [ ] No TODOs left unresolved in the diff

**RECOMMEND MERGE / DO NOT MERGE**

**If DO NOT MERGE — issues (max 5):**
1.
2.
3.

---

## Post-Merge (Codex CLI executes)

```bash
gh pr merge {number} --squash --delete-branch
# Record in build_progress.md:
# - Step name
# - PR number
# - Merge SHA
# - Date
# - One-line outcome
```
