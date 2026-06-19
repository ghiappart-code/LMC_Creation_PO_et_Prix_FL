"""Rapprochement entre lignes facture et articles Odoo.

Le matching exact se fait d'abord sur la référence fournisseur. Si aucun article
n'est trouvé, le module produit uniquement des suggestions prudentes par
désignation pour faciliter la revue manuelle.
"""

from __future__ import annotations

import pandas as pd

from lmc_po_price.models import WorkflowConfig
from lmc_po_price.pricing import sale_price
from lmc_po_price.text import normalize_key


PRIORITY_COLUMNS = [
    "Fact_reference",
    "Fact_designation",
    "Fact_PU_Net",
    "Fact_PU_Net_GZ",
    "Fact_PU_unitaire",
    "Match_Methode",
    "DB_Prix_Net",
    "Ecart_Prix",
    "New_Prix_de_vente",
]

STOPWORDS = {
    "bio",
    "cal",
    "vrac",
    "unite",
    "unit",
    "kg",
    "piece",
    "pieces",
    "colisage",
    "france",
    "conversion",
    "origine",
}


def match_invoice_to_articles(invoice_lines: pd.DataFrame, articles: pd.DataFrame, config: WorkflowConfig) -> pd.DataFrame:
    supplier_articles = articles[
        articles["fournisseur_id"].astype("Int64").astype(str) == str(config.supplier_code)
    ].copy()
    rows = [_match_line(line, supplier_articles, config) for _, line in invoice_lines.iterrows()]
    return _order_columns(pd.DataFrame(rows))


def _match_line(line: pd.Series, articles: pd.DataFrame, config: WorkflowConfig) -> dict[str, object]:
    reference_key = normalize_key(line.get("reference_fournisseur"))
    candidates = articles[articles["reference_fournisseur_key"] == reference_key] if reference_key else articles.iloc[0:0]

    if len(candidates) == 1:
        return _matched_row(line, candidates.iloc[0], "reference_exacte", config)
    if len(candidates) > 1:
        return _review_row(line, candidates, "reference_ambigue")

    suggestions = _description_suggestions(line, articles)
    return _unmatched_row(line, suggestions)


def _matched_row(line: pd.Series, product: pd.Series, method: str, config: WorkflowConfig) -> dict[str, object]:
    adjusted_invoice_price = float(line.get("prix_unitaire_ajuste") or line.get("prix_unitaire") or 0)
    unit_ratio = _numeric_or_default(product.get("ratio_unite_fournisseur"), 1.0)
    invoice_price = adjusted_invoice_price * unit_ratio
    current_price = float(product.get("prix_fournisseur") or product.get("cout") or 0)
    difference = invoice_price - current_price
    pct = difference / current_price if current_price else None
    changed = difference < config.price_decrease_threshold or difference > config.price_increase_threshold
    abnormal = bool(pct is not None and abs(pct) > config.abnormal_ratio and changed)
    base = _invoice_columns(line)
    base.update(_product_columns(product))
    base.update(
        {
            "Fact_PU_unitaire": invoice_price,
            "statut": "trouve",
            "Match_Methode": method,
            "DB_Prix_Net": current_price,
            "Ecart_Prix": difference,
            "Ecart_Prix_percent": pct,
            "New_Prix_de_vente": sale_price(
                invoice_price,
                product.get("tva"),
                product.get("categorie_marge"),
            ),
            "prix_change": bool(changed),
            "ecart_anormal": abnormal,
            "raison_revue": "ecart de prix anormal" if abnormal else "",
            "peut_etre_mis_a_jour": bool(changed and not abnormal),
            "Odoo_sale_ok": bool(changed and not abnormal),
        }
    )
    return base


def _review_row(line: pd.Series, candidates: pd.DataFrame, reason: str) -> dict[str, object]:
    base = _invoice_columns(line)
    base.update(
        {
            "statut": "a_verifier",
            "Match_Methode": reason,
            "Matchs_possibles": _candidate_summary(candidates),
            "DB_Prix_Net": None,
            "Ecart_Prix": None,
            "Ecart_Prix_percent": None,
            "New_Prix_de_vente": None,
            "prix_change": False,
            "ecart_anormal": False,
            "raison_revue": "plusieurs articles Odoo utilisent cette reference fournisseur",
            "peut_etre_mis_a_jour": False,
            "Odoo_sale_ok": False,
        }
    )
    return base


