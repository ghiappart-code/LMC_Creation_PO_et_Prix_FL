"""Extraction en lecture seule de la base articles depuis Odoo.

Ce module récupère les articles, fournisseurs, taxes, catégories et le flag
`sale_ok`, puis sauvegarde une base locale utilisée par l'application. Les mises
à jour Odoo seront dans des modules séparés pour garder les actions d'écriture
distinctes des extractions.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import pickle
from typing import Any

import pandas as pd


DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data_files"
DEFAULT_DATABASE_PATH = DEFAULT_DATA_DIR / "var_articles.data"


@dataclass(frozen=True)
class OdooConfig:
    url: str
    port: int
    database: str
    username: str
    password: str
    timeout: int = 300
    batch_size: int = 500


def default_database_path() -> Path:
    return DEFAULT_DATABASE_PATH


def database_status(path: Path = DEFAULT_DATABASE_PATH) -> dict[str, Any]:
    if not path.exists():
        return {
            "exists": False,
            "path": path,
            "modified_at": None,
            "size_bytes": 0,
        }
    stat = path.stat()
    return {
        "exists": True,
        "path": path,
        "modified_at": stat.st_mtime,
        "size_bytes": stat.st_size,
    }


def config_from_env() -> OdooConfig:
    required = ["ODOO_URL", "ODOO_DATABASE", "ODOO_USERNAME", "ODOO_PASSWORD"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise ValueError("Variable(s) Odoo manquante(s) : " + ", ".join(missing))
    return OdooConfig(
        url=os.environ["ODOO_URL"],
        port=int(os.getenv("ODOO_PORT", "443")),
        database=os.environ["ODOO_DATABASE"],
        username=os.environ["ODOO_USERNAME"],
        password=os.environ["ODOO_PASSWORD"],
        timeout=int(os.getenv("ODOO_TIMEOUT", "300")),
        batch_size=int(os.getenv("ODOO_BATCH_SIZE", "500")),
    )


def config_from_mapping(values: dict[str, Any]) -> OdooConfig:
    return OdooConfig(
        url=str(values["url"]),
        port=int(values.get("port", 443)),
        database=str(values["database"]),
        username=str(values["username"]),
        password=str(values["password"]),
        timeout=int(values.get("timeout", 300)),
        batch_size=int(values.get("batch_size", 500)),
    )


def refresh_articles_database(config: OdooConfig, output_path: Path = DEFAULT_DATABASE_PATH) -> pd.DataFrame:
    df = fetch_articles_from_odoo(config)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as handle:
        pickle.dump(df, handle)
    return df


def fetch_articles_from_odoo(config: OdooConfig) -> pd.DataFrame:
    import odoorpc

    odoo = odoorpc.ODOO(config.url, port=config.port, protocol="jsonrpc+ssl", timeout=config.timeout)
    odoo.login(config.database, config.username, config.password)

    Product = odoo.env["product.product"]
    product_fields = _model_field_names(Product)
    product_read_fields = [
        "id",
        "name",
        "standard_price",
        "barcode",
        "categ_id",
        "taxes_id",
        "product_tmpl_id",
        *[field for field in ["margin_classification_id", "sale_ok"] if field in product_fields],
    ]
    products = _search_read_all(
        Product,
        [("active", "=", True)],
        product_read_fields,
        batch_size=config.batch_size,
    )
    df_articles = pd.DataFrame(products)
    if df_articles.empty:
        return pd.DataFrame(columns=_article_output_columns(include_margin_markup=True))
    if "margin_classification_id" not in df_articles:
        df_articles["margin_classification_id"] = None
    if "sale_ok" not in df_articles:
        df_articles["sale_ok"] = pd.NA

    df_articles["template_id"] = df_articles["product_tmpl_id"].apply(_relation_id)
    df_articles["categ_id_only"] = df_articles["categ_id"].apply(_relation_id)
    df_articles["marge_nom"] = df_articles["margin_classification_id"].apply(_relation_name)
    df_articles["marge_markup"] = df_articles["margin_classification_id"].apply(_relation_id).map(
        margin_markup_by_id(Product, odoo, df_articles, config.batch_size)
    )

    SupplierInfo = odoo.env["product.supplierinfo"]
    supplier_partner_field = supplierinfo_partner_field(SupplierInfo)
    supplier_rows = _search_read_all(
        SupplierInfo,
        [],
        ["id", "product_tmpl_id", "product_id", supplier_partner_field, "product_code", "price", "product_uom"],
        batch_size=config.batch_size,
    )
    df_suppliers = pd.DataFrame(supplier_rows)
    if df_suppliers.empty:
        df_suppliers = pd.DataFrame(
            columns=[
                "template_id",
                "product_code",
                "price",
                "supplier_id",
                "supplier_name",
                "uom_id",
                "uom_name",
            ]
        )
    else:
        df_suppliers["supplier_id"] = df_suppliers[supplier_partner_field].apply(_relation_id)
        df_suppliers["supplier_name"] = df_suppliers[supplier_partner_field].apply(_relation_name)
        df_suppliers["uom_id"] = df_suppliers["product_uom"].apply(_relation_id)
        df_suppliers["uom_name"] = df_suppliers["product_uom"].apply(_relation_name)
        df_suppliers["template_id"] = df_suppliers["product_tmpl_id"].apply(_relation_id)

    df_uom = _fetch_uom_ratios(odoo, df_suppliers, config.batch_size)
    df_taxes = _fetch_taxes(odoo, df_articles, config.batch_size)
    df_categories = _fetch_categories(odoo, df_articles, config.batch_size)

    df_final = df_articles.merge(
        df_categories[["id", "parent_name"]],
        left_on="categ_id_only",
        right_on="id",
        how="left",
        suffixes=("", "_cat"),
    ).rename(columns={"parent_name": "categorie_mere"})

    df_final = df_final.merge(
        df_suppliers[
            [
                "template_id",
                "product_code",
                "price",
                "supplier_id",
                "supplier_name",
                "uom_id",
                "uom_name",
            ]
        ],
        on="template_id",
        how="left",
    )
    df_final = df_final.merge(
        df_uom[["id", "uom_ratio"]],
        left_on="uom_id",
        right_on="id",
        how="left",
        suffixes=("", "_uom"),
    )

    df_final["tax_id"] = df_final["taxes_id"].apply(lambda value: value[0] if value else None)
    df_final = df_final.merge(
        df_taxes[["id", "amount"]],
        left_on="tax_id",
        right_on="id",
        how="left",
        suffixes=("", "_tax"),
    ).rename(columns={"amount": "tax_amount"})

    column_mapping = {
        "id": "id",
        "name": "Nom",
        "supplier_id": "Fournisseurs/ID",
        "supplier_name": "Fournisseurs/Nom",
        "product_code": "Fournisseurs/Référence Fournisseur",
        "standard_price": "Coût",
        "price": "Fournisseurs/Prix",
        "uom_name": "Fournisseurs/Unité de mesure/Nom affiché",
        "uom_ratio": "Fournisseurs/Unité de mesure/Ratio",
        "tax_amount": "Taxes à la vente/Montant",
        "marge_nom": "Catégorie de marge/Nom",
        "marge_markup": "Catégorie de marge/Markup",
        "sale_ok": "vente_ok",
        "barcode": "Code Barre",
        "categorie_mere": "Catégorie d'article/Catégorie mère/Nom",
    }
    return df_final.rename(columns=column_mapping)[list(column_mapping.values())]


def _fetch_uom_ratios(odoo, df_suppliers: pd.DataFrame, batch_size: int) -> pd.DataFrame:
    ids = df_suppliers["uom_id"].dropna().unique().tolist() if "uom_id" in df_suppliers else []
    if not ids:
        return pd.DataFrame(columns=["id", "uom_ratio"])
    rows = _search_read_all(odoo.env["uom.uom"], [("id", "in", ids)], ["id", "name", "factor"], batch_size=batch_size)
    return pd.DataFrame(rows).rename(
        columns={"factor": "uom_ratio"}
    )


def _fetch_taxes(odoo, df_articles: pd.DataFrame, batch_size: int) -> pd.DataFrame:
    ids: list[int] = []
    for tax_list in df_articles.get("taxes_id", []):
        if tax_list:
            ids.extend(tax_list)
    if not ids:
        return pd.DataFrame(columns=["id", "amount"])
    rows = _search_read_all(
        odoo.env["account.tax"],
        [("id", "in", list(set(ids)))],
        ["id", "amount"],
        batch_size=batch_size,
    )
    return pd.DataFrame(rows)


def _fetch_categories(odoo, df_articles: pd.DataFrame, batch_size: int) -> pd.DataFrame:
    ids = df_articles["categ_id_only"].dropna().unique().tolist() if "categ_id_only" in df_articles else []
    if not ids:
        return pd.DataFrame(columns=["id", "parent_name"])
    rows = _search_read_all(
        odoo.env["product.category"],
        [("id", "in", ids)],
        ["id", "name", "parent_id"],
        batch_size=batch_size,
    )
    df = pd.DataFrame(rows)
    df["parent_name"] = df["parent_id"].apply(_relation_name)
    return df


def margin_markup_by_id(Product: Any, odoo: Any, df_articles: pd.DataFrame, batch_size: int) -> dict[int, float]:
    """Retourne les markups Odoo par id de product.margin.classification."""
    try:
        product_fields = Product.fields_get(["margin_classification_id"])
        relation_model = product_fields.get("margin_classification_id", {}).get("relation")
        if not relation_model:
            return {}

        margin_ids = sorted(
            {
                value
                for value in df_articles["margin_classification_id"].map(_relation_id).dropna().tolist()
                if value
            }
        )
        if not margin_ids:
            return {}

        Margin = odoo.env[relation_model]
        if "markup" not in _model_field_names(Margin):
            return {}

        rows = _search_read_all(
            Margin,
            [("id", "in", margin_ids)],
            ["id", "markup"],
            batch_size=min(batch_size, 100),
        )
        return {
            int(row["id"]): float(row["markup"])
            for row in rows
            if row.get("id") is not None and row.get("markup") is not None
        }
    except Exception:
        return {}


def supplierinfo_partner_field(model: Any) -> str:
    fields = _model_field_names(model)
    for field in ["partner_id", "name"]:
        if field in fields:
            return field
    raise ValueError("product.supplierinfo ne contient ni partner_id ni name pour le fournisseur")


def _model_field_names(model: Any) -> set[str]:
    try:
        fields_metadata = model.fields_get()
    except TypeError:
        fields_metadata = model.fields_get([])
    return set(fields_metadata)


def _search_read_all(
    model: Any,
    domain: list[Any],
    fields: list[str],
    *,
    batch_size: int,
) -> list[dict[str, Any]]:
    if batch_size <= 0:
        raise ValueError("Odoo batch_size doit etre superieur a zero")

    rows: list[dict[str, Any]] = []
    ids = model.search(domain)
    for offset in range(0, len(ids), batch_size):
        batch_ids = ids[offset : offset + batch_size]
        rows.extend(_read_with_retry(model, batch_ids, fields))
    return rows


def _read_with_retry(model: Any, ids: list[int], fields: list[str], attempts: int = 2) -> list[dict[str, Any]]:
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            return model.read(ids, fields)
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    return []


def _article_output_columns(include_margin_markup: bool = False) -> list[str]:
    columns = [
        "id",
        "Nom",
        "Fournisseurs/ID",
        "Fournisseurs/Nom",
        "Fournisseurs/Référence Fournisseur",
        "Coût",
        "Fournisseurs/Prix",
        "Fournisseurs/Unité de mesure/Nom affiché",
        "Fournisseurs/Unité de mesure/Ratio",
        "Taxes à la vente/Montant",
        "Catégorie de marge/Nom",
        "vente_ok",
        "Code Barre",
        "Catégorie d'article/Catégorie mère/Nom",
    ]
    if include_margin_markup:
        columns.insert(columns.index("vente_ok"), "Catégorie de marge/Markup")
    return columns


def _relation_id(value: object) -> int | None:
    return value[0] if isinstance(value, (list, tuple)) and value else None


def _relation_name(value: object) -> str | None:
    return value[1] if isinstance(value, (list, tuple)) and len(value) > 1 else None
