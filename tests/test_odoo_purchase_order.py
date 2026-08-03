import pandas as pd

from lmc_po_price.odoo_purchase_order import _prepare_purchase_order_lines, _resolve_product_id


class FakeModelData:
    def __init__(self):
        self.domains = []

    def search_read(self, domain, fields):
        self.domains.append((domain, fields))
        if domain == [
            ("model", "=", "product.product"),
            ("module", "=", "__export__"),
            ("name", "=", "product_product_6794_419eb2c4"),
        ]:
            return [{"res_id": 6794}]
        if domain == [
            ("model", "=", "product.product"),
            ("complete_name", "=", "__export__.product_product_6794_419eb2c4"),
        ]:
            return [{"res_id": 10000}]
        return []


class FakeOdoo:
    def __init__(self):
        self.model_data = FakeModelData()
        self.product = FakeProduct()
        self.env = {"ir.model.data": self.model_data, "product.product": self.product}


class FakeProduct:
    def __init__(self):
        self.read_calls = []

    def read(self, ids, fields):
        self.read_calls.append((ids, fields))
        if ids == [5015]:
            return [
                {
                    "id": 5015,
                    "name": "BETTERAVE CUITE",
                    "uom_po_id": [1, "Units"],
                    "uom_id": [1, "Units"],
                }
            ]
        return []


def test_resolve_product_id_uses_module_and_name_for_external_id():
    odoo = FakeOdoo()

    product_id = _resolve_product_id(odoo, "__export__.product_product_6794_419eb2c4")

    assert product_id == 6794
    assert odoo.model_data.domains == [
        (
            [
                ("model", "=", "product.product"),
                ("module", "=", "__export__"),
                ("name", "=", "product_product_6794_419eb2c4"),
            ],
            ["res_id"],
        )
    ]


def test_resolve_product_id_accepts_numeric_id():
    product_id = _resolve_product_id(FakeOdoo(), "12096")

    assert product_id == 12096


def test_prepare_purchase_order_lines_uses_numeric_product_id_without_model_data():
    odoo = FakeOdoo()
    rows = pd.DataFrame(
        [
            {
                "Référence commande": "FC123",
                "Fournisseur/ID": "244",
                "Lignes de la commande/Article/ID": "5015",
                "Lignes de la commande/Description": "[120153] BETTERAVE CUITE",
                "Lignes de la commande/Quantité": 24,
                "Lignes de la commande/Prix unitaire": 1.35,
                "Lignes de la commande/Date prévue": "19/06/2026 00:00",
            }
        ]
    )

    partner_id, order_lines, line_results = _prepare_purchase_order_lines(rows, odoo)

    assert partner_id == 244
    assert len(order_lines) == 1
    assert order_lines[0][2]["product_id"] == 5015
    assert order_lines[0][2]["product_uom"] == 1
    assert line_results[0]["status"] == "ready"
    assert odoo.model_data.domains == []
