from __future__ import annotations

from pathlib import Path
from datetime import datetime
import os
from time import perf_counter

import pandas as pd
import streamlit as st

from lmc_po_price.database import load_article_database
from lmc_po_price.exports import result_workbook_bytes
from lmc_po_price.suppliers import list_suppliers, parse_invoice_pdf, supplier_label
from lmc_po_price.matching import match_invoice_to_articles
from lmc_po_price.models import WorkflowConfig, WorkflowResult
from lmc_po_price.odoo_articles import (
    config_from_env,
    config_from_mapping,
    database_status,
    default_database_path,
    refresh_articles_database,
)
from lmc_po_price.odoo_price_update import (
    dry_run_odoo_price_updates,
    prepare_odoo_price_update_rows,
    update_odoo_prices,
)
from lmc_po_price.odoo_labels import (
    DEFAULT_LABEL_PRINT_CATEGORY_ID,
    DEFAULT_LABEL_REPORT_NAME,
    dry_run_labels_from_invoice_lines,
    generate_labels_pdf_from_invoice_lines,
)
from lmc_po_price.odoo_purchase_order import create_purchase_order_from_review, dry_run_purchase_order_from_review
from lmc_po_price.purchase_order import prepare_purchase_order_import_csv
from lmc_po_price.supplier_rules import filter_articles_for_supplier
from lmc_po_price.workflow import _purchase_order_review, ambiguous_review, blocked_price_changes, unmatched_review


st.set_page_config(page_title="Bons de commande et prix Odoo", layout="wide")
st.title("Bons de commande et prix Odoo")

TASK_PURCHASE_ORDER = "Créer le Bon de Commande"
TASK_PRICE_REVIEW = "Mettre à Jour le prix"
TASK_BOTH = "Créer le Bon de Commande et Mettre à Jour les prix"
DATABASE_DEFAULT = "Utiliser la base actuellement disponible"
DATABASE_UPLOAD = "Choisir une autre base locale"
DATABASE_ODOO = "Télécharger la base depuis Odoo"


def _odoo_config_from_streamlit():
    if "odoo" in st.secrets:
        return config_from_mapping(st.secrets["odoo"])
    return config_from_env()


def _configured_odoo_database_name() -> str:
    if "odoo" in st.secrets and st.secrets["odoo"].get("database"):
        return str(st.secrets["odoo"]["database"])
    return os.getenv("ODOO_DATABASE", "inconnue")


def _label_print_category_id() -> int:
    labels_config = st.secrets.get("labels", {})
    return int(labels_config.get("print_category_id", DEFAULT_LABEL_PRINT_CATEGORY_ID))


def _label_report_name() -> str:
    labels_config = st.secrets.get("labels", {})
    return str(labels_config.get("report_name", DEFAULT_LABEL_REPORT_NAME))


def _developer_mode_enabled() -> bool:
    if os.getenv("LMC_DEVELOPER_MODE", "").strip().casefold() in {"1", "true", "yes", "on"}:
        return True
    app_config = st.secrets.get("app", {})
    return bool(app_config.get("developer_mode", False))


def _load_database(database_mode: str, uploaded_file):
    if database_mode in {DATABASE_DEFAULT, DATABASE_ODOO}:
        return load_article_database(default_database_path())
    if database_mode == DATABASE_UPLOAD and uploaded_file is not None:
        return load_article_database(uploaded_file)
    raise ValueError("Aucune base articles locale n'a ete choisie.")


def _database_source_description(database_mode: str, uploaded_file, path: Path, article_count: int) -> str:
    if database_mode == DATABASE_ODOO:
        return f"La base articles Odoo {_configured_odoo_database_name()} est chargée avec {article_count} articles."
    if database_mode == DATABASE_UPLOAD and uploaded_file is not None:
        return f"La base articles locale {getattr(uploaded_file, 'name', 'fichier utilisateur')} est chargée avec {article_count} articles."
    return f"La base articles locale {path.name} est chargée avec {article_count} articles."


