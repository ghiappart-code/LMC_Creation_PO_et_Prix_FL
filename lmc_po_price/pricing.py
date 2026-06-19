from __future__ import annotations

import re

import pandas as pd


MARGIN_RATES = {
    "Taux de marque 15%": 17.6471,
    "Taux de marque 21%": 27.1941,
    "Taux de marque 21%-Consigne 0,07": 27.1941,
    "Taux de marque 21%-Consigne 0,15": 27.1941,
    "Taux de marque 25%": 34.1382,
    "Taux de marque 25%-Consigne 0,20": 34.1382,
    "Taux de marque 25%-Consigne 0,35": 34.1382,
    "Taux de marque 25%-Consigne 0,50": 34.1382,
    "Taux de marque 25%-Consigne 1€": 34.1382,
    "Taux de marque 25%-Consigne 2€": 34.1382,
    "Taux de marque 25%-Consigne 2€5": 34.1382,
    "Taux de marque 25%-Consigne 3€": 34.1382,
}

UNKNOWN_MARGIN_MESSAGE = "categorie marge a verifier"


def sale_price(cost: object, tax_rate: object, margin_category: object) -> float | str:
    try:
        margin_text = str(margin_category)
        margin_rate = MARGIN_RATES[margin_text]
        numeric_cost = float(cost)
        numeric_tax_rate = 0.0 if pd.isna(tax_rate) else float(tax_rate)
        price = numeric_cost * (1 + margin_rate / 100) * (1 + numeric_tax_rate / 100)
        if "Consigne" in margin_text:
            price += _deposit_value(margin_text)
        return round(price, 2)
    except Exception:
        return UNKNOWN_MARGIN_MESSAGE


def _deposit_value(margin_category: str) -> float:
    match = re.search(r"Consigne\s+(.+)$", margin_category)
    if not match:
        return 0.0
    raw_value = match.group(1).strip()
    normalized = raw_value.replace("€", ".").replace(",", ".")
    return float(normalized)

