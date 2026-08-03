import pytest

from lmc_po_price.odoo_articles import _search_read_all, config_from_mapping, margin_markup_by_id, supplierinfo_partner_field


class FakeOdooModel:
    def __init__(self, fields=None, rows=None):
        self.rows = rows or []
        self.fields = fields
        self.search_calls = []
        self.read_calls = []

    def fields_get(self, fields=None):
        return self.fields or {}

    def search(self, domain):
        self.search_calls.append(domain)
        return [row["id"] for row in self.rows]

    def read(self, ids, fields):
        self.read_calls.append((ids, fields))
        return [row for row in self.rows if row["id"] in ids]


class FakeProductModel(FakeOdooModel):
    def fields_get(self, fields=None):
        if fields == ["margin_classification_id"]:
            return {"margin_classification_id": {"relation": "product.margin.classification"}}
        return super().fields_get(fields)


class FakeOdoo:
    def __init__(self, margin_model):
        self.env = {"product.margin.classification": margin_model}


def test_config_from_mapping_accepts_optional_timeout_and_batch_size():
    config = config_from_mapping(
        {
            "url": "odoo.example.org",
            "database": "db",
            "username": "user",
            "password": "password",
            "timeout": 420,
            "batch_size": 250,
        }
    )

    assert config.timeout == 420
    assert config.batch_size == 250


def test_config_from_mapping_defaults_timeout_and_batch_size():
    config = config_from_mapping(
        {
            "url": "odoo.example.org",
            "database": "db",
            "username": "user",
            "password": "password",
        }
    )

    assert config.timeout == 300
    assert config.batch_size == 500


def test_supplierinfo_partner_field_prefers_v16_partner_id():
    model = FakeOdooModel({"name": {}, "partner_id": {}})

    assert supplierinfo_partner_field(model) == "partner_id"


def test_supplierinfo_partner_field_supports_v12_name():
    model = FakeOdooModel({"name": {}})

    assert supplierinfo_partner_field(model) == "name"


def test_supplierinfo_partner_field_rejects_unknown_schema():
    model = FakeOdooModel({"display_name": {}})

    with pytest.raises(ValueError, match="partner_id ni name"):
        supplierinfo_partner_field(model)


def test_search_read_all_fetches_all_batches():
    rows = [{"id": index} for index in range(5)]
    model = FakeOdooModel(rows=rows)

    result = _search_read_all(model, [("active", "=", True)], ["id"], batch_size=2)

    assert result == rows
    assert model.search_calls == [[("active", "=", True)]]
    assert [call[0] for call in model.read_calls] == [[0, 1], [2, 3], [4]]


def test_search_read_all_rejects_invalid_batch_size():
    with pytest.raises(ValueError, match="batch_size"):
        _search_read_all(FakeOdooModel(rows=[]), [], ["id"], batch_size=0)


def test_margin_markup_by_id_reads_odoo_markup_when_available():
    product = FakeProductModel()
    margin_model = FakeOdooModel(
        fields={"id": {}, "name": {}, "markup": {}},
        rows=[
            {"id": 21, "markup": 27.1941},
            {"id": 25, "markup": 34.1382},
        ],
    )
    odoo = FakeOdoo(margin_model)

    import pandas as pd

    df_articles = pd.DataFrame({"margin_classification_id": [[21, "Taux 21"], [25, "Taux 25"], None]})

    result = margin_markup_by_id(product, odoo, df_articles, batch_size=100)

    assert result == {21: 27.1941, 25: 34.1382}
