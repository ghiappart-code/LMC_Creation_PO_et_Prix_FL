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

    L'appelant doit demander une confirmation utilisateur avant d'exécuter cette
    fonction. La référence commande est fournie par la facture; Odoo conserve sa
    séquence interne seulement si le champ name n'est pas écrit.
    """
    if review_rows.empty:
        return PurchaseOrderSummary(0, "error", None, None, "Aucune ligne a importer", pd.DataFrame())

    import odoorpc

    odoo = odoorpc.ODOO(config.url, port=config.port, protocol="jsonrpc+ssl")
    odoo.login(config.database, config.username, config.password)

    PurchaseOrder = odoo.env["purchase.order"]
    Product = odoo.env["product.product"]

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

    order_lines = []
    line_results: list[dict[str, Any]] = []
    for _, row in review_rows.iterrows():
        article_ref = row.get("Lignes de la commande/Article/ID")
        product_id = _resolve_product_id(odoo, article_ref)
        if product_id is None:
            line_results.append(_line_result(row, "error", "article introuvable"))
            continue
        try:
            product = Product.browse(product_id)
            product_name = product.name
            order_lines.append(
                (
                    0,
                    0,
                    {
                        "product_id": product_id,
                        "name": row.get("Lignes de la commande/Description") or product_name,
                        "product_qty": float(row.get("Lignes de la commande/Quantité")),
                        "price_unit": float(row.get("Lignes de la commande/Prix unitaire")),
                        "product_uom": product.uom_po_id.id or product.uom_id.id,
                        "date_planned": _parse_odoo_datetime(row.get("Lignes de la commande/Date prévue")),
                    },
                )
            )
            line_results.append(
                _line_result(
                    row,
                    "ready",
                    "ligne preparee",
                    product_id_resolu=product_id,
                    nom_article_resolu=product_name,
                )
            )
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
        created_lines = _read_created_order_lines(odoo, purchase_order_id)
        enriched_results = _merge_created_line_diagnostics(line_results, created_lines)
        return PurchaseOrderSummary(
            len(review_rows),
            "success",
            purchase_order_id,
            order.name,
            f"Bon de commande cree: {order.name}",
            pd.DataFrame(enriched_results),
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
        module, name = _split_external_id(text)
        if module is None or name is None:
            return None
        rows = odoo.env["ir.model.data"].search_read(
            [("model", "=", "product.product"), ("module", "=", module), ("name", "=", name)],
            ["res_id"],
        )
        return rows[0]["res_id"] if rows else None
    try:
        return int(float(text))
    except ValueError:
        return None


def _split_external_id(value: str) -> tuple[str | None, str | None]:
    if "." not in value:
        return None, None
    module, name = value.split(".", 1)
    if not module or not name:
        return None, None
    return module, name


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


def _read_created_order_lines(odoo, purchase_order_id: int) -> list[dict[str, Any]]:
    line_ids = odoo.env["purchase.order.line"].search([("order_id", "=", purchase_order_id)])
    if not line_ids:
        return []
    return odoo.env["purchase.order.line"].read(
        line_ids,
        ["product_id", "name", "product_qty", "price_unit"],
    )


def _merge_created_line_diagnostics(
    line_results: list[dict[str, Any]],
    created_lines: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    ready_index = 0
    output: list[dict[str, Any]] = []
    for result in line_results:
        enriched = dict(result)
        if result.get("status") == "ready" and ready_index < len(created_lines):
            created_line = created_lines[ready_index]
            product_id = created_line.get("product_id")
            enriched.update(
                {
                    "product_id_lu_dans_po_odoo": _relation_id(product_id),
                    "nom_article_lu_dans_po_odoo": _relation_name(product_id),
                    "description_lue_dans_po_odoo": created_line.get("name"),
                    "quantite_lue_dans_po_odoo": created_line.get("product_qty"),
                    "prix_lu_dans_po_odoo": created_line.get("price_unit"),
                }
            )
            ready_index += 1
        output.append(enriched)
    return output


def _relation_id(value: object) -> int | None:
    return value[0] if isinstance(value, (list, tuple)) and value else None


def _relation_name(value: object) -> str | None:
    return value[1] if isinstance(value, (list, tuple)) and len(value) > 1 else None


def _line_result(
    row: pd.Series,
    status: str,
    message: str,
    product_id_resolu: int | None = None,
    nom_article_resolu: str | None = None,
) -> dict[str, Any]:
    return {
        "article_external_id_demande": row.get("Lignes de la commande/Article/ID"),
        "product_id_resolu_avant_creation": product_id_resolu,
        "nom_article_resolu_avant_creation": nom_article_resolu,
        "description": row.get("Lignes de la commande/Description"),
        "status": status,
        "message": message,
    }
