from __future__ import annotations

"""Préparation et écriture Odoo des changements de prix.

Le module sépare deux étapes : produire les lignes éligibles à la mise à jour,
puis écrire dans Odoo seulement après validation explicite dans l'interface.
"""

from dataclasses import dataclass
from typing import Any

import pandas as pd

from lmc_po_price.odoo_articles import OdooConfig


PRICE_UPDATE_COLUMNS = [
    "db_id_externe",
    "db_article_id",
    "db_fournisseur_id",
    "Fact_PU_unitaire",
    "Fact_PU_Net_GZ",
    "New_Prix_de_vente",
    "Odoo_sale_ok",
]


@dataclass(frozen=True)
class OdooActionSummary:
    total: int
    success: int
    warnings: int
    errors: int
    results: pd.DataFrame


def prepare_odoo_price_update_rows(price_changes: pd.DataFrame) -> pd.DataFrame:
    """Filtre les changements de prix qui peuvent être écrits automatiquement."""
    if price_changes.empty:
        return pd.DataFrame(columns=PRICE_UPDATE_COLUMNS)

    eligible = price_changes[
        (price_changes["statut"] == "trouve")
        & (price_changes["prix_change"] == True)
        & (price_changes["ecart_anormal"] == False)
        & (price_changes["peut_etre_mis_a_jour"] == True)
    ].copy()
    if eligible.empty:
        return pd.DataFrame(columns=PRICE_UPDATE_COLUMNS)

    update_rows = eligible[PRICE_UPDATE_COLUMNS].copy()
    update_rows = update_rows.dropna(
        subset=["db_article_id", "db_fournisseur_id", "Fact_PU_unitaire", "Fact_PU_Net_GZ", "New_Prix_de_vente"]
    )
    update_rows = update_rows[update_rows["New_Prix_de_vente"].map(_is_number)]
    if update_rows.empty:
        return pd.DataFrame(columns=PRICE_UPDATE_COLUMNS)
    return update_rows


def update_odoo_prices(update_rows: pd.DataFrame, config: OdooConfig) -> OdooActionSummary:
    """Met à jour prix fournisseur, coût, prix de vente et sale_ok dans Odoo.

    Les appels Odoo sont regroupés en batch :
    - 1 appel pour récupérer tous les template_id des produits
    - 1 appel pour récupérer toutes les lignes fournisseur (supplierinfo)
    - 1 appel write par produit (inévitable car les valeurs diffèrent)
    """
    import odoorpc

    odoo = odoorpc.ODOO(config.url, port=config.port, protocol="jsonrpc+ssl")
    odoo.login(config.database, config.username, config.password)

    Product = odoo.env["product.product"]
    SupplierInfo = odoo.env["product.supplierinfo"]

    # Batch : récupérer tous les template_id en un seul appel
    article_ids = [_safe_int(row.get("db_article_id")) for _, row in update_rows.iterrows()]
    valid_article_ids = [aid for aid in article_ids if aid is not None]
    template_map: dict[int, int] = {}
    if valid_article_ids:
        product_data = Product.search_read(
            [("id", "in", valid_article_ids)],
            ["id", "product_tmpl_id"],
        )
        template_map = {p["id"]: p["product_tmpl_id"][0] for p in product_data if p.get("product_tmpl_id")}

    # Batch : récupérer toutes les lignes fournisseur en un seul appel
    template_ids = list(template_map.values())
    supplier_ids_list = [_safe_int(row.get("db_fournisseur_id")) for _, row in update_rows.iterrows()]
    unique_supplier_ids = list({sid for sid in supplier_ids_list if sid is not None})
    supplier_info_map: dict[tuple[int, int], int] = {}
    if template_ids and unique_supplier_ids:
        supplier_rows = SupplierInfo.search_read(
            [("product_tmpl_id", "in", template_ids), ("name", "in", unique_supplier_ids)],
            ["id", "product_tmpl_id", "name"],
        )
        for s in supplier_rows:
            tmpl_id = s["product_tmpl_id"][0] if s.get("product_tmpl_id") else None
            name_id = s["name"][0] if s.get("name") else None
            if tmpl_id and name_id:
                supplier_info_map[(tmpl_id, name_id)] = s["id"]

    # Mise à jour ligne par ligne (les valeurs diffèrent pour chaque produit)
    results: list[dict[str, Any]] = []
    for (_, row), article_id, supplier_id in zip(update_rows.iterrows(), article_ids, supplier_ids_list):
        if article_id is None or supplier_id is None:
            results.append(_result(row, "error", "ID article ou fournisseur manquant"))
            continue
        try:
            new_cost = float(row["Fact_PU_unitaire"])
            new_supplier_price = float(row["Fact_PU_Net_GZ"])
            new_sale_price = float(row["New_Prix_de_vente"])
            new_sale_ok = bool(row.get("Odoo_sale_ok", True))

            template_id = template_map.get(article_id)
            status = "success"
            supplier_message = "prix fournisseur mis a jour"
            if template_id and (template_id, supplier_id) in supplier_info_map:
                SupplierInfo.write([supplier_info_map[(template_id, supplier_id)]], {"price": new_supplier_price})
            else:
                status = "warning"
                supplier_message = "ligne fournisseur introuvable; produit mis a jour seulement"

            Product.write(
                [article_id],
                {
                    "standard_price": new_cost,
                    "list_price": new_sale_price,
                    "sale_ok": new_sale_ok,
                },
            )
            results.append(
                _result(
                    row,
                    status,
                    f"{supplier_message}; cout={new_cost}; prix_vente={new_sale_price}; sale_ok={new_sale_ok}",
                )
            )
        except Exception as exc:
            results.append(_result(row, "error", str(exc)))

    return _summary(results)


def _summary(results: list[dict[str, Any]]) -> OdooActionSummary:
    result_df = pd.DataFrame(results)
    if result_df.empty:
        result_df = pd.DataFrame(columns=[*PRICE_UPDATE_COLUMNS, "status", "message"])
    return OdooActionSummary(
        total=len(result_df),
        success=int((result_df["status"] == "success").sum()) if not result_df.empty else 0,
        warnings=int((result_df["status"] == "warning").sum()) if not result_df.empty else 0,
        errors=int((result_df["status"] == "error").sum()) if not result_df.empty else 0,
        results=result_df,
    )


def _result(row: pd.Series, status: str, message: str) -> dict[str, Any]:
    return {
        **{column: row.get(column) for column in PRICE_UPDATE_COLUMNS},
        "status": status,
        "message": message,
    }


def _safe_int(value: object) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _is_number(value: object) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False

