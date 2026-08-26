from lmc_po_price.pricing import UNKNOWN_MARGIN_MESSAGE, sale_price


def test_sale_price_prefers_odoo_margin_markup_when_available():
    assert sale_price(10, 5.5, "Categorie inconnue en v16", 20) == 12.66


def test_sale_price_keeps_legacy_margin_category_fallback():
    assert sale_price(10, 5.5, "Taux de marque 21%") == 13.42


def test_sale_price_reports_unknown_margin_when_no_rule_exists():
    assert sale_price(10, 5.5, "") == UNKNOWN_MARGIN_MESSAGE
