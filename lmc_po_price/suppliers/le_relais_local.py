from __future__ import annotations

import re
from datetime import date

import pandas as pd

from lmc_po_price.models import ParsedInvoice
from lmc_po_price.suppliers.base import SupplierInvoiceParser
from lmc_po_price.text import normalize_key, parse_decimal


class LeRelaisLocalParser(SupplierInvoiceParser):
    """Parseur de facture Le Relais Local.

    Cette facture contient une ligne `CONTRIBUTION TRANSPORT 0,6%` qui n'est pas
    un produit. Le parseur l'isole dans `charges` et applique son taux aux prix
    unitaires produits dans `prix_unitaire_ajuste`.
    """

    supplier_code = "244"
    display_name = "Le Relais Local"

    def matches(self, text: str) -> bool:
        text_key = text.casefold()
        return "lerelaislocal.fr" in text_key or "le relais local" in text_key

    def parse(self, text: str, rows: list[str]) -> ParsedInvoice:
        product_rows: list[dict[str, object]] = []
        charge_rows: list[dict[str, object]] = []

        for row in rows:
            if row.startswith("l'unité") and product_rows:
                product_rows[-1]["designation"] = f"{product_rows[-1]['designation']} {row}".strip()
                continue
            match = re.match(r"^(?P<ref>[A-Z0-9]{4,})\s+(?P<body>.+)$", row)
            if not match:
                continue
            parsed = _parse_line_body(match.group("ref"), match.group("body"))
            if parsed is None:
                continue
            if _is_transport_charge(parsed["reference_fournisseur"], parsed["designation"]):
                charge_rows.append(parsed)
            else:
                product_rows.append(parsed)

        transport_rate = _transport_rate(charge_rows)
        for row in product_rows:
            row["taux_transport"] = transport_rate
            row["prix_unitaire_ajuste"] = round(float(row["prix_unitaire"]) * (1 + transport_rate), 6)

        return ParsedInvoice(
            supplier_code=self.supplier_code,
            supplier_name=self.display_name,
            invoice_number=_search_text(r"Facture\s+N[°o]\s+(FC\d+)", text),
            invoice_date=_parse_french_date(_search_text(r"(\d{2}/\d{2}/\d{4})\s+DEMAI", text)),
            delivery_date=_parse_french_date(_search_text(r"Date Livraison\s+(\d{2}/\d{2}/\d{4})", text)),
            lines=pd.DataFrame(product_rows),
            charges=pd.DataFrame(charge_rows),
            metadata={
                "fournisseur": self.display_name,
                "taux_transport": transport_rate,
                "source": "pdf",
            },
        )


def _parse_line_body(reference: str, body: str) -> dict[str, object] | None:
    structured = re.match(
        r"^(?P<designation>.+?)\s+"
        r"(?P<colis>[#A-Z0-9.,]+)\s+"
        r"(?P<quantite>\d+(?:[,.]\d+)?)\s+"
        r"(?P<unite>U|KG|irgule)?\s*"
        r"(?P<brut>\d+[,.]\d+)\s+"
        r"(?P<net>\d+[,.]\d+)\s+"
        r"(?P<montant>\d+[,.]\d+)\s+"
        r"(?P<tva>\d+)$",
        body,
        flags=re.IGNORECASE,
    )
    if structured:
        unit = (structured.group("unite") or "").upper()
        if unit == "IRGULE":
            unit = "KG"
        return {
            "reference_fournisseur": reference,
            "designation": _cleanup_designation(structured.group("designation")),
            "quantite": parse_decimal(structured.group("quantite")),
            "unite": unit,
            "prix_unitaire": parse_decimal(structured.group("net")),
            "prix_unitaire_brut": parse_decimal(structured.group("brut")),
            "montant_ht": parse_decimal(structured.group("montant")),
            "code_tva": structured.group("tva"),
            "reference_key": normalize_key(reference),
            "designation_key": normalize_key(structured.group("designation")),
        }

    numbers = re.findall(r"-?\d+,\d+|-?\d+\.\d+|-?\d+", body)
    if len(numbers) < 4:
        return None
    amount = parse_decimal(numbers[-2])
    net_price = parse_decimal(numbers[-3])
    gross_price = parse_decimal(numbers[-4])
    if amount is None or net_price is None:
        return None
    quantity = round(amount / net_price, 6) if net_price else None
    price_pos = body.rfind(numbers[-4])
    before_prices = body[:price_pos].strip()
    unit_match = re.search(r"\b(U|KG)\b\s*$", before_prices, flags=re.IGNORECASE)
    unit = unit_match.group(1).upper() if unit_match else ""
    before_unit = before_prices[: unit_match.start()].strip() if unit_match else before_prices
    designation = _cleanup_designation(before_unit)

    return {
        "reference_fournisseur": reference,
        "designation": designation,
        "quantite": quantity,
        "unite": unit,
        "prix_unitaire": net_price,
        "prix_unitaire_brut": gross_price,
        "montant_ht": amount,
        "code_tva": numbers[-1],
        "reference_key": normalize_key(reference),
        "designation_key": normalize_key(designation),
    }


def _cleanup_designation(value: str) -> str:
    text = re.sub(r"\b(BIO)\b.*$", r"\1", value).strip()
    return re.sub(r"\s+", " ", text)


def _is_transport_charge(reference: str, designation: object) -> bool:
    key = normalize_key(f"{reference} {designation}")
    return "contributiontransport" in key or reference.casefold().startswith("gasoi")


def _transport_rate(charges: list[dict[str, object]]) -> float:
    for charge in charges:
        match = re.search(r"(\d+(?:[,.]\d+)?)\s*%", str(charge.get("designation", "")))
        if match:
            return float(match.group(1).replace(",", ".")) / 100
    return 0.0


def _search_text(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _parse_french_date(value: str | None) -> date | None:
    if not value:
        return None
    day, month, year = value.split("/")
    return date(int(year), int(month), int(day))

