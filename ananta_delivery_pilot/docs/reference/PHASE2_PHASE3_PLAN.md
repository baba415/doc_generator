# Phase 2 / Phase 3 Plan (No Implementation in Phase 1)

## Phase 2: Local Web Workflow (FastAPI)

### Goal
Expose the Phase 1 state machine through a role-based local web workflow:
- Draft -> Review -> Approved -> Finalized

### Planned Work
1. FastAPI app with auth stubs and role checks.
2. Transaction wizard UI:
   - Create contract
   - Add delivery
   - Record COA
   - Generate pack
   - Mark paid
3. Evidence upload endpoints with immutable storage semantics.
4. DREP view browser + CSV download.
5. Approval actions (signature policy hooks, audit entries).

### Non-goals in Phase 2
- OCR parsing automation
- Cloud multi-tenant deployment

## Phase 3: LPO Parser + Review/Edit Assist

### Goal
Accelerate data capture from source documents while preserving human control.

### Planned Work
1. LPO ingestion pipeline:
   - file upload
   - extraction
   - confidence scoring
2. Review/edit screen with field-level diff and provenance.
3. Suggested mapping to:
   - buyer_id
   - product lines
   - quantities/prices
   - dates/terms
4. Controlled autofill into Phase 1 commands.
5. Exception queue for low-confidence fields.

### Guardrails
- No auto-finalization without human confirmation.
- Extracted fields must retain source references and confidence metadata.

