from __future__ import annotations

from abc import ABC, abstractmethod

from lmc_po_price.models import ParsedInvoice


class SupplierInvoiceParser(ABC):
    """Interface commune pour les parseurs de factures fournisseur.

    Les parseurs fournisseurs ne doivent faire qu'une chose : transformer un PDF
    fournisseur en lignes facture normalisées. Les comparaisons avec Odoo, la
    génération du bon de commande et les exports restent dans les modules communs.
    """

    supplier_code: str
    display_name: str

    @abstractmethod
    def matches(self, text: str) -> bool:
        """Retourne True si le texte PDF semble appartenir à ce fournisseur."""

    @abstractmethod
    def parse(self, text: str, rows: list[str]) -> ParsedInvoice:
        """Décode le texte et les lignes visuelles du PDF en `ParsedInvoice`."""

