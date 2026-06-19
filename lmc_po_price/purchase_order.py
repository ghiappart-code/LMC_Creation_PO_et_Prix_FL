from __future__ import annotations

from datetime import date, datetime

import pandas as pd


ODOO_PO_COLUMNS = [
    "ID Externe",
    "Référence commande",
    "Fournisseur/ID",
    "Lignes de la commande/Description",
    "Lignes de la commande/Article/ID",
    "Lignes de la commande/Unité de mesure d'article",
    "Lignes de la commande/Quantité",
    "Lignes de la commande/Prix unitaire",
    "Lignes de la commande/Taxes/ID",
    "Lignes de la commande/Date prévue",
]

DEFAULT_PURCHASE_TAX_ID = "l10n_fr.1_tva_acq_reduite"


def prepare_purchase_order_review(
    matched_lines: pd.DataFrame,
    supplier_external_id: str | None = None,
    order_reference: str = "A définir",
    planned_date: date | datetime | None = None,
) -> pd.DataFrame:
    """Prépare un brouillon de bon de commande au format import Odoo.

    Cette fonction reste en mode contrôle/revue : elle ne crée rien dans Odoo.
    Elle produit un DataFrame dont les colonnes reprennent le CSV d'import fourni
    comme modèle. Les lignes non matchées ou ambiguës sont exclues, car elles ne
    contiennent pas encore l'ID article Odoo fiable.
    """
    eligible = matched_lines[matched_lines["statut"] == "trouve"].copy()
    if eligible.empty:
        return pd.DataFrame(columns=ODOO_PO_COLUMNS)

    planned_date_text = _format_planned_date(planned_date)
    rows: list[dict[str, object]] = []
    for index, (_, row) in enumerate(eligible.iterrows()):
        rows.append(
            {
                "ID Externe": "",
                "Référence commande": order_reference if index == 0 else "",
                "Fournisseur/ID": supplier_external_id or _supplier_external_id(row) if index == 0 else "",
                "Lignes de la commande/Description": _line_description(row),
                "Lignes de la commande/Article/ID": row.get("db_id_externe") or "",
                "Lignes de la commande/Unité de mesure d'article": row.get("Fact_unite") or row.get("db_unite") or "",
                "Lignes de la commande/Quantité": row.get("Fact_quantite"),
                "Lignes de la commande/Prix unitaire": row.get("Fact_PU_unitaire"),
                "Lignes de la commande/Taxes/ID": DEFAULT_PURCHASE_TAX_ID,
                "Lignes de la commande/Date prévue": planned_date_text,
            }
        )
    return pd.DataFrame(rows, columns=ODOO_PO_COLUMNS)


def prepare_purchase_order_import_csv(review_rows: pd.DataFrame) -> bytes:
    """Convertit le brouillon PO en CSV compatible import Odoo.

    L'encodage `latin1` et le séparateur `;` suivent le fichier d'exemple fourni.
    """
    return review_rows.to_csv(index=False, sep=";", encoding="latin1").encode("latin1")


def _line_description(row: pd.Series) -> str:
    reference = row.get("Fact_reference") or ""
    designation = row.get("Fact_designation") or row.get("db_designation") or ""
    return f"[{reference}] {designation}".strip()


def _supplier_external_id(row: pd.Series) -> str:
    external_id = row.get("db_fournisseur_id_externe")
    if external_id:
        return str(external_id)
    supplier_id = row.get("db_fournisseur_id")
    if pd.isna(supplier_id):
        return ""
    return str(int(float(supplier_id)))


def _format_planned_date(value: date | datetime | None) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y %H:%M")
    return value.strftime("%d/%m/%Y %H:%M")
