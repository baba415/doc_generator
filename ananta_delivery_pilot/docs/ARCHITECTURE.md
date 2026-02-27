# 2026 Operating Architecture

## Layering
- `Ananta` = rails/orchestration layer.
- `Ananta Flows Ltd` = merchant principal and producer box.
- `Guildgate Global Ventures Ltd` = managed services operator (commercial and collection agent).
- `Funding Nodes` = capital providers by funding mode.

## Non-Negotiables
- Goods invoices use supplier identity/TIN of `Ananta Flows Ltd`.
- Guildgate books service revenue only for managed flows.
- Assigned collections land in dedicated Guildgate collection account and sweep to Ananta Flows.
- Run/Batch references are mandatory for pilot processing flows.

## Supported Funding Modes
- `none`
- `murabaha_supplier_direct`
- `direct_facility_to_merchant`
- `controlled_disbursement_on_behalf_of_merchant`
- `receivables_finance`
- `hybrid_topup`
