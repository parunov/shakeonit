from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CURRENCIES = ("BYN", "RUB", "EUR", "USD")
CURRENCY_SYMBOLS = {"BYN": "Br", "RUB": "₽", "EUR": "€", "USD": "$"}


def parse_amount(value: str) -> int:
    """Convert a human amount to integer cents/kopecks."""
    normalized = value.strip().replace(" ", "").replace(",", ".")
    try:
        amount = Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError("Введите сумму числом, например 1250,50") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Сумма должна быть больше нуля")
    quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    minor = int(quantized * 100)
    if minor > 999_999_999_99:
        raise ValueError("Слишком большая сумма")
    return minor


def format_money(minor: int, currency: str) -> str:
    sign = "−" if minor < 0 else ""
    absolute = abs(minor)
    number = f"{absolute // 100:,}.{absolute % 100:02d}".replace(",", " ")
    return f"{sign}{number} {CURRENCY_SYMBOLS.get(currency, currency)}"


def split_amount(amount: int, user_ids: list[int]) -> dict[int, int]:
    """Split exactly, assigning leftover cents deterministically by sorted id."""
    unique_ids = sorted(set(user_ids))
    if not unique_ids:
        raise ValueError("Нужно выбрать хотя бы одного участника")
    base, remainder = divmod(amount, len(unique_ids))
    return {user_id: base + (index < remainder) for index, user_id in enumerate(unique_ids)}
