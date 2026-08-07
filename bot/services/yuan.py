"""Пересчёт USDT/RUB в цену юаня.

Делитель живёт здесь, а не в хендлере: по нему считает и строка курса,
и сумма платежа в QR+KYC — расхождение между ними недопустимо.
Прод считает по 6.67 (не 6.7) — не менять без явного запроса.
"""
from decimal import Decimal

FACTOR = Decimal("1") / Decimal("6.67")


def yuan_price(rate: Decimal) -> Decimal:
    """Цена одного юаня в рублях по курсу USDT/RUB."""
    return (FACTOR * rate).quantize(Decimal("0.01"))
