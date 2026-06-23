from __future__ import annotations

from pathlib import Path
from datetime import datetime
from time import perf_counter

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


def _format_duration(seconds: float) -> str:
    if seconds < 10:
        return f"{seconds:.1f} secondes"
    return f"{seconds:.0f} secondes"


def _show_po_creation_diagnostics(results: pd.DataFrame | None) -> None:
    if results is None or results.empty:
        return
    if st.checkbox("Afficher le diagnostic technique de création Odoo", key="show_po_debug"):
        st.dataframe(results, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
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
        odoo_database = st.secrets.get("odoo", {}).get("database", "inconnue")
        st.caption(f"Base Odoo : `{odoo_database}`")
        if st.button("Charger la base depuis Odoo"):
            try:
                with st.spinner("Extraction des articles depuis Odoo..."):
                    df = refresh_articles_database(_odoo_config_from_streamlit(), data_path)
                st.session_state["database_loaded_from_odoo"] = f"Base mise a jour : {len(df)} lignes."
                st.rerun()
            except Exception as exc:
                st.error(f"Impossible de charger la base depuis Odoo : {exc}")
        if st.session_state.get("database_loaded_from_odoo"):
            st.success(st.session_state["database_loaded_from_odoo"])
        database_ready = data_path.exists()

    invoice_file = st.file_uploader("Facture fournisseur", type=["pdf"])
    selected_task = st.radio(
        "Action à préparer",
        [TASK_BOTH, TASK_PURCHASE_ORDER, TASK_PRICE_REVIEW],
    )
    launch = st.button("Lancer l'analyse", type="primary", disabled=not invoice_file or not database_ready)

# ---------------------------------------------------------------------------
# Lancement de l'analyse — résultat stocké dans session_state
# ---------------------------------------------------------------------------
if launch:
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
        st.session_state["result"] = result
        st.session_state["price_update_rows"] = price_update_rows
        st.session_state["selected_task"] = selected_task
        # Réinitialiser les confirmations Odoo à chaque nouvelle analyse
        st.session_state.pop("po_created", None)
        st.session_state.pop("po_creation_results", None)
        st.session_state.pop("prices_updated", None)
    except Exception as exc:
        st.error(f"Analyse impossible : {exc}")
        st.stop()

# ---------------------------------------------------------------------------
# Affichage — on travaille depuis session_state
# ---------------------------------------------------------------------------
if "result" not in st.session_state:
    st.info("Chargez une facture et une base articles, puis lancez l'analyse.")
    st.stop()

result: WorkflowResult = st.session_state["result"]
price_update_rows: pd.DataFrame = st.session_state["price_update_rows"]
invoice = result.invoice
current_task = st.session_state.get("selected_task", selected_task)

include_purchase_order = current_task in {TASK_BOTH, TASK_PURCHASE_ORDER}
include_price_review = current_task in {TASK_BOTH, TASK_PRICE_REVIEW}

# Métriques
cols = st.columns(5)
cols[0].metric("Lignes facture", len(invoice.lines))
cols[1].metric("Articles trouves", len(result.matched))
cols[2].metric("Non retrouves", len(result.unmatched))
cols[3].metric("A verifier", len(result.ambiguous))
cols[4].metric("Prix changes", len(result.price_changes))

# Téléchargement classeur de contrôle
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

# ---------------------------------------------------------------------------
# Résumé des métadonnées
# ---------------------------------------------------------------------------
_invoice_file_name = getattr(invoice_file, "name", None) or f"{invoice.invoice_number or 'facture'}.pdf"
invoice_stem = Path(_invoice_file_name).stem

metadata_rows = {
    "Numéro de facture": invoice.invoice_number,
    "Date de facture": str(invoice.invoice_date) if invoice.invoice_date else "n/a",
    "Date de livraison": str(invoice.delivery_date) if invoice.delivery_date else "n/a",
    "Fournisseur": invoice.supplier_name,
    "Code fournisseur": invoice.supplier_code,
}
metadata_rows.update({k: str(v) for k, v in (invoice.metadata or {}).items() if k != "fournisseur"})
metadata_df = pd.DataFrame(
    [{"Champ": k, "Valeur": v} for k, v in metadata_rows.items()]
)

# ---------------------------------------------------------------------------
# Onglets de consultation (lecture seule)
# ---------------------------------------------------------------------------
st.divider()
st.subheader(f"Résultats pour la facture {invoice_stem}")
st.caption("Résumé des métadonnées")
st.dataframe(metadata_df, use_container_width=True, hide_index=True)

tab_names = ["Tous les articles facture"]
if include_purchase_order:
    tab_names.append("Bon de commande")
if include_price_review:
    tab_names.append("Changements de prix")
tab_names.extend(["Non retrouves", "A verifier"])

tabs = st.tabs(tab_names)
tab_index = 0

with tabs[tab_index]:
    st.dataframe(result.all_lines, use_container_width=True, hide_index=True)
tab_index += 1

if include_purchase_order:
    with tabs[tab_index]:
        st.dataframe(result.purchase_order_review, use_container_width=True, hide_index=True)
    tab_index += 1

if include_price_review:
    with tabs[tab_index]:
        st.dataframe(result.price_changes, use_container_width=True, hide_index=True)
    tab_index += 1

with tabs[tab_index]:
    st.dataframe(unmatched_review(result.unmatched), use_container_width=True, hide_index=True)
tab_index += 1

with tabs[tab_index]:
    st.dataframe(result.ambiguous, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# Section Odoo — CRÉATION BON DE COMMANDE (hors onglets)
# ---------------------------------------------------------------------------
if include_purchase_order:
    st.divider()
    st.subheader("Création du Bon de Commande dans Odoo")
    if result.purchase_order_review.empty:
        st.info("Aucune ligne éligible pour créer un bon de commande.")
    else:
        st.download_button(
            "Télécharger le CSV d'import bon de commande",
            data=prepare_purchase_order_import_csv(result.purchase_order_review),
            file_name=f"{invoice.invoice_number or 'facture'}_bon_commande_odoo.csv",
            mime="text/csv",
        )
        # Vérification des totaux
        total_po = (
            result.purchase_order_review["Lignes de la commande/Quantité"].fillna(0)
            * result.purchase_order_review["Lignes de la commande/Prix unitaire"].fillna(0)
        ).sum()
        # Les montant_ht des lignes sont déjà sans transport (transport est dans charges)
        total_facture_sans_transport = result.invoice.lines["montant_ht"].fillna(0).sum()
        transport_ht = result.invoice.charges["montant_ht"].fillna(0).sum() if not result.invoice.charges.empty else 0.0
        ecart = total_po - total_facture_sans_transport
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total HT bon de commande", f"{total_po:.2f} €")
        col2.metric("Total HT facture sans transport", f"{total_facture_sans_transport:.2f} €")
        col3.metric("Total HT transport", f"{transport_ht:.2f} €")
        col4.metric("Écart", f"{ecart:.2f} €", delta_color="inverse" if abs(ecart) > 0.01 else "off")
        if abs(ecart) > 0.01:
            st.warning(f"⚠️ Écart de {ecart:.2f} € entre le bon de commande et la facture.")
        else:
            st.success("✅ Total bon de commande conforme à la facture.")

        # Vérification préalable des lignes avant création Odoo
        issues = []
        valid_rows = []
        for _, row in result.purchase_order_review.iterrows():
            article_id = row.get("Lignes de la commande/Article/ID", "")
            quantite = row.get("Lignes de la commande/Quantité")
            prix = row.get("Lignes de la commande/Prix unitaire")
            description = row.get("Lignes de la commande/Description", "")
            raisons = []
            if not article_id:
                raisons.append("ID article manquant")
            try:
                if prix is None or float(prix) <= 0:
                    raisons.append("prix unitaire nul ou manquant")
            except (TypeError, ValueError):
                raisons.append("prix unitaire invalide")
            if raisons:
                issues.append({
                    "Article/ID": article_id,
                    "Description": description,
                    "Raison": ", ".join(raisons),
                })
            else:
                valid_rows.append(row)

        if issues:
            st.warning(f"{len(issues)} ligne(s) exclue(s) du bon de commande — à corriger si nécessaire :")
            st.dataframe(pd.DataFrame(issues), use_container_width=True, hide_index=True)

        po_review_valid = pd.DataFrame(valid_rows, columns=result.purchase_order_review.columns) if valid_rows else pd.DataFrame(columns=result.purchase_order_review.columns)

        if po_review_valid.empty:
            st.error("Aucune ligne valide pour créer le bon de commande.")
        elif st.session_state.get("po_created"):
            st.success(st.session_state["po_created"])
            _show_po_creation_diagnostics(st.session_state.get("po_creation_results"))
        else:
            st.info(f"{len(valid_rows)} ligne(s) valide(s) seront incluses dans le bon de commande.")
            confirm_po = st.checkbox(
                "J'ai vérifié le bon de commande et je veux le créer dans Odoo",
                key="confirm_po",
            )
            if st.button("Créer le bon de commande dans Odoo", disabled=not confirm_po, key="create_po"):
                try:
                    with st.spinner("Création du bon de commande dans Odoo..."):
                        started_at = perf_counter()
                        summary = create_purchase_order_from_review(
                            po_review_valid,
                            _odoo_config_from_streamlit(),
                        )
                        elapsed_seconds = perf_counter() - started_at
                    if summary.status == "success":
                        success_message = f"{summary.message} en {_format_duration(elapsed_seconds)}"
                        st.session_state["po_created"] = success_message
                        st.session_state["po_creation_results"] = summary.results
                        st.success(success_message)
                    else:
                        st.error(summary.message)
                    _show_po_creation_diagnostics(summary.results)
                except Exception as exc:
                    st.error(f"Impossible de créer le bon de commande : {exc}")

# ---------------------------------------------------------------------------
# Section Odoo — RAPPROCHEMENT MISE À JOUR DES PRIX (hors onglets)
# ---------------------------------------------------------------------------
if include_price_review:
    st.divider()
    st.subheader("Rapprochement Mise à Jour des Prix dans Odoo")
    if price_update_rows.empty:
        st.info("Aucune ligne de changement de prix n'est éligible à une mise à jour automatique.")
    else:
        st.warning(f"{len(price_update_rows)} ligne(s) éligible(s) à la mise à jour Odoo.")
        if st.session_state.get("prices_updated"):
            st.success(st.session_state["prices_updated"])
        else:
            confirm_prices = st.checkbox(
                "J'ai vérifié les changements de prix et je veux mettre à jour Odoo",
                key="confirm_prices",
            )
            if st.button("Mettre à jour les prix dans Odoo", disabled=not confirm_prices, key="update_prices"):
                try:
                    with st.spinner("Mise à jour des prix dans Odoo..."):
                        summary = update_odoo_prices(price_update_rows, _odoo_config_from_streamlit())
                    if summary.errors:
                        msg = (
                            f"Mise à jour terminée avec erreurs : {summary.success} succès, "
                            f"{summary.warnings} avertissement(s), {summary.errors} erreur(s)."
                        )
                        st.error(msg)
                    else:
                        msg = (
                            f"Mise à jour terminée : {summary.success} succès, "
                            f"{summary.warnings} avertissement(s), 0 erreur."
                        )
                        st.session_state["prices_updated"] = msg
                        st.success(msg)
                    st.dataframe(summary.results, use_container_width=True, hide_index=True)
                except Exception as exc:
                    st.error(f"Impossible de mettre à jour les prix : {exc}")

st.caption(
    "Les écritures Odoo ne sont jamais automatiques : elles ne sont lancées "
    "qu'après vérification et validation explicite par case à cocher."
)
