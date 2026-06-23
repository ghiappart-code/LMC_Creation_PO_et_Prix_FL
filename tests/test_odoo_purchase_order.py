from lmc_po_price.odoo_purchase_order import _resolve_product_id


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
        self.env = {"ir.model.data": self.model_data}


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
