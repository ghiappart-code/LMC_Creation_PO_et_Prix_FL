from __future__ import annotations

"""Generation controlee des etiquettes Fruits et Legumes dans Odoo."""

from dataclasses import dataclass
from http.cookiejar import CookieJar
import json
from typing import Any
from urllib.parse import quote
from urllib.request import HTTPCookieProcessor, Request, build_opener

import pandas as pd

from lmc_po_price.odoo_articles import OdooConfig
from lmc_po_price.odoo_purchase_order import _connect_odoo


DEFAULT_LABEL_PRINT_CATEGORY_ID = 14
DEFAULT_LABEL_REPORT_NAME = "product_print_category.report_pricetag"


@dataclass(frozen=True)
class LabelDryRunSummary:
    total_lines: int
    ready: int
    excluded: int
    errors: int
    status: str
    message: str
    labels: pd.DataFrame
    excluded_lines: pd.DataFrame


@dataclass(frozen=True)
class LabelPdfSummary:
    total_labels: int
    status: str
    message: str
    pdf: bytes | None
    labels: pd.DataFrame
    excluded_lines: pd.DataFrame


def dry_run_labels_from_invoice_lines(
    invoice_rows: pd.DataFrame,
    config: OdooConfig,
    print_category_id: int = DEFAULT_LABEL_PRINT_CATEGORY_ID,
) -> LabelDryRunSummary:
    """Prepare les etiquettes sans creer de wizard ni de PDF."""
    odoo = _connect_odoo(config)
    labels, excluded = _prepare_label_rows(invoice_rows, odoo, print_category_id)
    errors = int((excluded["status"] == "error").sum()) if not excluded.empty else 0
    status = "ready" if not labels.empty and errors == 0 else "error"
    message = (
        f"Verification etiquettes terminee : {int(labels['nb_etiquettes'].sum()) if not labels.empty else 0} "
        f"etiquette(s), {len(excluded)} ligne(s) exclue(s), {errors} erreur(s)."
    )
    return LabelDryRunSummary(
        total_lines=len(_eligible_invoice_rows(invoice_rows)),
        ready=len(labels),
        excluded=len(excluded),
        errors=errors,
        status=status,
        message=message,
        labels=labels,
        excluded_lines=excluded,
    )


def generate_labels_pdf_from_invoice_lines(
    invoice_rows: pd.DataFrame,
    config: OdooConfig,
    print_category_id: int = DEFAULT_LABEL_PRINT_CATEGORY_ID,
    report_name: str = DEFAULT_LABEL_REPORT_NAME,
) -> LabelPdfSummary:
    """Cree le wizard transitoire Odoo et retourne le PDF des etiquettes."""
    odoo = _connect_odoo(config)
    labels, excluded = _prepare_label_rows(invoice_rows, odoo, print_category_id)
    errors = int((excluded["status"] == "error").sum()) if not excluded.empty else 0
    if labels.empty:
        return LabelPdfSummary(
            0,
            "error",
            "Aucune etiquette Fruits et Legumes a generer.",
            None,
            labels,
            excluded,
        )
    if errors:
        return LabelPdfSummary(
            int(labels["nb_etiquettes"].sum()),
            "error",
            "Generation bloquee : certaines lignes Fruits et Legumes n'ont pas de nombre de colis exploitable.",
            None,
            labels,
            excluded,
        )

    wizard_lines = [
        (
            0,
            0,
            {
                "product_id": int(row["article_id"]),
                "quantity": int(row["nb_etiquettes"]),
                "print_category_id": print_category_id,
            },
        )
        for _, row in labels.iterrows()
    ]
    wizard_id = odoo.env["product.print.wizard"].create({"line_ids": wizard_lines})
    line_ids = odoo.env["product.print.wizard.line"].search([("wizard_id", "=", wizard_id)])
    pdf = _render_report_pdf(config, report_name, ",".join(map(str, line_ids)))
    return LabelPdfSummary(
        int(labels["nb_etiquettes"].sum()),
        "success",
        f"PDF etiquettes genere : {int(labels['nb_etiquettes'].sum())} etiquette(s).",
        pdf,
        labels,
        excluded,
    )


