import pandas as pd

from lmc_po_price.odoo_price_update import (
    _dry_run_price_update_rows,
    _relation_or_scalar_id,
    prepare_odoo_price_update_rows,
)


class FakeProductModel:
    def __init__(self):
        self.read_calls = []

    def read(self, ids, fields):
        self.read_calls.append((ids, fields))
        if ids == [5015]:
            return [
                {
                    "id": 5015,
                    "product_tmpl_id": [7015, "BETTERAVE CUITE"],
                    "standard_price": 1.1,
                    "list_price": 2.2,
                    "sale_ok": True,
                }
            ]
        return []


class FakeSupplierInfoModel:
    def __init__(self):
        self.search_read_calls = []

    def search_read(self, domain, fields):
        self.search_read_calls.append((domain, fields))
        if domain == [("product_tmpl_id", "=", 7015), ("partner_id", "=", 244)]:
            return [{"id": 9001, "price": 1.2}]
        if domain == [("product_tmpl_id", "=", 7015), ("name", "=", 244)]:
            return [{"id": 9002, "price": 1.2}]
        return []


def test_prepare_odoo_price_update_rows_keeps_existing_lmc_columns():
    price_changes = pd.DataFrame(
        [
            {
                "statut": "trouve",
                "prix_change": True,
                "peut_etre_mis_a_jour": True,
                "db_id_externe": "__export__.product_product_5015",
                "db_article_id": 5015,
                "db_fournisseur_id": 244,
                "Fact_PU_unitaire": 1.35,
                "Fact_PU_Net_GZ": 1.4,
                "New_Prix_de_vente": 2.7,
                "Odoo_sale_ok": True,
            }
        ]
    )

    rows = prepare_odoo_price_update_rows(price_changes)

    assert list(rows.columns) == [
        "db_id_externe",
        "db_article_id",
        "db_fournisseur_id",
        "Fact_PU_unitaire",
        "Fact_PU_Net_GZ",
        "New_Prix_de_vente",
        "Odoo_sale_ok",
    ]
    assert rows.loc[0, "db_article_id"] == 5015


def test_dry_run_price_update_rows_uses_v16_partner_id_field():
    Product = FakeProductModel()
    SupplierInfo = FakeSupplierInfoModel()
    update_rows = pd.DataFrame(
        [
            {
                "db_article_id": 5015,
                "db_fournisseur_id": 244,
                "Fact_PU_unitaire": 1.35,
                "Fact_PU_Net_GZ": 1.4,
                "New_Prix_de_vente": 2.7,
                "Odoo_sale_ok": False,
            }
        ]
    )

    results = _dry_run_price_update_rows(Product, SupplierInfo, update_rows, "partner_id")

    assert results[0]["status"] == "ready"
    assert results[0]["supplierinfo_id"] == 9001
    assert results[0]["supplierinfo_partner_field"] == "partner_id"
    assert SupplierInfo.search_read_calls == [
        ([("product_tmpl_id", "=", 7015), ("partner_id", "=", 244)], ["id", "price"])
    ]


def test_dry_run_price_update_rows_supports_v12_name_field():
    Product = FakeProductModel()
    SupplierInfo = FakeSupplierInfoModel()
    update_rows = pd.DataFrame(
        [
            {
                "db_article_id": 5015,
                "db_fournisseur_id": 244,
                "Fact_PU_unitaire": 1.35,
                "Fact_PU_Net_GZ": 1.4,
                "New_Prix_de_vente": 2.7,
                "Odoo_sale_ok": True,
            }
        ]
    )

    results = _dry_run_price_update_rows(Product, SupplierInfo, update_rows, "name")

    assert results[0]["status"] == "ready"
    assert results[0]["supplierinfo_id"] == 9002
    assert results[0]["supplierinfo_partner_field"] == "name"


def test_relation_or_scalar_id_handles_many2one_and_scalar_values():
    assert _relation_or_scalar_id([42, "Product Template"]) == 42
    assert _relation_or_scalar_id("42") == 42
    assert _relation_or_scalar_id(None) is None

