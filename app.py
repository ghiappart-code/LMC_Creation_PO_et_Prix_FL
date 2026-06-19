from __future__ import annotations

from pathlib import Path
from datetime import datetime

import pandas as pd
import streamlit as st

from lmc_po_price.database import load_article_database
from lmc_po_price.exports import result_workbook_bytes
from lmc_po_price.invoice_parsing import parse_invoice_pdf
from lmc_po_price.matching import match_invoice_to_articles
from lmc_po_price.models import WorkflowConfig, WorkflowResult
from lmc_po_price.odoo_articles import (
    config_from_env,
    config_from_mapping,
    database_status,
    default_database_path,
    refresh_articles_database,
)
from lmc_po_price.odoo_price_update import prepare_odoo_price_update_rows, update_odoo_prices
from lmc_po_price.odoo_purchase_order import create_purchase_order_from_review
from lmc_po_price.purchase_order import prepare_purchase_order_import_csv
from lmc_po_price.workflow import _purchase_order_review, unmatched_review


st.set_page_config(page_title="Bons de commande et prix Odoo", layout="wide")
st.title("Bons de commande et prix Odoo")

TASK_PURCHASE_ORDER = "Créer le bon de commande"
TASK_PRICE_REVIEW = "Comparer / mettre à jour les prix"
TASK_BOTH = "Créer le bon de commande et comparer les prix"


def _odoo_config_from_streamlit():
    if "odoo" in st.secrets:
        return config_from_mapping(st.secrets["odoo"])
    return config_from_env()


def _load_database(database_mode: str, uploaded_file):
    if database_mode == "Chargement base depuis Odoo":
        return load_article_database(default_database_path())
    if uploaded_file is not None:
        return load_article_database(uploaded_file)
    return load_article_database(default_database_path())


def _format_timestamp(value: float | None) -> str:
    if value is None:
        return "n/a"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} o"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} Ko"
    return f"{size_bytes / (1024 * 1024):.1f} Mo"


data_path = default_database_path()
status = database_status(data_path)
local_database_exists = status["exists"]

with st.sidebar:
    st.header("Parametres")
    st.subheader("Base articles")
    if status["exists"]:
        st.info(
            "Base Odoo locale disponible\n\n"
            f"- Fichier : `{status['path'].name}`\n"
            f"- Derniere mise a jour : {_format_timestamp(status['modified_at'])}\n"
            f"- Taille : {_format_size(int(status['size_bytes']))}"
        )
    else:
        st.warning("Aucune base Odoo locale disponible apres relance de l'application.")

    database_mode = st.radio(
        "Choisir la source de la base",
        ["Base article locale", "Chargement base depuis Odoo"],
    )
    database_file = None

    if database_mode == "Base article locale":
        if local_database_exists:
            st.caption(f"Base locale par defaut disponible : `{data_path.name}`")
        else:
            st.caption("Aucune base locale par defaut n'est disponible.")
        database_file = st.file_uploader("Choisir une base articles locale", type=["data", "csv", "xlsx", "xls"])
        database_ready = database_file is not None or local_database_exists
    else:
        if local_database_exists:
            st.caption(f"Derniere base extraite : `{data_path.name}`")
        else:
            st.caption("Aucune base extraite depuis Odoo pour le moment.")
        if st.button("Charger la base depuis Odoo"):
            try:
                with st.spinner("Extraction des articles depuis Odoo..."):
                    df = refresh_articles_database(_odoo_config_from_streamlit(), data_path)
                st.session_state["database_loaded_from_odoo"] = True
                st.success(f"Base mise a jour : {len(df)} lignes.")
                st.rerun()
            except Exception as exc:
                st.error(f"Impossible de charger la base depuis Odoo : {exc}")
        database_ready = data_path.exists()

    invoice_file = st.file_uploader("Facture fournisseur", type=["pdf"])
    selected_task = st.radio(
        "Action à préparer",
        [TASK_BOTH, TASK_PURCHASE_ORDER, TASK_PRICE_REVIEW],
    )
    launch = st.button("Lancer l'analyse", type="primary", disabled=not invoice_file or not database_ready)

if not launch:
    st.info("Chargez une facture et une base articles, puis lancez l'analyse.")
    st.stop()

try:
    invoice = parse_invoice_pdf(invoice_file)
    articles = _load_database(database_mode, database_file)
    config = WorkflowConfig(supplier_code=invoice.supplier_code)
    all_lines = match_invoice_to_articles(invoice.lines, articles, config)
    matched = all_lines[all_lines["statut"] == "trouve"].copy()
    unmatched = all_lines[all_lines["statut"] == "non_trouve"].copy()
    ambiguous = all_lines[all_lines["statut"] == "a_verifier"].copy()
    price_changes = matched[matched["prix_change"]].copy()
    price_update_rows = prepare_odoo_price_update_rows(price_changes)
    result = WorkflowResult(
        invoice=invoice,
        all_lines=all_lines,
        matched=matched,
        unmatched=unmatched,
        ambiguous=ambiguous,
        price_changes=price_changes,
        purchase_order_review=_purchase_order_review(
            all_lines,
            invoice.invoice_number,
            invoice.delivery_date,
        ),
        sale_flag_review=pd.DataFrame(),
    )
