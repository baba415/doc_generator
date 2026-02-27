# ANANTA Merge Notes (Phase 1)

## Module Mapping to Rails Truth Primitives

### `core/`
- `core/ids.py`: immutable ULID generation (`doc_id`, `contract_id`, `delivery_id`, etc.).
- `core/hashing.py`: canonical hashing primitives (`content_sha256`, `pdf_sha256`).
- `core/manifest.py`: deterministic manifest payload for adopted operational artifacts.
- `core/enums.py`: canonical status and document type constants.
- `core/config.py`: runtime registry load + lane/group inference.

### `domain/`
- `domain/transitions.py`: delivery and contract state transition rules.
- `domain/validators.py`: blocking validation gates (TIN, run/batch, COA completeness, over-delivery).
- `domain/services.py`: command-level orchestration (contract->delivery->pack->payment->exports).

### `adapters/`
- `adapters/sqlite_repo.py`: transactional persistence layer and DREP SQL views.
- `adapters/pdf_bridge.py`: low-level render bridge to existing PDF primitives (`app/pdf_layout.py`) without calling legacy `generator.generate()`.
- `adapters/storage.py`: evidence immutability and output layout.
- `adapters/csv_export.py`: deterministic DREP export writer.

### `apps/`
- `apps/cli.py`: Phase 1 command interface and command contracts.

## Data Contracts Relevant for Rails Integration

- `contracts` + `contract_line_items`: source commitment state.
- `deliveries`: execution event state (`PLANNED` -> `PAID` lifecycle).
- `sales_transactions` + `sales_lines`: canonical sell-side ledger rows.
- `documents` + `document_sales_links`: adopted artifacts linked to ledger line grain.
- `payments` + `payment_allocations`: settlement cash allocations.
- `tax_withholding_events`: certified withholding evidence events.
- `evidence_originals`: immutable source-evidence inventory.
- `coa_results` (+ `delivery_coa_links`): batch/run quality truth.

## Future Merge Considerations

1. Replace local SQLite with shared transactional store while preserving table contracts.
2. Replace file-path outputs with object-store URIs (same document metadata keys).
3. Keep canonical hashing and ULID semantics unchanged to preserve artifact identity across environments.
4. Preserve per-vendor numbering boundaries in merge target.
5. Maintain line-level `document_sales_links` integrity; avoid collapsing to invoice-only references.

