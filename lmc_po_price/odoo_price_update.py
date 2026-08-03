from __future__ import annotations

"""Préparation et écriture Odoo des changements de prix.

Le module sépare deux étapes : produire les lignes éligibles à la mise à jour,
puis écrire dans Odoo seulement après validation explicite dans l'interface.
"""

from dataclasses import dataclass
from typing import Any

import pandas as pd

from lmc_po_price.odoo_articles import OdooConfig, supplierinfo_partner_field


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


PREFLIGHT_COLUMNS = [
    *PRICE_UPDATE_COLUMNS,
    "current_cost",
    "current_supplier_price",
    "current_sale_price",
    "current_sale_ok",
    "supplierinfo_id",
    "supplierinfo_partner_field",
    "status",
    "message",
]


def prepare_odoo_price_update_rows(price_changes: pd.DataFrame) -> pd.DataFrame:
    """Filtre les changements de prix qui peuvent être écrits automatiquement."""
    if price_changes.empty:
        return pd.DataFrame(columns=PRICE_UPDATE_COLUMNS)

    eligible = price_changes[
        (price_changes["statut"] == "trouve")
        & (price_changes["prix_change"] == True)
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


def dry_run_odoo_price_updates(update_rows: pd.DataFrame, config: OdooConfig) -> OdooActionSummary:
    """Verifie les changements de prix dans Odoo sans ecrire."""
    import odoorpc

    odoo = odoorpc.ODOO(config.url, port=config.port, protocol="jsonrpc+ssl", timeout=config.timeout)
    odoo.login(config.database, config.username, config.password)

    Product = odoo.env["product.product"]
    SupplierInfo = odoo.env["product.supplierinfo"]
    supplier_partner_field = supplierinfo_partner_field(SupplierInfo)
    results = _dry_run_price_update_rows(Product, SupplierInfo, update_rows, supplier_partner_field)
    return _summary(results, success_status="ready", empty_columns=PREFLIGHT_COLUMNS)


def update_odoo_prices(update_rows: pd.DataFrame, config: OdooConfig) -> OdooActionSummary:
    """Met à jour prix fournisseur, coût, prix de vente et sale_ok dans Odoo."""
    import odoorpc

    odoo = odoorpc.ODOO(config.url, port=config.port, protocol="jsonrpc+ssl", timeout=config.timeout)
    odoo.login(config.database, config.username, config.password)

    Product = odoo.env["product.product"]
    SupplierInfo = odoo.env["product.supplierinfo"]
    supplier_partner_field = supplierinfo_partner_field(SupplierInfo)

    results: list[dict[str, Any]] = []
    for _, row in update_rows.iterrows():
        article_id = _safe_int(row.get("db_article_id"))
        supplier_id = _safe_int(row.get("db_fournisseur_id"))
        if article_id is None or supplier_id is None:
            results.append(_result(row, "error", "ID article ou fournisseur manquant"))
            continue

        try:
            new_cost = float(row["Fact_PU_unitaire"])
            new_supplier_price = float(row["Fact_PU_Net_GZ"])
            new_sale_price = float(row["New_Prix_de_vente"])
            new_sale_ok = bool(row.get("Odoo_sale_ok", True))

            product_rows = Product.read([article_id], ["id", "product_tmpl_id"])
            if not product_rows:
                results.append(_result(row, "error", "Article introuvable dans Odoo"))
                continue
            template_id = _relation_or_scalar_id(product_rows[0].get("product_tmpl_id"))
            if template_id is None:
                results.append(_result(row, "error", "Template article introuvable dans Odoo"))
                continue

            supplier_info = SupplierInfo.search_read(
                [("product_tmpl_id", "=", template_id), (supplier_partner_field, "=", supplier_id)],
                ["id"],
            )

            status = "success"
            supplier_message = "prix fournisseur mis a jour"
            if supplier_info:
                SupplierInfo.write([supplier_info[0]["id"]], {"price": new_supplier_price})
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
                    (
                        f"{supplier_message}; champ_fournisseur={supplier_partner_field}; "
                        f"cout={new_cost}; prix_vente={new_sale_price}; sale_ok={new_sale_ok}"
                    ),
                )
            )
        except Exception as exc:
            results.append(_result(row, "error", str(exc)))

    return _summary(results)


