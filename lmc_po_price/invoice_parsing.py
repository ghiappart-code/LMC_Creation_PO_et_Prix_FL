from lmc_po_price.models import ParsedInvoice
from lmc_po_price.suppliers import parse_invoice_pdf as _parse_invoice_pdf


def parse_invoice_pdf(file_or_path) -> ParsedInvoice:
    """Point d'entrée historique conservé pour compatibilité."""
    return _parse_invoice_pdf(file_or_path)
