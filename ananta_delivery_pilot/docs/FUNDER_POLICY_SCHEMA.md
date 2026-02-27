# Funder Policy Schema

```json
{
  "name": "string",
  "allowed_funding_modes": ["murabaha_supplier_direct", "..."],
  "restricted_categories": ["alcohol", "..."],
  "max_ticket_value": 2500000000,
  "max_tenor_days": 180,
  "documentation_requirements": ["pfi_to_bank", "delivery_pack"]
}
```

## Runtime Enforcement
- Reject restricted categories.
- Reject unsupported funding mode.
- Reject tenor/ticket breaches.
- Log policy violations in `manifest.json`.
