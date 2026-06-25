from pathlib import Path

import pandas as pd

from lmc_po_price.database import load_article_database
from lmc_po_price.invoice_parsing import parse_invoice_pdf
from lmc_po_price.odoo_price_update import prepare_odoo_price_update_rows
from lmc_po_price.purchase_order import ODOO_PO_COLUMNS, prepare_purchase_order_review
from lmc_po_price.workflow import run_local_workflow


ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "echantillons" / "base_odoo" / "var_articles.data"
INVOICE = ROOT / "echantillons" / "factures" / "FC260642740.pdf"


def test_database_contains_vente_ok():
    articles = load_article_database(DATABASE)

    assert "vente_ok" in articles.columns
    assert len(articles) == 3722


def test_le_relais_local_invoice_excludes_transport_from_products():
    invoice = parse_invoice_pdf(INVOICE)

    assert invoice.invoice_number == "FC260642740"
    assert len(invoice.lines) == 27
    assert len(invoice.charges) == 1
    assert invoice.metadata["taux_transport"] == 0.006
    assert "GASOI" not in set(invoice.lines["reference_fournisseur"])
    assert invoice.lines.loc[0, "unite"] == "U"
    assert invoice.lines.loc[1, "unite"] == "KG"


def test_local_workflow_matches_known_sample_lines():
    result = run_local_workflow(INVOICE, DATABASE)

    assert len(result.all_lines) == 27
    assert len(result.matched) == 4
    assert len(result.unmatched) == 23
    assert len(result.ambiguous) == 0
    assert "120235" in set(result.price_changes["Fact_reference"])
    assert not result.price_changes["Odoo_sale_ok"].eq(True).all()


def test_unmatched_suggestions_are_conservative():
    result = run_local_workflow(INVOICE, DATABASE)
    suggestions = result.unmatched.set_index("Fact_reference")["Matchs_possibles"]

    assert "BROCOLI" in suggestions.loc["120323"]
    assert "CAROTTE LAVEE" in suggestions.loc["120147"]
    assert "ESSUIE TOUT" in suggestions.loc["101551"]


def test_purchase_order_review_uses_odoo_import_columns():
    result = run_local_workflow(INVOICE, DATABASE)

    assert list(result.purchase_order_review.columns) == ODOO_PO_COLUMNS
    assert len(result.purchase_order_review) == len(result.matched)
    assert result.purchase_order_review.loc[0, "Référence commande"] == "FC260642740"
    assert result.purchase_order_review.loc[0, "Fournisseur/ID"] == "244"
    assert result.purchase_order_review["Lignes de la commande/Date prévue"].eq("19/06/2026 00:00").all()


def test_purchase_order_review_uses_numeric_supplier_id_over_external_id():
    rows = pd.DataFrame(
        [
            {
                "statut": "trouve",
                "db_fournisseur_id": 244.0,
                "db_fournisseur_id_externe": "__export__.res_partner_244_contact",
                "db_id_externe": "__export__.product_product_5015_3bb4a2bc",
                "Fact_reference": "120153",
                "Fact_designation": "BETTERAVE CUITE SS VIDE 500G BIO",
                "Fact_unite": "U",
                "Fact_quantite": 24,
                "Fact_PU_unitaire": 1.3581,
            }
        ]
    )

    review = prepare_purchase_order_review(rows)

    assert review.loc[0, "Fournisseur/ID"] == "244"


def test_price_update_rows_are_prepared_for_odoo():
    result = run_local_workflow(INVOICE, DATABASE)

    update_rows = prepare_odoo_price_update_rows(result.price_changes)

    assert update_rows.empty
    assert {"db_article_id", "db_fournisseur_id", "Fact_PU_unitaire", "New_Prix_de_vente"}.issubset(update_rows.columns)
