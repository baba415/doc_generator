"""Payload enrichment — auto-populate required fields from entity data.

Governed by contracts/pilot_event_contract.md v0.1.
Principle: callers provide what they want to do. The API fills in what the system knows.
Caller-provided values always take precedence (never overwrite).

Enrichment is OBSERVABLE: the receipt includes which fields were enriched and from where.
Enrichment is FAIL-CLOSED: if required fields are still missing after enrichment, reject.

Payload structure (as per validator dotted-path convention):
  - "trade_id"              → payload["trade_id"]           (top-level)
  - "payload.actor_org_id" → payload["payload"]["actor_org_id"]  (nested)
  - "payload.payment_terms" → payload["payload"]["payment_terms"] (nested)
"""

from dataclasses import dataclass, field
from typing import Any, List


@dataclass
class EnrichmentReport:
    """What enrichment did — included in the receipt for auditability."""
    enrichment_version: str = "enrich_v1"  # bump when enrichment logic changes
    fields_added: dict = field(default_factory=dict)
    # e.g., {"trade_id": "entity.core_uuid", "actor_org_id": "entity.operator_uuid"}
    missing_after_enrichment: list = field(default_factory=list)
    # e.g., ["payload.shipment_id"] — fields still missing after enrichment


def enrich_common_fields(payload: dict, entity_row: dict) -> tuple:
    """Enrich fields common to all event types.

    Returns (enriched_payload, added_fields_map).

    Handles the validator's dotted-path convention:
      - trade_id is top-level in the outer dict
      - actor_org_id lives inside the nested payload["payload"] sub-dict
    """
    enriched = dict(payload)
    # Deep-copy the nested payload sub-dict so we don't mutate the original
    if 'payload' in enriched and isinstance(enriched['payload'], dict):
        enriched['payload'] = dict(enriched['payload'])
    added = {}

    # trade_id: top-level, use core_uuid (UUID format) instead of ULID
    if 'trade_id' not in enriched:
        uuid_val = entity_row.get('core_uuid')
        if uuid_val:
            enriched['trade_id'] = uuid_val
            added['trade_id'] = 'entity.core_uuid'
        else:
            fallback = entity_row.get('id') or entity_row.get('contract_id')
            if fallback:
                enriched['trade_id'] = fallback
                added['trade_id'] = 'entity.id (fallback — may not be UUID)'

    # actor_org_id: nested inside payload["payload"] sub-dict
    # (validator checks "payload.actor_org_id" via _get_nested)
    nested = enriched.get('payload') if isinstance(enriched.get('payload'), dict) else {}
    if 'actor_org_id' not in nested:
        # Prefer operator_uuid (UUID from parties join), fall back to string ID
        org_val = (
            entity_row.get('operator_uuid')
            or entity_row.get('operator_id')
            or entity_row.get('vendor_of_record_id')
            or entity_row.get('org')
        )
        src = _source_field(
            entity_row,
            ['operator_uuid', 'operator_id', 'vendor_of_record_id', 'org'],
        )
        if org_val:
            nested['actor_org_id'] = org_val
            added['actor_org_id'] = 'entity.{}'.format(src)
        # If no org found: leave missing — validator will reject (fail-closed).
        # Do NOT invent a default like 'ORG-PILOT-DEFAULT' — that's not entity data.
        enriched['payload'] = nested

    return enriched, added


def enrich_event_specific_fields(
    payload: dict,
    entity_row: dict,
    event_type: str,
) -> tuple:
    """Enrich fields specific to the event type.

    Returns (enriched_payload, added_fields_map).
    Event-specific fields (e.g. payment_terms) live in the nested payload sub-dict.
    """
    enriched = dict(payload)
    if 'payload' in enriched and isinstance(enriched['payload'], dict):
        enriched['payload'] = dict(enriched['payload'])
    added = {}

    if event_type == 'TERMS_SUBMITTED':
        nested = enriched.get('payload') if isinstance(enriched.get('payload'), dict) else {}
        for field_name, entity_fields in [
            ('payment_terms', ['payment_terms', 'due_terms']),
            ('delivery_term', ['delivery_term', 'incoterm']),
            ('delivery_location', ['delivery_location', 'destination']),
        ]:
            if field_name not in nested:
                entity_val = _first_available(entity_row, entity_fields)
                if entity_val:
                    nested[field_name] = entity_val
                    added[field_name] = 'entity.{}'.format(
                        _source_field(entity_row, entity_fields)
                    )
                # else: leave missing — validator will reject (fail-closed).
                # Do NOT invent defaults like "NET30" — that's an assumption, not data.
        enriched['payload'] = nested

    elif event_type == 'SHIPMENT_DISPATCHED':
        nested = enriched.get('payload') if isinstance(enriched.get('payload'), dict) else {}
        if 'shipment_id' not in nested:
            ship_val = (
                entity_row.get('shipment_id')
                or entity_row.get('delivery_id')
                or entity_row.get('core_uuid')
            )
            if ship_val:
                nested['shipment_id'] = ship_val
                added['shipment_id'] = 'entity.{}'.format(
                    _source_field(entity_row, ['shipment_id', 'delivery_id', 'core_uuid'])
                )
            enriched['payload'] = nested

    elif event_type == 'DELIVERY_CONFIRMED':
        nested = enriched.get('payload') if isinstance(enriched.get('payload'), dict) else {}
        if 'received_quantity_mt' not in nested:
            qty = entity_row.get('quantity') or entity_row.get('delivered_qty')
            if qty is not None:
                qty_field = 'quantity' if entity_row.get('quantity') is not None else 'delivered_qty'
                nested['received_quantity_mt'] = qty
                added['received_quantity_mt'] = 'entity.{}'.format(qty_field)
            enriched['payload'] = nested

    return enriched, added


def validate_required_after_enrichment(
    payload: dict,
    event_type: str,
    validator: Any,
) -> List[str]:
    """Check if required fields are still missing after enrichment.

    Returns list of missing field names. Empty list = all good.
    """
    result = validator.validate(event_type, payload)
    if result.valid:
        return []

    missing = [e for e in result.errors if 'Missing required field' in str(e)]
    return missing


def build_enrichment_report(
    common_added: dict,
    specific_added: dict,
    missing: list,
) -> EnrichmentReport:
    """Combine enrichment results into a report."""
    all_added = {**common_added, **specific_added}
    return EnrichmentReport(
        fields_added=all_added,
        missing_after_enrichment=missing,
    )


def _first_available(row: dict, fields: list) -> Any:
    """Return the first non-None value from a list of field names."""
    for f in fields:
        val = row.get(f)
        if val is not None:
            return val
    return None


def _source_field(row: dict, fields: list) -> str:
    """Return the name of the first field that has a value."""
    for f in fields:
        if row.get(f) is not None:
            return f
    return 'unknown'
