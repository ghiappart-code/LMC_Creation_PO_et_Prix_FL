from __future__ import annotations

import pandas as pd

from lmc_po_price.text import normalize_key


RELAIS_VERT_SUPPLIER_CODE = "254"

FRUIT_VEG_CATEGORY_KEYS = {
    normalize_key("Fruits & Legumes"),
    normalize_key("Fruits et legumes"),
    normalize_key("Fruits et légumes"),
}


def filter_articles_for_supplier(articles: pd.DataFrame, supplier_code: str) -> pd.DataFrame:
    """Applique les règles de périmètre propres au fournisseur avant matching."""
    if str(supplier_code) != RELAIS_VERT_SUPPLIER_CODE:
        return articles
    if "categorie_mere" not in articles:
        return articles
    category_key = articles["categorie_mere"].fillna("").map(normalize_key)
    return articles[category_key.isin(FRUIT_VEG_CATEGORY_KEYS)].copy()
