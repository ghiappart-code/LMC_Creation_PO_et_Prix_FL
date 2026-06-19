"""Chargement et normalisation de la base articles Odoo.

La base peut venir d'un fichier local (`.data`, CSV, Excel) ou d'une extraction
Odoo. Ce module harmonise les noms de colonnes pour que les traitements communs
ne dépendent pas du format d'entrée.
"""

from __future__ import annotations

import pickle
from pathlib import Path
from typing import BinaryIO

import pandas as pd

from lmc_po_price.text import normalize_column_name, normalize_key


REQUIRED_COLUMNS = {
    "article_id",
    "nom",
    "fournisseur_id",
    "reference_fournisseur",
    "cout",
    "prix_fournisseur",
}

COLUMN_ALIASES = {
    "id": "article_id",
    "id_externe": "id_externe",
    "nom": "nom",
    "fournisseurs/id": "fournisseur_id",
    "fournisseurs_id": "fournisseur_id",
    "fournisseurs/id_externe": "fournisseur_id_externe",
    "fournisseurs_id_externe": "fournisseur_id_externe",
    "fournisseurs_reference_fournisseur": "reference_fournisseur",
    "fournisseurs/reference_fournisseur": "reference_fournisseur",
    "cout": "cout",
    "coût": "cout",
    "fournisseurs/prix": "prix_fournisseur",
    "fournisseurs_prix": "prix_fournisseur",
    "fournisseurs_unite_de_mesure_nom_affiche": "unite_fournisseur",
    "fournisseurs/unite_de_mesure/nom_affiche": "unite_fournisseur",
    "fournisseurs_unite_de_mesure_nom_affiche": "unite_fournisseur",
    "fournisseurs_unite_de_mesure_ratio": "ratio_unite_fournisseur",
    "fournisseurs/unite_de_mesure/ratio": "ratio_unite_fournisseur",
    "fournisseurs_unite_de_mesure_ratio": "ratio_unite_fournisseur",
    "taxes_a_la_vente/montant": "tva",
    "taxes_à_la_vente/montant": "tva",
    "taxes_a_la_vente_montant": "tva",
    "categorie_de_marge/nom": "categorie_marge",
    "catégorie_de_marge/nom": "categorie_marge",
    "categorie_de_marge_nom": "categorie_marge",
    "vente_ok": "vente_ok",
    "sale_ok": "vente_ok",
    "code_barre": "code_barre",
    "categorie_d'article/categorie_mere/nom": "categorie_mere",
    "catégorie_d'article/catégorie_mère/nom": "categorie_mere",
    "categorie_d'article_categorie_mere_nom": "categorie_mere",
}


def load_article_database(file_or_path: BinaryIO | str | Path) -> pd.DataFrame:
    name = str(getattr(file_or_path, "name", file_or_path)).lower()
    if name.endswith(".csv"):
        df = pd.read_csv(file_or_path)
    elif name.endswith((".xlsx", ".xls")):
        df = pd.read_excel(file_or_path)
    elif name.endswith(".data"):
        with _open_binary_if_path(file_or_path) as handle:
            df = pickle.load(handle)
        if not isinstance(df, pd.DataFrame):
            raise ValueError("Le fichier .data ne contient pas un DataFrame pandas.")
    else:
        raise ValueError("Format de base non pris en charge. Utiliser CSV, Excel ou .data.")
    return normalize_article_database(df)


def normalize_article_database(df: pd.DataFrame) -> pd.DataFrame:
    normalized = df.copy()
    normalized.columns = [_map_column_name(col) for col in normalized.columns]
    normalized = _coalesce_duplicate_columns(normalized)

    missing = REQUIRED_COLUMNS - set(normalized.columns)
    if missing:
        raise ValueError("Colonne(s) manquante(s) dans la base Odoo : " + ", ".join(sorted(missing)))

    for column in [
        "id_externe",
        "nom",
        "fournisseur_id_externe",
        "reference_fournisseur",
        "unite_fournisseur",
        "categorie_marge",
        "code_barre",
        "categorie_mere",
    ]:
        if column in normalized:
            normalized[column] = normalized[column].fillna("").astype(str).str.strip()
    for column in ["article_id", "fournisseur_id"]:
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    for column in ["cout", "prix_fournisseur", "ratio_unite_fournisseur", "tva"]:
        if column in normalized:
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    if "ratio_unite_fournisseur" not in normalized:
        normalized["ratio_unite_fournisseur"] = 1.0
    normalized["ratio_unite_fournisseur"] = normalized["ratio_unite_fournisseur"].fillna(1.0).replace(0, 1.0)
    if "vente_ok" not in normalized:
        normalized["vente_ok"] = pd.NA
    if "fournisseur_id_externe" not in normalized:
        normalized["fournisseur_id_externe"] = ""
    normalized["reference_fournisseur"] = normalized["reference_fournisseur"].map(_clean_identifier)
    normalized["reference_fournisseur_key"] = normalized["reference_fournisseur"].map(normalize_key)
    normalized["designation_key"] = normalized["nom"].map(normalize_key)
    return normalized


def _map_column_name(column: object) -> str:
    raw = str(column).strip()
    slash_name = raw.lower()
    simple_name = raw.lower().replace(" ", "_").replace("-", "_")
    normalized_name = normalize_column_name(raw.replace("/", "_"))
    return COLUMN_ALIASES.get(
        slash_name,
        COLUMN_ALIASES.get(simple_name, COLUMN_ALIASES.get(normalized_name, normalized_name)),
    )


def _clean_identifier(value: object) -> str:
    text = "" if value is None else str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    if text.casefold() in {"nan", "none", "false"}:
        return ""
    return text


def _coalesce_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    output = pd.DataFrame(index=df.index)
    for column in dict.fromkeys(df.columns):
        values = df.loc[:, df.columns == column]
        output[column] = values.iloc[:, 0] if values.shape[1] == 1 else values.bfill(axis=1).iloc[:, 0]
    return output


def _open_binary_if_path(file_or_path: BinaryIO | str | Path):
    if isinstance(file_or_path, (str, Path)):
        return Path(file_or_path).open("rb")
    return _NullContext(file_or_path)


class _NullContext:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *_):
        return False
