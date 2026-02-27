from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


KG_PER_MT = Decimal("1000")
MT_QUANT = Decimal("0.001")


def mt_to_kg_int(value_mt: Decimal | float | int | str) -> int:
    mt = Decimal(str(value_mt)).quantize(MT_QUANT, rounding=ROUND_HALF_UP)
    kg = (mt * KG_PER_MT).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(kg)


def kg_to_mt_decimal(value_kg: int | float | str | Decimal) -> Decimal:
    kg = Decimal(str(value_kg))
    return (kg / KG_PER_MT).quantize(MT_QUANT, rounding=ROUND_HALF_UP)


def kg_to_mt_str(value_kg: int | float | str | Decimal) -> str:
    return format(kg_to_mt_decimal(value_kg), "f")

