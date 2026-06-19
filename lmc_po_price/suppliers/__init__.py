"""Catalogue des parseurs de factures fournisseur.

Chaque fournisseur peut avoir une mise en page PDF différente. Ce paquet isole
donc le décodage propre à chaque fournisseur et expose une fonction commune qui
retourne toujours un `ParsedInvoice` normalisé pour les traitements partagés.
"""

from __future__ import annotations

from lmc_po_price.models import ParsedInvoice
from lmc_po_price.suppliers.le_relais_local import LeRelaisLocalParser
from lmc_po_price.suppliers.pdf import extract_pdf_text_and_rows


PARSERS = [
    LeRelaisLocalParser(),
]


def parse_invoice_pdf(file_or_path) -> ParsedInvoice:
    """Détecte le fournisseur puis délègue le décodage au parseur adapté."""
    text, rows = extract_pdf_text_and_rows(file_or_path)
    for parser in PARSERS:
        if parser.matches(text):
            return parser.parse(text, rows)
    raise ValueError("Fournisseur non reconnu pour cette version.")

