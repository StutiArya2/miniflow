"""A plugin block: how a developer adds a block that no-code flows can then use.

Any ``*.py`` file in a ``blocks/`` folder next to a JSON flow is loaded
automatically, so ``{"block": "convert_currency", ...}`` works in every flow in
``examples/``. Run ``miniflow blocks convert_currency`` to see its settings.
"""

from miniflow.blocks import Setting, block

RATES_TO_INR = {"INR": 1.0, "USD": 83.0, "EUR": 90.0}


@block(
    "convert_currency",
    settings=[
        Setting("rows", list, help="Rows with an amount and a currency"),
        Setting("amount_field", str, default="amount"),
        Setting("currency_field", str, default="currency"),
        Setting("rates", dict, default=None, help="Rate per currency (default: INR, USD, EUR)"),
        Setting("output_field", str, default="amount_inr"),
    ],
    kind="cpu",
)
def convert_currency(rows, amount_field, currency_field, rates, output_field):
    """Add a field with each row's amount converted to Indian rupees."""
    rates = rates or RATES_TO_INR
    out = []
    for i, row in enumerate(rows, start=1):
        currency = row[currency_field]
        if currency not in rates:
            raise ValueError(f"row {i}: no exchange rate for {currency!r}")
        out.append({**row, output_field: round(row[amount_field] * rates[currency], 2)})
    return out
