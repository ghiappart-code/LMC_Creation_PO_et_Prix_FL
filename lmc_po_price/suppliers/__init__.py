"""Catalogue des parseurs de factures fournisseur.

Chaque fournisseur peut avoir une mise en page PDF différente. Ce paquet isole
donc le décodage propre à chaque fournisseur et expose une fonction commune qui
retourne toujours un `ParsedInvoice` normalisé pour les traitements partagés.
"""

from __future__ import annotations

from lmc_po_price.models import ParsedInvoice
from lmc_po_price.suppliers.le_relais_local import LeRelaisLocalParser
from lmc_po_price.suppliers.pdf import extract_pdf_text_and_rows
from lmc_po_price.suppliers.relais_vert import RelaisVertParser


PARSERS = [
    LeRelaisLocalParser(),
    RelaisVertParser(),
]


def list_suppliers() -> list[str]:
    return sorted(parser.supplier_code for parser in PARSERS)


def supplier_label(supplier_code: str) -> str:
    parser = _parser_by_code(supplier_code)
    if parser is None:
        return supplier_code
    if parser.supplier_code == LeRelaisLocalParser.supplier_code:
        return f"Relais Local ({parser.supplier_code})"
    return f"{parser.display_name} ({parser.supplier_code})"


def detect_supplier_from_text(text: str) -> str | None:
    for parser in PARSERS:
        if parser.matches(text):
            return parser.supplier_code
    return None


def parse_invoice_pdf(file_or_path, expected_supplier_code: str | None = None) -> ParsedInvoice:
    """Détecte le fournisseur puis délègue le décodage au parseur adapté."""
    text, rows = extract_pdf_text_and_rows(file_or_path)
    for parser in PARSERS:
        if parser.matches(text):
            if expected_supplier_code is not None and parser.supplier_code != str(expected_supplier_code):
                raise ValueError(
                    "Le fournisseur selectionne ne correspond pas a la facture. "
                    f"Facture detectee: {parser.display_name} ({parser.supplier_code})."
                )
            return parser.parse_file(file_or_path, text, rows)
    raise ValueError("Fournisseur non reconnu pour cette version.")


def _parser_by_code(supplier_code: str):
    for parser in PARSERS:
        if parser.supplier_code == str(supplier_code):
            return parser
    return None
