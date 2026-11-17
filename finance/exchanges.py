from decimal import ROUND_HALF_UP, Decimal, localcontext

from rest_framework.exceptions import ValidationError

MAX_MONEY = Decimal("9999999999999999.99")


def received_currency(currency: str) -> str:
    return "USD" if currency == "IQD" else "IQD"


def exchange_amount(amount: Decimal, currency: str, rate: Decimal) -> Decimal:
    # More precision than the largest supported money/rate product, before rounding.
    with localcontext() as context:
        context.prec = 60
        converted = amount * rate if currency == "USD" else amount / rate
        received = converted.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if received <= 0 or received > MAX_MONEY:
        raise ValidationError(
            {"amount": ["The rounded received amount is outside the supported range."]}
        )
    return received