def _prepare_label_rows(
    invoice_rows: pd.DataFrame,
    odoo,
    print_category_id: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows = _eligible_invoice_rows(invoice_rows)
    if rows.empty:
        return _labels_dataframe([]), _excluded_dataframe([])

    product_ids = sorted({int(value) for value in rows["db_article_id"].dropna()})
    products = _read_products(odoo, product_ids)
    labels: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    for _, row in rows.iterrows():
        product_id = _int_or_none(row.get("db_article_id"))
        if product_id is None:
            excluded.append(_excluded_row(row, "error", "article Odoo manquant"))
            continue
        product = products.get(product_id)
        if product is None:
            excluded.append(_excluded_row(row, "error", "article absent d'Odoo"))
            continue
        product_category_id = _relation_id(product.get("print_category_id"))
        product_category_name = _relation_name(product.get("print_category_id")) or "AUCUNE"
        if product_category_id != print_category_id:
            excluded.append(
                _excluded_row(
                    row,
                    "excluded",
                    "categorie d'impression differente",
                    product_category=product_category_name,
                )
            )
            continue
        label_count = _int_or_none(row.get("Fact_nb_colis"))
        if label_count is None or label_count <= 0:
            excluded.append(
                _excluded_row(
                    row,
                    "error",
                    "nombre de colis facture manquant ou nul",
                    product_category=product_category_name,
                )
            )
            continue
        labels.append(
            {
                "article_id": product_id,
                "reference": row.get("Fact_reference"),
                "article": row.get("Fact_designation") or product.get("display_name"),
                "qte_commande": row.get("Fact_quantite"),
                "uom": row.get("Fact_unite"),
                "categorie_impression": product_category_name,
                "nb_colis_facture": label_count,
                "nb_etiquettes": label_count,
            }
        )

    return _labels_dataframe(labels), _excluded_dataframe(excluded)


def _eligible_invoice_rows(invoice_rows: pd.DataFrame) -> pd.DataFrame:
    if invoice_rows.empty or "statut" not in invoice_rows:
        return pd.DataFrame()
    rows = invoice_rows[invoice_rows["statut"] == "trouve"].copy()
    if "Fact_quantite" in rows:
        rows = rows[pd.to_numeric(rows["Fact_quantite"], errors="coerce").fillna(0) > 0]
    if "db_article_id" in rows:
        rows = rows[rows["db_article_id"].notna()]
    return rows


def _read_products(odoo, product_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not product_ids:
        return {}
    products = odoo.env["product.product"].read(product_ids, ["id", "display_name", "print_category_id"])
    return {int(product["id"]): product for product in products}


def _excluded_row(
    row: pd.Series,
    status: str,
    reason: str,
    product_category: str = "",
) -> dict[str, Any]:
    return {
        "status": status,
        "raison": reason,
        "article_id": row.get("db_article_id"),
        "reference": row.get("Fact_reference"),
        "article": row.get("Fact_designation"),
        "qte_commande": row.get("Fact_quantite"),
        "uom": row.get("Fact_unite"),
        "nb_colis_facture": row.get("Fact_nb_colis"),
        "categorie_impression": product_category,
    }


def _labels_dataframe(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        rows,
        columns=[
            "article_id",
            "reference",
            "article",
            "qte_commande",
            "uom",
            "categorie_impression",
            "nb_colis_facture",
            "nb_etiquettes",
        ],
    )


def _excluded_dataframe(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        rows,
        columns=[
            "status",
            "raison",
            "article_id",
            "reference",
            "article",
            "qte_commande",
            "uom",
            "nb_colis_facture",
            "categorie_impression",
        ],
    )


def _render_report_pdf(config: OdooConfig, report_name: str, ids: str) -> bytes:
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    base_url = _base_url(config)
    auth_payload = {
        "jsonrpc": "2.0",
        "params": {
            "db": config.database,
            "login": config.username,
            "password": config.password,
        },
    }
    auth_request = Request(
        f"{base_url}/web/session/authenticate",
        data=json.dumps(auth_payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    auth_response = opener.open(auth_request, timeout=config.timeout)
    auth_data = json.loads(auth_response.read().decode("utf-8"))
    if auth_data.get("error"):
        raise RuntimeError(auth_data["error"])
    if not auth_data.get("result", {}).get("uid"):
        raise RuntimeError("Authentification Odoo refusee.")

    report_path = quote(report_name, safe=".")
    ids_path = quote(ids, safe=",")
    pdf_request = Request(f"{base_url}/report/pdf/{report_path}/{ids_path}", method="GET")
    pdf_response = opener.open(pdf_request, timeout=config.timeout)
    content_type = pdf_response.headers.get("Content-Type", "")
    pdf = pdf_response.read()
    if "pdf" not in content_type.lower() and not pdf.startswith(b"%PDF"):
        raise RuntimeError(f"Reponse inattendue d'Odoo: Content-Type={content_type!r}, taille={len(pdf)}")
    return pdf


def _base_url(config: OdooConfig) -> str:
    host = config.url
    if host.startswith(("http://", "https://")):
        base = host.rstrip("/")
    else:
        scheme = "https" if config.port == 443 else "http"
        base = f"{scheme}://{host}"
    if config.port in (80, 443):
        return base
    return f"{base}:{config.port}"


def _int_or_none(value: object) -> int | None:
    if value is None or pd.isna(value):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not numeric.is_integer():
        return None
    return int(numeric)


def _relation_id(value: object) -> int | None:
    if isinstance(value, (list, tuple)) and value:
        return int(value[0])
    return None


def _relation_name(value: object) -> str:
    if isinstance(value, (list, tuple)) and len(value) > 1:
        return str(value[1])
    return ""
