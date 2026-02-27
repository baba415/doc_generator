from __future__ import annotations

from enum import Enum


class DeliveryStatus(str, Enum):
    PLANNED = "PLANNED"
    DISPATCHED = "DISPATCHED"
    DELIVERED = "DELIVERED"
    INVOICED = "INVOICED"
    PAID = "PAID"


class ContractStatus(str, Enum):
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"


class LpoState(str, Enum):
    ACTIVE = "ACTIVE"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    CLOSED = "CLOSED"


class PaymentStatus(str, Enum):
    UNPAID = "UNPAID"
    PARTIAL = "PARTIAL"
    PAID = "PAID"


class DocumentType(str, Enum):
    WAYBILL = "WAYBILL"
    WEIGHING_TICKET = "WEIGHING_TICKET"
    COA = "COA"
    INVOICE = "INVOICE"
    RECEIPT = "RECEIPT"


class EvidenceLevel(str, Enum):
    MISSING = "missing"
    SELF_ATTESTED = "self_attested"
    SUPPLIER_ACKNOWLEDGED = "supplier_acknowledged"


class PlannedDeliveryStatus(str, Enum):
    PLANNED = "PLANNED"
    SCHEDULED = "SCHEDULED"
    DISPATCHED = "DISPATCHED"
    DELIVERED = "DELIVERED"
    INVOICED = "INVOICED"
    PAID = "PAID"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"
