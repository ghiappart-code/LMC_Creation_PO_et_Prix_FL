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
    )


def config_from_mapping(values: dict[str, Any]) -> OdooConfig:
    return OdooConfig(
        url=str(values["url"]),
        port=int(values.get("port", 443)),
        database=str(values["database"]),
        username=str(values["username"]),
        password=str(values["password"]),
    )


def refresh_articles_database(config: OdooConfig, output_path: Path = DEFAULT_DATABASE_PATH) -> pd.DataFrame:
    df = fetch_articles_from_odoo(config)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as handle:
        pickle.dump(df, handle)
    return df


def fetch_articles_from_odoo(config: OdooConfig) -> pd.DataFrame:
    import odoorpc

    odoo = odoorpc.ODOO(config.url, port=config.port, protocol="jsonrpc+ssl")
    odoo.login(config.database, config.username, config.password)

    Product = odoo.env["product.product"]
    products = Product.search_read(
        [("active", "=", True)],
        [
            "id",
            "name",
            "standard_price",
            "barcode",
            "categ_id",
            "taxes_id",
            "product_tmpl_id",
            "margin_classification_id",
            "sale_ok",
        ],
    )
    df_articles = pd.DataFrame(products)

    IrModelData = odoo.env["ir.model.data"]
    external_ids = IrModelData.search_read([("model", "=", "product.product")], ["res_id", "complete_name"])
    df_external_ids = pd.DataFrame(external_ids)
    if df_external_ids.empty:
        df_articles["external_id"] = None
    else:
        df_articles = df_articles.merge(
            df_external_ids.rename(columns={"complete_name": "external_id"})[["res_id", "external_id"]],
            left_on="id",
            right_on="res_id",
            how="left",
        )

    df_articles["template_id"] = df_articles["product_tmpl_id"].apply(_relation_id)
    df_articles["categ_id_only"] = df_articles["categ_id"].apply(_relation_id)
    df_articles["marge_nom"] = df_articles["margin_classification_id"].apply(_relation_name)

    SupplierInfo = odoo.env["product.supplierinfo"]
    supplier_rows = SupplierInfo.search_read(
        [],
        ["id", "product_tmpl_id", "product_id", "name", "product_code", "price", "product_uom"],
    )
    df_suppliers = pd.DataFrame(supplier_rows)
    if df_suppliers.empty:
        df_suppliers = pd.DataFrame(
            columns=[
                "template_id",
                "product_code",
                "price",
                "supplier_id",
                "supplier_external_id",
                "uom_id",
                "uom_name",
            ]
        )
    else:
        df_suppliers["supplier_id"] = df_suppliers["name"].apply(_relation_id)
        df_suppliers["uom_id"] = df_suppliers["product_uom"].apply(_relation_id)
        df_suppliers["uom_name"] = df_suppliers["product_uom"].apply(_relation_name)
        df_suppliers["template_id"] = df_suppliers["product_tmpl_id"].apply(_relation_id)
        df_suppliers = _add_supplier_external_ids(IrModelData, df_suppliers)

    df_uom = _fetch_uom_ratios(odoo, df_suppliers)
    df_taxes = _fetch_taxes(odoo, df_articles)
    df_categories = _fetch_categories(odoo, df_articles)

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
                "supplier_external_id",
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
        "external_id": "ID Externe",
        "id": "id",
        "name": "Nom",
        "supplier_id": "Fournisseurs/ID",
        "supplier_external_id": "Fournisseurs/ID Externe",
        "product_code": "Fournisseurs/Référence Fournisseur",
        "standard_price": "Coût",
        "price": "Fournisseurs/Prix",
        "uom_name": "Fournisseurs/Unité de mesure/Nom affiché",
        "uom_ratio": "Fournisseurs/Unité de mesure/Ratio",
        "tax_amount": "Taxes à la vente/Montant",
        "marge_nom": "Catégorie de marge/Nom",
        "sale_ok": "vente_ok",
        "barcode": "Code Barre",
        "categorie_mere": "Catégorie d'article/Catégorie mère/Nom",
    }
    return df_final.rename(columns=column_mapping)[list(column_mapping.values())]


def _fetch_uom_ratios(odoo, df_suppliers: pd.DataFrame) -> pd.DataFrame:
    ids = df_suppliers["uom_id"].dropna().unique().tolist() if "uom_id" in df_suppliers else []
    if not ids:
        return pd.DataFrame(columns=["id", "uom_ratio"])
    return pd.DataFrame(odoo.env["uom.uom"].search_read([("id", "in", ids)], ["id", "name", "factor"])).rename(
        columns={"factor": "uom_ratio"}
    )


def _add_supplier_external_ids(IrModelData, df_suppliers: pd.DataFrame) -> pd.DataFrame:
    """Ajoute l'ID externe Odoo des fournisseurs quand il existe.

    Le CSV d'import de bon de commande utilise un identifiant externe de
    `res.partner` dans la colonne `Fournisseur/ID`. On le récupère via
    `ir.model.data`, comme pour les produits.
    """
    supplier_ids = df_suppliers["supplier_id"].dropna().unique().tolist()
    if not supplier_ids:
        output = df_suppliers.copy()
        output["supplier_external_id"] = None
        return output

    external_rows = IrModelData.search_read(
        [("model", "=", "res.partner"), ("res_id", "in", supplier_ids)],
        ["res_id", "complete_name"],
    )
    df_external = pd.DataFrame(external_rows)
    if df_external.empty:
        output = df_suppliers.copy()
        output["supplier_external_id"] = None
        return output

    return df_suppliers.merge(
        df_external.rename(columns={"complete_name": "supplier_external_id"})[
            ["res_id", "supplier_external_id"]
        ],
        left_on="supplier_id",
        right_on="res_id",
        how="left",
    )


def _fetch_taxes(odoo, df_articles: pd.DataFrame) -> pd.DataFrame:
    ids: list[int] = []
    for tax_list in df_articles.get("taxes_id", []):
        if tax_list:
            ids.extend(tax_list)
    if not ids:
        return pd.DataFrame(columns=["id", "amount"])
    return pd.DataFrame(odoo.env["account.tax"].search_read([("id", "in", list(set(ids)))], ["id", "amount"]))


def _fetch_categories(odoo, df_articles: pd.DataFrame) -> pd.DataFrame:
    ids = df_articles["categ_id_only"].dropna().unique().tolist() if "categ_id_only" in df_articles else []
    if not ids:
        return pd.DataFrame(columns=["id", "parent_name"])
    df = pd.DataFrame(odoo.env["product.category"].search_read([("id", "in", ids)], ["id", "name", "parent_id"]))
    df["parent_name"] = df["parent_id"].apply(_relation_name)
    return df


def _relation_id(value: object) -> int | None:
    return value[0] if isinstance(value, (list, tuple)) and value else None


def _relation_name(value: object) -> str | None:
    return value[1] if isinstance(value, (list, tuple)) and len(value) > 1 else None
