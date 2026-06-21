from __future__ import annotations

"""Création contrôlée de bons de commande dans Odoo."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

from lmc_po_price.odoo_articles import OdooConfig


@dataclass(frozen=True)
class PurchaseOrderSummary:
    total_lines: int
    status: str
    purchase_order_id: int | None
    purchase_order_name: str | None
    message: str
    results: pd.DataFrame


def create_purchase_order_from_review(review_rows: pd.DataFrame, config: OdooConfig) -> PurchaseOrderSummary:
    """Crée un bon de commande Odoo à partir des lignes revues.

    Les appels Odoo sont regroupés en batch pour minimiser la latence réseau :
    - 1 appel pour résoudre tous les external_ids articles
    - 1 appel pour récupérer toutes les unités de mesure
    """
    if review_rows.empty:
        return PurchaseOrderSummary(0, "error", None, None, "Aucune ligne a importer", pd.DataFrame())

    import odoorpc

    odoo = odoorpc.ODOO(config.url, port=config.port, protocol="jsonrpc+ssl")
    odoo.login(config.database, config.username, config.password)

    PurchaseOrder = odoo.env["purchase.order"]

    # Résolution du partenaire (1 appel)
    first = review_rows.iloc[0]
    supplier_ref = first.get("Fournisseur/ID")
    partner_id = _resolve_partner_id(odoo, supplier_ref)
    if partner_id is None:
        return PurchaseOrderSummary(
            len(review_rows),
            "error",
            None,
            None,
            f"Fournisseur introuvable dans Odoo: {supplier_ref}",
            pd.DataFrame(),
        )

    # Résolution batch de tous les articles (1 appel ir.model.data)
    article_external_ids = [
        str(row.get("Lignes de la commande/Article/ID", ""))
        for _, row in review_rows.iterrows()
    ]
    external_ids_to_resolve = [
        eid for eid in article_external_ids
        if eid and (eid.startswith("__") or "." in eid)
    ]
    external_id_map: dict[str, int] = {}
    if external_ids_to_resolve:
        ext_rows = odoo.env["ir.model.data"].search_read(
            [("model", "=", "product.product"), ("complete_name", "in", external_ids_to_resolve)],
            ["complete_name", "res_id"],
        )
        external_id_map = {r["complete_name"]: r["res_id"] for r in ext_rows}

    # Résolution des product_ids
    product_ids = []
    for eid in article_external_ids:
        if not eid:
            product_ids.append(None)
        elif eid in external_id_map:
            product_ids.append(external_id_map[eid])
        else:
            try:
                product_ids.append(int(float(eid)))
            except (ValueError, TypeError):
                product_ids.append(None)

    # Récupération batch des unités de mesure (1 appel product.product)
    valid_ids = [pid for pid in product_ids if pid is not None]
    uom_map: dict[int, int] = {}
    name_map: dict[int, str] = {}
    if valid_ids:
        product_rows = odoo.env["product.product"].search_read(
            [("id", "in", valid_ids)],
            ["id", "name", "uom_po_id", "uom_id"],
        )
        for p in product_rows:
            uom_po = p.get("uom_po_id")
            uom = p.get("uom_id")
            uom_map[p["id"]] = (uom_po[0] if uom_po else None) or (uom[0] if uom else None)
            name_map[p["id"]] = p.get("name", "")

    # Construction des lignes de commande
    order_lines = []
    line_results: list[dict[str, Any]] = []
    for (_, row), product_id in zip(review_rows.iterrows(), product_ids):
        if product_id is None:
            line_results.append(_line_result(row, "error", "article introuvable"))
            continue
        try:
            order_lines.append(
                (
                    0,
                    0,
                    {
                        "product_id": product_id,
                        "name": row.get("Lignes de la commande/Description") or name_map.get(product_id, ""),
                        "product_qty": float(row.get("Lignes de la commande/Quantité") or 0),
                        "price_unit": float(row.get("Lignes de la commande/Prix unitaire") or 0),
                        "product_uom": uom_map.get(product_id),
                        "date_planned": _parse_odoo_datetime(row.get("Lignes de la commande/Date prévue")),
                    },
                )
            )
            line_results.append(_line_result(row, "ready", "ligne preparee"))
        except Exception as exc:
            line_results.append(_line_result(row, "error", str(exc)))

    if not order_lines:
        return PurchaseOrderSummary(len(review_rows), "error", None, None, "Aucune ligne valide", pd.DataFrame(line_results))

    try:
        values = {
            "partner_id": partner_id,
            "order_line": order_lines,
        }
        reference = first.get("Référence commande")
        if reference:
            values["name"] = str(reference)
        purchase_order_id = PurchaseOrder.create(values)
        order = PurchaseOrder.browse(purchase_order_id)
        return PurchaseOrderSummary(
            len(review_rows),
            "success",
            purchase_order_id,
            order.name,
            f"Bon de commande cree: {order.name}",
            pd.DataFrame(line_results),
        )
    except Exception as exc:
        return PurchaseOrderSummary(len(review_rows), "error", None, None, str(exc), pd.DataFrame(line_results))


def _resolve_partner_id(odoo, value: object) -> int | None:
    if value is None or pd.isna(value) or value == "":
        return None
    text = str(value)
    if text.startswith("__") or "." in text:
        rows = odoo.env["ir.model.data"].search_read(
            [("model", "=", "res.partner"), ("complete_name", "=", text)],
            ["res_id"],
        )
        return rows[0]["res_id"] if rows else None
    try:
        return int(float(text))
    except ValueError:
        return None


def _resolve_product_id(odoo, value: object) -> int | None:
    if value is None or pd.isna(value) or value == "":
        return None
    text = str(value)
    if text.startswith("__") or "." in text:
        rows = odoo.env["ir.model.data"].search_read(
            [("model", "=", "product.product"), ("complete_name", "=", text)],
            ["res_id"],
        )
        return rows[0]["res_id"] if rows else None
    try:
        return int(float(text))
    except ValueError:
        return None


def _parse_odoo_datetime(value: object) -> str:
    if value is None or pd.isna(value) or value == "":
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = str(value)
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return text


def _line_result(row: pd.Series, status: str, message: str) -> dict[str, Any]:
    return {
        "article": row.get("Lignes de la commande/Article/ID"),
        "description": row.get("Lignes de la commande/Description"),
        "status": status,
        "message": message,
    }
