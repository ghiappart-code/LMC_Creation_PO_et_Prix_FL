"""Exports Excel/CSV pour les fichiers de contrôle utilisateur.

Ce module ne contient aucune écriture Odoo. Il met en forme les DataFrames issus
du workflow en onglets de revue lisibles, avec les formats numériques attendus.
"""

from __future__ import annotations

from io import BytesIO

import pandas as pd
from openpyxl.styles import Alignment

from lmc_po_price.models import WorkflowResult
from lmc_po_price.workflow import unmatched_review


def result_workbook_bytes(
    result: WorkflowResult,
    include_purchase_order: bool = True,
    include_price_review: bool = True,
) -> bytes:
    """Construit le classeur de contrôle selon les tâches sélectionnées."""
    sheets = {
        "tous_les_articles_facture": result.all_lines,
        "articles_trouves": result.matched,
        "articles_non_retrouves": unmatched_review(result.unmatched),
        "articles_ambigus": result.ambiguous,
    }
    if include_price_review:
        sheets["changements_prix"] = result.price_changes
    if include_purchase_order:
        sheets["bon_commande_a_verifier"] = result.purchase_order_review
    sheets["notes"] = _notes()
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, df in sheets.items():
            df.to_excel(writer, index=False, sheet_name=name[:31])
        _format_workbook(writer)
    return buffer.getvalue()


def _notes() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"champ": "Fact_PU_Net_GZ", "description": "Prix facture augmente du taux de contribution transport, par exemple 0,6%."},
            {"champ": "Fact_PU_unitaire", "description": "Prix facture converti dans l'unite Odoo lorsque la base fournit un ratio d'unite fournisseur."},
            {"champ": "statut", "description": "trouve, non_trouve ou a_verifier."},
            {"champ": "peut_etre_mis_a_jour", "description": "Vrai seulement si le match est certain et si l'ecart n'est pas anormal."},
            {"champ": "Odoo_sale_ok", "description": "Valeur a envoyer vers Odoo pour sale_ok lors d'une future mise a jour prix eligible."},
            {"champ": "db_vente_ok", "description": "Champ Odoo sale_ok renomme en francais dans la base locale."},
        ]
    )


def _format_workbook(writer: pd.ExcelWriter) -> None:
    number_formats = {
        "Fact_PU_Net": "0.00",
        "Fact_PU_Net_GZ": "0.00",
        "Fact_PU_unitaire": "0.00",
        "DB_Prix_Net": "0.00",
        "Ecart_Prix": "0.00",
        "New_Prix_de_vente": "0.00",
        "Fact_montant_HT": "0.00",
        "db_cout": "0.00",
        "db_prix_fournisseur": "0.00",
    }
    for ws in writer.book.worksheets:
        ws.freeze_panes = "A2"
        headers = [cell.value for cell in ws[1]]
        for column_cells in ws.columns:
            header = str(column_cells[0].value or "")
            width = max(12, min(48, len(header) + 2))
            ws.column_dimensions[column_cells[0].column_letter].width = width
            if header in number_formats:
                for cell in column_cells[1:]:
                    cell.number_format = number_formats[header]
            if header == "Matchs_possibles":
                ws.column_dimensions[column_cells[0].column_letter].width = 80
                for cell in column_cells:
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