def _pending_database_description(database_mode: str, uploaded_file, path: Path, ready: bool) -> str:
    if not ready:
        return "Base articles non chargee."
    if database_mode == DATABASE_UPLOAD and uploaded_file is not None:
        return f"Base articles chargee : {getattr(uploaded_file, 'name', 'fichier utilisateur')}"
    if database_mode == DATABASE_ODOO and st.session_state.get("database_loaded_from_odoo"):
        return st.session_state["database_loaded_from_odoo"]
    status = database_status(path)
    if status["exists"]:
        return (
            f"Base articles disponible : {path.name} "
            f"({_format_size(int(status['size_bytes']))}, mise a jour {_format_timestamp(status['modified_at'])})"
        )
    return "Base articles non chargee."


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


def _show_po_creation_diagnostics(results: pd.DataFrame | None, developer_mode: bool) -> None:
    if not developer_mode:
        return
    if results is None or results.empty:
        return
    if st.checkbox("Afficher le diagnostic technique de création Odoo", key="show_po_debug"):
        st.dataframe(results, use_container_width=True, hide_index=True)


def _show_label_diagnostics(
    excluded_lines: pd.DataFrame | None,
    developer_mode: bool,
    print_category_id: int,
    report_name: str,
) -> None:
    if not developer_mode:
        return
    if st.checkbox("Afficher le diagnostic technique des étiquettes", key="show_label_debug"):
        st.caption(f"print_category_id={print_category_id}, rapport={report_name}")
        if excluded_lines is not None and not excluded_lines.empty:
            st.dataframe(excluded_lines, use_container_width=True, hide_index=True)


def _show_preanalysis_status(database_message: str, invoice_file, database_ready: bool) -> None:
    st.subheader("État de chargement")
    if database_ready:
        st.success(database_message)
    else:
        st.warning(database_message)

    if invoice_file is not None:
        st.success(f"Facture chargee : {getattr(invoice_file, 'name', 'fichier facture')}")
    else:
        st.warning("Facture fournisseur non chargee.")

    if database_ready and invoice_file is not None:
        st.info("Les fichiers sont prêts. Lancez l'analyse pour préparer les contrôles Odoo.")
    else:
        st.info("Chargez une facture et une base articles, puis lancez l'analyse.")


