from pathlib import Path

import pandas as pd

from lmc_po_price.supplier_rules import filter_articles_for_supplier
from lmc_po_price.suppliers import parse_invoice_pdf, supplier_label
from lmc_po_price.workflow import run_local_workflow


ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "echantillons" / "base_odoo" / "var_articles.data"
INVOICE = ROOT / "echantillons" / "factures" / "FC12437208.pdf"


def test_relais_vert_invoice_is_parsed_without_footer_pollution():
    invoice = parse_invoice_pdf(INVOICE)

    assert invoice.supplier_code == "254"
    assert invoice.supplier_name == "Relais Vert"
    assert invoice.invoice_number == "FC12437208"
    assert len(invoice.lines) == 6
    assert set(invoice.lines["reference_fournisseur"]) == {"AILF", "BABO", "CORO", "MEBO", "PDTAL", "TOBLE"}

    toble = invoice.lines.set_index("reference_fournisseur").loc["TOBLE"]
    assert toble["designation"] == "TOMATE ANCIENNE BLEUE CAT II"
    assert toble["remise_temp"] == 0
    assert toble["remise_detail"] == ""


def test_invoice_supplier_mismatch_blocks_analysis():
    try:
        parse_invoice_pdf(INVOICE, expected_supplier_code="244")
    except ValueError as exc:
        assert "ne correspond pas a la facture" in str(exc)
        assert "Relais Vert (254)" in str(exc)
    else:
        raise AssertionError("Expected supplier mismatch to raise ValueError")


def test_supplier_labels_are_simple_for_sidebar():
    assert supplier_label("244") == "Relais Local (244)"
    assert supplier_label("254") == "Relais Vert (254)"


def test_relais_vert_workflow_keeps_only_fruit_and_veg_matches():
    result = run_local_workflow(INVOICE, DATABASE)

    assert len(result.all_lines) == 6
    assert len(result.matched) == 2
    assert len(result.purchase_order_review) == 2
    assert set(result.matched["Fact_reference"]) == {"PDTAL", "TOBLE"}
    assert result.matched["db_categorie_mere"].eq("Fruits et légumes").all()
    assert result.purchase_order_review.loc[0, "Fournisseur/ID"] == "254"


def test_supplier_rules_filter_relais_vert_to_fruit_and_veg_only():
    articles = pd.DataFrame(
        [
            {"article_id": 1, "categorie_mere": "Fruits & Legumes"},
            {"article_id": 2, "categorie_mere": "Epicerie"},
            {"article_id": 3, "categorie_mere": "Fruits et légumes"},
        ]
    )

    filtered = filter_articles_for_supplier(articles, "254")

    assert filtered["article_id"].tolist() == [1, 3]


def test_supplier_rules_do_not_filter_other_suppliers():
    articles = pd.DataFrame(
        [
            {"article_id": 1, "categorie_mere": "Fruits & Legumes"},
            {"article_id": 2, "categorie_mere": "Epicerie"},
        ]
    )

    filtered = filter_articles_for_supplier(articles, "244")

    assert filtered["article_id"].tolist() == [1, 2]