def _unmatched_row(line: pd.Series, suggestions: pd.DataFrame) -> dict[str, object]:
    base = _invoice_columns(line)
    base.update(
        {
            "statut": "non_trouve",
            "Match_Methode": "aucune_reference_trouvee",
            "Matchs_possibles": _candidate_summary(suggestions),
            "DB_Prix_Net": None,
            "Ecart_Prix": None,
            "Ecart_Prix_percent": None,
            "New_Prix_de_vente": None,
            "prix_change": False,
            "ecart_anormal": False,
            "raison_revue": "article non retrouve dans Odoo",
            "peut_etre_mis_a_jour": False,
            "Odoo_sale_ok": False,
        }
    )
    return base


def _description_suggestions(line: pd.Series, articles: pd.DataFrame) -> pd.DataFrame:
    tokens = _important_tokens(line.get("designation"))
    if not tokens:
        return articles.iloc[0:0]

    first_token = tokens[0]
    scored_rows: list[tuple[float, int]] = []
    for index, article in articles.iterrows():
        article_tokens = _important_tokens(article.get("nom"))
        if not article_tokens or first_token not in article_tokens:
            continue
        score = _suggestion_score(tokens, article_tokens)
        minimum_score = 0.70 if len(tokens) > 1 else 0.80
        if score >= minimum_score:
            scored_rows.append((score, index))

    if not scored_rows:
        return articles.iloc[0:0]
    scored_rows.sort(key=lambda item: item[0], reverse=True)
    selected = articles.loc[[index for _score, index in scored_rows[:5]]].copy()
    selected["match_score"] = [round(score, 2) for score, _index in scored_rows[:5]]
    return selected


def _invoice_columns(line: pd.Series) -> dict[str, object]:
    return {
        "Fact_reference": line.get("reference_fournisseur"),
        "Fact_designation": line.get("designation"),
        "Fact_quantite": line.get("quantite"),
        "Fact_unite": line.get("unite"),
        "Fact_PU_Net": line.get("prix_unitaire"),
        "Fact_PU_Net_GZ": line.get("prix_unitaire_ajuste", line.get("prix_unitaire")),
        "Fact_PU_unitaire": line.get("prix_unitaire_ajuste", line.get("prix_unitaire")),
        "Fact_taux_transport": line.get("taux_transport", 0.0),
        "Fact_montant_HT": line.get("montant_ht"),
    }


def _product_columns(product: pd.Series) -> dict[str, object]:
    return {
        "db_id_externe": product.get("id_externe"),
        "db_article_id": product.get("article_id"),
        "db_designation": product.get("nom"),
        "db_reference_fournisseur": product.get("reference_fournisseur"),
        "db_fournisseur_id": product.get("fournisseur_id"),
        "db_fournisseur_id_externe": product.get("fournisseur_id_externe"),
        "db_cout": product.get("cout"),
        "db_prix_fournisseur": product.get("prix_fournisseur"),
        "db_vente_ok": product.get("vente_ok"),
        "db_unite": product.get("unite_fournisseur"),
        "db_ratio_unite": product.get("ratio_unite_fournisseur"),
        "db_tva": product.get("tva"),
        "db_categorie_marge": product.get("categorie_marge"),
        "db_categorie_mere": product.get("categorie_mere"),
    }


def _candidate_summary(candidates: pd.DataFrame) -> str:
    if candidates.empty:
        return ""
    values = []
    for _, row in candidates.head(5).iterrows():
        score = row.get("match_score")
        score_text = f" - score {score:.2f}" if isinstance(score, float) else ""
        values.append(f"{row.get('article_id')} - {row.get('nom')} - ref {row.get('reference_fournisseur')}{score_text}")
    return "\n".join(values)


def _important_tokens(value: object) -> list[str]:
    text = str(value or "").casefold()
    raw_tokens = [
        normalize_key(token)
        for token in text.replace("-", " ").replace("/", " ").split()
    ]
    tokens: list[str] = []
    for token in raw_tokens:
        if len(token) < 3 or token.isdigit() or token in STOPWORDS:
            continue
        if token not in tokens:
            tokens.append(token)
    return tokens


def _suggestion_score(invoice_tokens: list[str], article_tokens: list[str]) -> float:
    invoice_set = set(invoice_tokens)
    article_set = set(article_tokens)
    common = invoice_set & article_set
    if not common:
        return 0.0
    first_bonus = 0.35 if invoice_tokens[0] in article_set else 0.0
    coverage = len(common) / len(invoice_set)
    precision = len(common) / max(len(article_set), 1)
    return first_bonus + 0.45 * coverage + 0.20 * precision


def _numeric_or_default(value: object, default: float) -> float:
    try:
        if pd.isna(value):
            return default
        numeric = float(value)
        return numeric if numeric else default
    except (TypeError, ValueError):
        return default


def _order_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    priority = [column for column in PRIORITY_COLUMNS if column in df.columns]
    rest = [column for column in df.columns if column not in priority]
    return df[priority + rest]