def _dataframe_for_display(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    display = df.copy()
    price_columns = [
        "Fact_PU_Net",
        "Fact_PU_Net_GZ",
        "Fact_PU_unitaire",
        "DB_Prix_Net",
        "Ecart_Prix",
        "New_Prix_de_vente",
        "Fact_montant_HT",
        "db_cout",
        "db_prix_fournisseur",
    ]
    for column in price_columns:
        if column in display.columns:
            display[column] = display[column].map(_format_optional_decimal)
    if "Ecart_Prix_percent" in display.columns:
        display["Ecart_Prix_percent"] = display["Ecart_Prix_percent"].map(_format_optional_percent)
    return display


def _format_optional_decimal(value: object) -> object:
    if pd.isna(value):
        return None
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return value


def _format_optional_percent(value: object) -> object:
    if pd.isna(value):
        return None
    try:
        return f"{float(value):.1%}"
    except (TypeError, ValueError):
        return value


def _highlight_abnormal_price_rows(row: pd.Series) -> list[str]:
    style = "background-color: #f8d7da; color: #842029"
    is_abnormal = bool(row.get("ecart_anormal", False))
    return [style if is_abnormal else "" for _ in row]


def _highlight_price_change_rows(row: pd.Series) -> list[str]:
    if bool(row.get("prix_change", False)) and not bool(row.get("peut_etre_mis_a_jour", True)):
        return ["background-color: #fff3cd; color: #664d03; font-weight: 600" for _ in row]
    return _highlight_abnormal_price_rows(row)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
data_path = default_database_path()
status = database_status(data_path)
local_database_exists = status["exists"]
developer_mode = _developer_mode_enabled()

with st.sidebar:
    st.header("Parametres")
    if developer_mode:
        st.caption("Mode developpeur actif")
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

    database_options = [DATABASE_ODOO]
    if local_database_exists:
        database_options.append(DATABASE_DEFAULT)
    database_options.append(DATABASE_UPLOAD)
    database_mode = st.radio("Choisir la source de la base", database_options)
    database_file = None

    if database_mode == DATABASE_DEFAULT:
        if local_database_exists:
            st.caption(f"Base locale par defaut disponible : `{data_path.name}`")
        else:
            st.caption("Aucune base locale par defaut n'est disponible.")
        database_ready = local_database_exists
    elif database_mode == DATABASE_UPLOAD:
        database_file = st.file_uploader("Télécharger une base articles locale", type=["data", "csv", "xlsx", "xls"])
        database_ready = database_file is not None
    else:
        if local_database_exists:
            st.caption(f"Derniere base extraite : `{data_path.name}`")
        else:
            st.caption("Aucune base extraite depuis Odoo pour le moment.")
        odoo_database = st.secrets.get("odoo", {}).get("database", "inconnue")
        st.caption(f"Base Odoo : `{odoo_database}`")
        if st.button("Télécharger la base depuis Odoo"):
            try:
                with st.spinner("Extraction des articles depuis Odoo..."):
                    df = refresh_articles_database(_odoo_config_from_streamlit(), data_path)
                st.session_state["database_loaded_from_odoo"] = (
                    f"La base articles Odoo {_configured_odoo_database_name()} est chargée avec {len(df)} articles."
                )
                st.rerun()
            except Exception as exc:
                st.error(f"Impossible de charger la base depuis Odoo : {exc}")
        if st.session_state.get("database_loaded_from_odoo"):
            st.success(st.session_state["database_loaded_from_odoo"])
        database_ready = data_path.exists()

    st.subheader("Facture fournisseur")
    selected_supplier_code = st.selectbox(
        "Fournisseur de la facture",
        list_suppliers(),
        format_func=supplier_label,
    )
    invoice_file = st.file_uploader("Choisir une facture fournisseur", type=["pdf"])
    selected_task = st.radio(
        "Action à préparer",
        [TASK_BOTH, TASK_PURCHASE_ORDER, TASK_PRICE_REVIEW],
    )
    include_labels_option = st.checkbox(
        "Préparer l'impression des étiquettes Fruits et Légumes",
        value=False,
    )
    abnormal_ratio_percent = st.number_input(
        "Signalement d'un écart de prix supérieur à (%)",
        min_value=0,
        max_value=1000,
        value=30,
        step=5,
    )
    launch = st.button("Lancer l'analyse", type="primary", disabled=not invoice_file or not database_ready)

# ---------------------------------------------------------------------------
# Lancement de l'analyse — résultat stocké dans session_state
# ---------------------------------------------------------------------------
if launch:
    try:
        invoice = parse_invoice_pdf(invoice_file, expected_supplier_code=selected_supplier_code)
        articles = _load_database(database_mode, database_file)
        database_article_count = len(articles)
        config = WorkflowConfig(
            supplier_code=invoice.supplier_code,
            abnormal_ratio=float(abnormal_ratio_percent) / 100,
        )
        articles = filter_articles_for_supplier(articles, config.supplier_code)
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
        st.session_state["include_labels"] = include_labels_option
        st.session_state["database_source"] = _database_source_description(
            database_mode,
            database_file,
            data_path,
            database_article_count,
        )
        st.session_state["database_row_count"] = database_article_count
        st.session_state["database_filtered_row_count"] = len(articles)
        # Réinitialiser les confirmations Odoo à chaque nouvelle analyse
        st.session_state.pop("po_created", None)
        st.session_state.pop("po_creation_results", None)
        st.session_state.pop("po_dry_run_summary", None)
        st.session_state.pop("label_dry_run_summary", None)
        st.session_state.pop("labels_pdf", None)
        st.session_state.pop("labels_pdf_message", None)
        st.session_state.pop("price_dry_run_summary", None)
        st.session_state.pop("prices_updated", None)
    except Exception as exc:
        st.error(f"Analyse impossible : {exc}")
        st.stop()

# ---------------------------------------------------------------------------
# Affichage — on travaille depuis session_state
# ---------------------------------------------------------------------------
if "result" not in st.session_state:
    database_message = _pending_database_description(database_mode, database_file, data_path, database_ready)
    _show_preanalysis_status(database_message, invoice_file, database_ready)
    st.stop()

result: WorkflowResult = st.session_state["result"]
price_update_rows: pd.DataFrame = st.session_state["price_update_rows"]
invoice = result.invoice
current_task = st.session_state.get("selected_task", selected_task)
current_include_labels = bool(st.session_state.get("include_labels", include_labels_option))

include_purchase_order = current_task in {TASK_BOTH, TASK_PURCHASE_ORDER}
include_price_review = current_task in {TASK_BOTH, TASK_PRICE_REVIEW}
include_labels = current_include_labels
database_source = st.session_state.get("database_source", "Base articles non identifiee")
database_row_count = st.session_state.get("database_row_count")
database_filtered_row_count = st.session_state.get("database_filtered_row_count")
price_dry_run_summary = st.session_state.get("price_dry_run_summary")
blocked_changes = blocked_price_changes(result.price_changes)

# Métriques
cols = st.columns(6)
cols[0].metric("Lignes facture", len(invoice.lines))
cols[1].metric("Articles trouves", len(result.matched))
cols[2].metric("Non retrouves", len(result.unmatched))
cols[3].metric("Matching DB ambigus", len(result.ambiguous))
cols[4].metric("Prix changes", len(result.price_changes))
cols[5].metric("Changements bloqués", len(blocked_changes))
st.caption(database_source)
if database_filtered_row_count is not None and database_row_count != database_filtered_row_count:
    st.caption(f"{database_filtered_row_count} article(s) conservé(s) après filtrage fournisseur.")

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
    tab_names.append("Changements bloqués")
tab_names.extend(["Non retrouves", "Matching DB ambigus"])

tabs = st.tabs(tab_names)
tab_index = 0

with tabs[tab_index]:
    st.dataframe(_dataframe_for_display(result.all_lines), use_container_width=True, hide_index=True)
tab_index += 1

if include_purchase_order:
    with tabs[tab_index]:
        st.dataframe(result.purchase_order_review, use_container_width=True, hide_index=True)
    tab_index += 1

if include_price_review:
    with tabs[tab_index]:
        price_changes_display = _dataframe_for_display(result.price_changes)
        if price_changes_display.empty:
            st.dataframe(price_changes_display, use_container_width=True, hide_index=True)
        else:
            if not blocked_changes.empty:
                st.warning(
                    f"{len(blocked_changes)} changement(s) de prix sont bloqués et surlignés ci-dessous."
                )
            st.dataframe(
                price_changes_display.style.apply(_highlight_price_change_rows, axis=1),
                use_container_width=True,
                hide_index=True,
            )
    tab_index += 1

    with tabs[tab_index]:
        blocked_display = _dataframe_for_display(blocked_changes)
        if blocked_display.empty:
            st.success("Aucun changement de prix bloqué.")
        else:
            st.warning("Ces changements de prix demandent une correction ou une décision manuelle avant écriture Odoo.")
            st.dataframe(blocked_display, use_container_width=True, hide_index=True)
    tab_index += 1

with tabs[tab_index]:
    st.dataframe(unmatched_review(result.unmatched), use_container_width=True, hide_index=True)
tab_index += 1

with tabs[tab_index]:
    st.dataframe(_dataframe_for_display(ambiguous_review(result.ambiguous)), use_container_width=True, hide_index=True)

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
        else:
            st.info(f"{len(valid_rows)} ligne(s) valide(s) seront incluses dans le bon de commande.")
            po_dry_run_summary = st.session_state.get("po_dry_run_summary")
            po_already_created = bool(st.session_state.get("po_created"))
            if st.button(
                "Vérifier le bon de commande sans écriture dans Odoo",
                disabled=po_already_created,
                key="dry_run_po",
            ):
                try:
                    with st.spinner("Vérification du bon de commande sans écriture dans Odoo..."):
                        po_dry_run_summary = dry_run_purchase_order_from_review(
                            po_review_valid,
                            _odoo_config_from_streamlit(),
                        )
                    st.session_state["po_dry_run_summary"] = po_dry_run_summary
                except Exception as exc:
                    st.error(f"Impossible de vérifier le bon de commande dans Odoo : {exc}")

            if po_dry_run_summary is not None:
                if po_dry_run_summary.errors:
                    st.error(po_dry_run_summary.message)
                else:
                    st.success(po_dry_run_summary.message)
                st.dataframe(po_dry_run_summary.results, use_container_width=True, hide_index=True)

            po_dry_run_ok = po_dry_run_summary is not None and po_dry_run_summary.errors == 0
            if po_already_created:
                st.success(st.session_state["po_created"])
                _show_po_creation_diagnostics(st.session_state.get("po_creation_results"), developer_mode)
            elif not po_dry_run_ok:
                st.info("Vous pouvez lancer une vérification du bon de commande sans écriture dans Odoo.")
            if not po_already_created:
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
                        _show_po_creation_diagnostics(summary.results, developer_mode)
                    except Exception as exc:
                        st.error(f"Impossible de créer le bon de commande : {exc}")

# ---------------------------------------------------------------------------
# Section Odoo — IMPRESSION DES ETIQUETTES F&L (hors onglets)
# ---------------------------------------------------------------------------
if include_labels:
    st.divider()
    st.subheader("Impression des étiquettes Fruits et Légumes")
    label_print_category_id = _label_print_category_id()
    label_report_name = _label_report_name()
    st.caption("Les étiquettes sont préparées depuis les lignes analysées de la facture, sans CSV intermédiaire.")

    label_dry_run_summary = st.session_state.get("label_dry_run_summary")
    if st.button("Vérifier les étiquettes sans générer le PDF", key="dry_run_labels"):
        try:
            with st.spinner("Vérification des étiquettes dans Odoo..."):
                label_dry_run_summary = dry_run_labels_from_invoice_lines(
                    result.all_lines,
                    _odoo_config_from_streamlit(),
                    print_category_id=label_print_category_id,
                )
            st.session_state["label_dry_run_summary"] = label_dry_run_summary
            st.session_state.pop("labels_pdf", None)
            st.session_state.pop("labels_pdf_message", None)
        except Exception as exc:
            st.error(f"Impossible de vérifier les étiquettes dans Odoo : {exc}")

    if label_dry_run_summary is None:
        st.info("Lancez une vérification pour contrôler les articles Fruits et Légumes avant génération du PDF.")
    else:
        if label_dry_run_summary.errors:
            st.error(label_dry_run_summary.message)
        elif label_dry_run_summary.ready:
            st.success(label_dry_run_summary.message)
        else:
            st.warning(label_dry_run_summary.message)

        if label_dry_run_summary.labels.empty:
            st.info("Aucune ligne éligible pour l'impression des étiquettes.")
        else:
            st.dataframe(label_dry_run_summary.labels, use_container_width=True, hide_index=True)

        if not label_dry_run_summary.excluded_lines.empty:
            if label_dry_run_summary.errors:
                error_lines = label_dry_run_summary.excluded_lines[
                    label_dry_run_summary.excluded_lines["status"] == "error"
                ]
                st.warning(f"{len(error_lines)} ligne(s) demandent une correction avant impression.")
                st.dataframe(error_lines, use_container_width=True, hide_index=True)
            _show_label_diagnostics(
                label_dry_run_summary.excluded_lines,
                developer_mode,
                label_print_category_id,
                label_report_name,
            )

        labels_ready = label_dry_run_summary.ready > 0 and label_dry_run_summary.errors == 0
        if st.session_state.get("labels_pdf") and st.session_state.get("labels_pdf_message"):
            st.success(st.session_state["labels_pdf_message"])
            st.download_button(
                "Télécharger les étiquettes PDF",
                data=st.session_state["labels_pdf"],
                file_name=f"{invoice.invoice_number or 'facture'}_etiquettes_fl.pdf",
                mime="application/pdf",
                key="download_labels_pdf",
            )
        else:
            confirm_labels = st.checkbox(
                "J'ai vérifié les étiquettes et je veux générer le PDF",
                key="confirm_labels",
                disabled=not labels_ready,
            )
            if st.button("Générer le PDF des étiquettes", disabled=not confirm_labels, key="generate_labels_pdf"):
                try:
                    with st.spinner("Génération du PDF des étiquettes depuis Odoo..."):
                        summary = generate_labels_pdf_from_invoice_lines(
                            result.all_lines,
                            _odoo_config_from_streamlit(),
                            print_category_id=label_print_category_id,
                            report_name=label_report_name,
                        )
                    if summary.status == "success" and summary.pdf:
                        st.session_state["labels_pdf"] = summary.pdf
                        st.session_state["labels_pdf_message"] = summary.message
                        st.success(summary.message)
                        st.download_button(
                            "Télécharger les étiquettes PDF",
                            data=summary.pdf,
                            file_name=f"{invoice.invoice_number or 'facture'}_etiquettes_fl.pdf",
                            mime="application/pdf",
                            key="download_labels_pdf_after_generation",
                        )
                    else:
                        st.error(summary.message)
                        _show_label_diagnostics(
                            summary.excluded_lines,
                            developer_mode,
                            label_print_category_id,
                            label_report_name,
                        )
                except Exception as exc:
                    st.error(f"Impossible de générer le PDF des étiquettes : {exc}")

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
        dry_run_summary = price_dry_run_summary
        if st.button("Vérifier la MàJ sans écrire dans Odoo", key="dry_run_prices"):
            try:
                with st.spinner("Vérification de la MàJ sans écrire dans Odoo..."):
                    dry_run_summary = dry_run_odoo_price_updates(price_update_rows, _odoo_config_from_streamlit())
                st.session_state["price_dry_run_summary"] = dry_run_summary
            except Exception as exc:
                st.error(f"Impossible de vérifier les prix dans Odoo : {exc}")

        if dry_run_summary is not None:
            if dry_run_summary.errors:
                st.error(
                    f"Vérification Odoo avec erreurs : {dry_run_summary.success} prêt(s), "
                    f"{dry_run_summary.warnings} avertissement(s), {dry_run_summary.errors} erreur(s)."
                )
            else:
                st.success(
                    f"Vérification Odoo terminée : {dry_run_summary.success} prêt(s), "
                    f"{dry_run_summary.warnings} avertissement(s), 0 erreur."
                )
            st.dataframe(dry_run_summary.results, use_container_width=True, hide_index=True)

        if st.session_state.get("prices_updated"):
            st.success(st.session_state["prices_updated"])
        else:
            dry_run_ok = dry_run_summary is not None and dry_run_summary.errors == 0
            if not dry_run_ok:
                st.info("Vous pouvez lancer une vérification de la MàJ sans écrire dans Odoo.")
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
    "Les actions Odoo ne sont jamais automatiques : elles ne sont lancées "
    "qu'après vérification et validation explicite par case à cocher."
)

st.divider()
st.subheader("Classeur de contrôle")
price_dry_run_summary = st.session_state.get("price_dry_run_summary")
price_dry_run_results = price_dry_run_summary.results if price_dry_run_summary is not None else None
po_dry_run_summary = st.session_state.get("po_dry_run_summary")
po_dry_run_results = po_dry_run_summary.results if po_dry_run_summary is not None else None
po_creation_results = st.session_state.get("po_creation_results")
label_dry_run_summary = st.session_state.get("label_dry_run_summary")
label_dry_run_results = label_dry_run_summary.labels if label_dry_run_summary is not None else None
label_excluded_results = label_dry_run_summary.excluded_lines if label_dry_run_summary is not None else None
st.download_button(
    "Telecharger le classeur de controle",
    data=result_workbook_bytes(
        result,
        include_purchase_order=include_purchase_order,
        include_price_review=include_price_review,
        include_developer_sheets=developer_mode,
        price_dry_run_results=price_dry_run_results,
        po_dry_run_results=po_dry_run_results,
        po_creation_results=po_creation_results,
        label_dry_run_results=label_dry_run_results,
        label_excluded_results=label_excluded_results,
    ),
    file_name=f"{invoice.invoice_number or 'facture'}_controle_odoo.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
