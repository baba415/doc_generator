from __future__ import annotations

from core.enums import ContractStatus, DeliveryStatus, PaymentStatus


_ALLOWED_DELIVERY_TRANSITIONS: dict[DeliveryStatus, set[DeliveryStatus]] = {
    DeliveryStatus.PLANNED: {DeliveryStatus.DISPATCHED},
    DeliveryStatus.DISPATCHED: {DeliveryStatus.DELIVERED},
    DeliveryStatus.DELIVERED: {DeliveryStatus.INVOICED},
    DeliveryStatus.INVOICED: {DeliveryStatus.PAID},
    DeliveryStatus.PAID: set(),
}


def validate_delivery_transition(current: str, target: str) -> None:
    current_status = DeliveryStatus(current)
    target_status = DeliveryStatus(target)
    allowed = _ALLOWED_DELIVERY_TRANSITIONS[current_status]
    if target_status not in allowed:
        raise ValueError(f"Invalid delivery transition: {current_status.value} -> {target_status.value}")


def derive_payment_status(outstanding_balance: float) -> PaymentStatus:
    if outstanding_balance <= 0:
        return PaymentStatus.PAID
    return PaymentStatus.PARTIAL


def derive_contract_status(*, expected_qty: float, delivered_qty: float, total_outstanding: float, cancelled: bool = False) -> ContractStatus:
    if cancelled:
        return ContractStatus.CANCELLED
    if delivered_qty <= 0:
        return ContractStatus.OPEN
    if delivered_qty < expected_qty or total_outstanding > 0:
        return ContractStatus.PARTIAL
    return ContractStatus.COMPLETE