def _dry_run_price_update_rows(
    Product,
    SupplierInfo,
    update_rows: pd.DataFrame,
    supplier_partner_field: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for _, row in update_rows.iterrows():
        article_id = _safe_int(row.get("db_article_id"))
        supplier_id = _safe_int(row.get("db_fournisseur_id"))
        if article_id is None or supplier_id is None:
            results.append(
                _preflight_result(
                    row,
                    "error",
                    "ID article ou fournisseur manquant",
                    supplierinfo_partner_field=supplier_partner_field,
                )
            )
            continue

        try:
            new_cost = float(row["Fact_PU_unitaire"])
            new_supplier_price = float(row["Fact_PU_Net_GZ"])
            new_sale_price = float(row["New_Prix_de_vente"])
            new_sale_ok = bool(row.get("Odoo_sale_ok", True))

            product_rows = Product.read(
                [article_id],
                ["id", "product_tmpl_id", "standard_price", "list_price", "sale_ok"],
            )
            if not product_rows:
                results.append(
                    _preflight_result(
                        row,
                        "error",
                        "Article introuvable dans Odoo",
                        supplierinfo_partner_field=supplier_partner_field,
                    )
                )
                continue

            product = product_rows[0]
            template_id = _relation_or_scalar_id(product.get("product_tmpl_id"))
            if template_id is None:
                results.append(
                    _preflight_result(
                        row,
                        "error",
                        "Template article introuvable dans Odoo",
                        supplierinfo_partner_field=supplier_partner_field,
                    )
                )
                continue

            supplier_info = SupplierInfo.search_read(
                [("product_tmpl_id", "=", template_id), (supplier_partner_field, "=", supplier_id)],
                ["id", "price"],
            )
            if supplier_info:
                status = "ready"
                supplier_message = "pret: article et ligne fournisseur trouves"
                supplierinfo_id = supplier_info[0].get("id")
                current_supplier_price = supplier_info[0].get("price")
            else:
                status = "warning"
                supplier_message = (
                    "ligne fournisseur introuvable; "
                    "l'ecriture mettrait seulement a jour l'article"
                )
                supplierinfo_id = None
                current_supplier_price = None

            results.append(
                _preflight_result(
                    row,
                    status,
                    (
                        f"{supplier_message}; champ_fournisseur={supplier_partner_field}; "
                        f"cout {product.get('standard_price')} -> {new_cost}; "
                        f"prix_fournisseur {current_supplier_price} -> {new_supplier_price}; "
                        f"prix_vente {product.get('list_price')} -> {new_sale_price}; "
                        f"sale_ok {product.get('sale_ok')} -> {new_sale_ok}"
                    ),
                    current_cost=product.get("standard_price"),
                    current_supplier_price=current_supplier_price,
                    current_sale_price=product.get("list_price"),
                    current_sale_ok=product.get("sale_ok"),
                    supplierinfo_id=supplierinfo_id,
                    supplierinfo_partner_field=supplier_partner_field,
                )
            )
        except Exception as exc:
            results.append(_preflight_result(row, "error", str(exc), supplierinfo_partner_field=supplier_partner_field))
    return results


def _summary(
    results: list[dict[str, Any]],
    success_status: str = "success",
    empty_columns: list[str] | None = None,
) -> OdooActionSummary:
    result_df = pd.DataFrame(results)
    if result_df.empty:
        result_df = pd.DataFrame(columns=empty_columns or [*PRICE_UPDATE_COLUMNS, "status", "message"])
    return OdooActionSummary(
        total=len(result_df),
        success=int((result_df["status"] == success_status).sum()) if not result_df.empty else 0,
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


def _preflight_result(
    row: pd.Series,
    status: str,
    message: str,
    *,
    current_cost: object = None,
    current_supplier_price: object = None,
    current_sale_price: object = None,
    current_sale_ok: object = None,
    supplierinfo_id: object = None,
    supplierinfo_partner_field: object = None,
) -> dict[str, Any]:
    return {
        **{column: row.get(column) for column in PRICE_UPDATE_COLUMNS},
        "current_cost": current_cost,
        "current_supplier_price": current_supplier_price,
        "current_sale_price": current_sale_price,
        "current_sale_ok": current_sale_ok,
        "supplierinfo_id": supplierinfo_id,
        "supplierinfo_partner_field": supplierinfo_partner_field,
        "status": status,
        "message": message,
    }


def _relation_or_scalar_id(value: object) -> int | None:
    if isinstance(value, (list, tuple)) and value:
        return _safe_int(value[0])
    return _safe_int(value)


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
