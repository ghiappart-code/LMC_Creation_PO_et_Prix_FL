"""Orchestration commune facture + base articles.

Le workflow reçoit une facture déjà décodable par un parseur fournisseur et une
base articles normalisée. Il produit les tableaux de revue utilisés aussi bien
pour la mise à jour prix que pour le brouillon de bon de commande.
"""

from __future__ import annotations

import pandas as pd

from lmc_po_price.database import load_article_database
from lmc_po_price.invoice_parsing import parse_invoice_pdf
from lmc_po_price.matching import match_invoice_to_articles
from lmc_po_price.models import WorkflowConfig, WorkflowResult
from lmc_po_price.pricing import UNKNOWN_MARGIN_MESSAGE
from lmc_po_price.purchase_order import prepare_purchase_order_review
from lmc_po_price.supplier_rules import filter_articles_for_supplier


UNMATCHED_COLUMNS = [
    "Fact_reference",
    "Fact_designation",
    "Fact_PU_Net",
    "Fact_PU_Net_GZ",
    "Fact_PU_unitaire",
    "Match_Methode",
    "Matchs_possibles",
    "Fact_quantite",
    "Fact_unite",
    "Fact_montant_HT",
    "raison_revue",
]

AMBIGUOUS_COLUMNS_FIRST = [
    "Fact_reference",
    "Fact_designation",
    "raison_revue",
    "Matchs_possibles",
]


def run_local_workflow(invoice_path, database_path, config: WorkflowConfig | None = None) -> WorkflowResult:
    """Exécute le workflow complet à partir de fichiers locaux de test."""
    invoice = parse_invoice_pdf(invoice_path)
    articles = load_article_database(database_path)
    workflow_config = config or WorkflowConfig(supplier_code=invoice.supplier_code)
    articles = filter_articles_for_supplier(articles, workflow_config.supplier_code)
    all_lines = match_invoice_to_articles(invoice.lines, articles, workflow_config)
    matched = all_lines[all_lines["statut"] == "trouve"].copy()
    unmatched = all_lines[all_lines["statut"] == "non_trouve"].copy()
    ambiguous = all_lines[all_lines["statut"] == "a_verifier"].copy()
    price_changes = matched[matched["prix_change"]].copy()
    purchase_order_review = _purchase_order_review(
        all_lines,
        invoice.invoice_number,
        invoice.delivery_date,
    )
    return WorkflowResult(
        invoice=invoice,
        all_lines=all_lines,
        matched=matched,
        unmatched=unmatched,
        ambiguous=ambiguous,
        price_changes=price_changes,
        purchase_order_review=purchase_order_review,
        sale_flag_review=pd.DataFrame(),
    )


def _purchase_order_review(
    all_lines: pd.DataFrame,
    invoice_number: str | None = None,
    delivery_date=None,
) -> pd.DataFrame:
    """Prépare l'onglet de revue du futur bon de commande."""
    return prepare_purchase_order_review(
        all_lines,
        order_reference=invoice_number or "Facture sans numéro",
        planned_date=delivery_date,
    )


def unmatched_review(unmatched: pd.DataFrame) -> pd.DataFrame:
    available = [column for column in UNMATCHED_COLUMNS if column in unmatched]
    return unmatched[available].copy()


def ambiguous_review(ambiguous: pd.DataFrame) -> pd.DataFrame:
    ordered_columns = [column for column in AMBIGUOUS_COLUMNS_FIRST if column in ambiguous]
    ordered_columns.extend(column for column in ambiguous.columns if column not in ordered_columns)
    return ambiguous[ordered_columns].copy()


def blocked_price_changes(price_changes: pd.DataFrame) -> pd.DataFrame:
    """Retourne les changements dont le prix de vente ne peut pas etre calcule."""
    required = {"prix_change", "New_Prix_de_vente"}
    if price_changes.empty or not required.issubset(price_changes.columns):
        return pd.DataFrame(columns=price_changes.columns)
    blocked = price_changes[
        (price_changes["prix_change"] == True)
        & (price_changes["New_Prix_de_vente"] == UNKNOWN_MARGIN_MESSAGE)
    ].copy()
    if blocked.empty or "raison_revue" not in blocked:
        return blocked
    return blocked[["raison_revue", *[column for column in blocked.columns if column != "raison_revue"]]]