except Exception as exc:
    st.error(f"Analyse impossible : {exc}")
    st.stop()

cols = st.columns(5)
cols[0].metric("Lignes facture", len(invoice.lines))
cols[1].metric("Articles trouves", len(matched))
cols[2].metric("Non retrouves", len(unmatched))
cols[3].metric("A verifier", len(ambiguous))
cols[4].metric("Prix changes", len(price_changes))

include_purchase_order = selected_task in {TASK_BOTH, TASK_PURCHASE_ORDER}
include_price_review = selected_task in {TASK_BOTH, TASK_PRICE_REVIEW}

st.download_button(
    "Telecharger le classeur de controle",
    data=result_workbook_bytes(
        result,
        include_purchase_order=include_purchase_order,
        include_price_review=include_price_review,
    ),
    file_name=f"{invoice.invoice_number or 'facture'}_controle_odoo.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)

tab_names = ["Tous les articles facture"]
if include_purchase_order:
    tab_names.append("Bon de commande")
if include_price_review:
    tab_names.append("Changements de prix")
tab_names.extend(["Non retrouves", "A verifier"])

tabs = st.tabs(tab_names)
tab_index = 0
with tabs[tab_index]:
    st.dataframe(all_lines, use_container_width=True, hide_index=True)
tab_index += 1
if include_purchase_order:
    with tabs[tab_index]:
        st.dataframe(result.purchase_order_review, use_container_width=True, hide_index=True)
        st.download_button(
            "Télécharger le CSV d'import bon de commande",
            data=prepare_purchase_order_import_csv(result.purchase_order_review),
            file_name=f"{invoice.invoice_number or 'facture'}_bon_commande_odoo.csv",
            mime="text/csv",
            disabled=result.purchase_order_review.empty,
        )
        st.divider()
        st.subheader("Création Odoo")
        if result.purchase_order_review.empty:
            st.info("Aucune ligne éligible pour créer un bon de commande.")
        else:
            confirm_po = st.checkbox(
                "J'ai vérifié le bon de commande et je veux le créer dans Odoo",
                key=f"confirm_po_{invoice.invoice_number}",
            )
            if st.button(
                "Créer le bon de commande dans Odoo",
                disabled=not confirm_po,
                key=f"create_po_{invoice.invoice_number}",
            ):
                try:
                    with st.spinner("Création du bon de commande dans Odoo..."):
                        summary = create_purchase_order_from_review(
                            result.purchase_order_review,
                            _odoo_config_from_streamlit(),
                        )
                    if summary.status == "success":
                        st.success(summary.message)
                    else:
                        st.error(summary.message)
                    if not summary.results.empty:
                        st.dataframe(summary.results, use_container_width=True, hide_index=True)
                except Exception as exc:
                    st.error(f"Impossible de créer le bon de commande : {exc}")
    tab_index += 1
if include_price_review:
    with tabs[tab_index]:
        st.dataframe(price_changes, use_container_width=True, hide_index=True)
        st.divider()
        st.subheader("Mise à jour Odoo")
        if price_update_rows.empty:
            st.info("Aucune ligne de changement de prix n'est éligible à une mise à jour automatique.")
        else:
            st.warning(f"{len(price_update_rows)} ligne(s) éligible(s) à la mise à jour Odoo.")
            st.dataframe(price_update_rows, use_container_width=True, hide_index=True)
            confirm_prices = st.checkbox(
                "J'ai vérifié les changements de prix et je veux mettre à jour Odoo",
                key=f"confirm_prices_{invoice.invoice_number}",
            )
            if st.button(
                "Mettre à jour les prix dans Odoo",
                disabled=not confirm_prices,
                key=f"update_prices_{invoice.invoice_number}",
            ):
                try:
                    with st.spinner("Mise à jour des prix dans Odoo..."):
                        summary = update_odoo_prices(price_update_rows, _odoo_config_from_streamlit())
                    if summary.errors:
                        st.error(
                            f"Mise à jour terminée avec erreurs : {summary.success} succès, "
                            f"{summary.warnings} avertissement(s), {summary.errors} erreur(s)."
                        )
                    else:
                        st.success(
                            f"Mise à jour terminée : {summary.success} succès, "
                            f"{summary.warnings} avertissement(s), {summary.errors} erreur."
                        )
                    st.dataframe(summary.results, use_container_width=True, hide_index=True)
                except Exception as exc:
                    st.error(f"Impossible de mettre à jour les prix : {exc}")
    tab_index += 1
with tabs[tab_index]:
    st.dataframe(unmatched_review(unmatched), use_container_width=True, hide_index=True)
tab_index += 1
with tabs[tab_index]:
    st.dataframe(ambiguous, use_container_width=True, hide_index=True)

st.caption(
    "Les écritures Odoo ne sont jamais automatiques : elles ne sont lancées "
    "qu'après vérification des onglets concernés et validation explicite par case à cocher."
)
