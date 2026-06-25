from __future__ import annotations

import re
from datetime import date, datetime

import pandas as pd

from lmc_po_price.models import ParsedInvoice
from lmc_po_price.suppliers.base import SupplierInvoiceParser
from lmc_po_price.suppliers.pdf import _pdf_bytes
from lmc_po_price.text import normalize_key, parse_decimal


class RelaisVertParser(SupplierInvoiceParser):
    supplier_code = "254"
    display_name = "Relais Vert"

    def matches(self, text: str) -> bool:
        text_key = text.casefold()
        return "relais-vert.com" in text_key or "relais vert" in text_key

    def parse(self, text: str, rows: list[str]) -> ParsedInvoice:
        raise ValueError("Le parseur Relais Vert doit recevoir le PDF source.")

    def parse_file(self, file_or_path, text: str, rows: list[str]) -> ParsedInvoice:
        import fitz

        records: list[dict[str, object]] = []
        fuel_pct = _find_fuel_surcharge_pct(text)
        invoice_date = _parse_french_date(_find_invoice_date(text))

        with fitz.open(stream=_pdf_bytes(file_or_path), filetype="pdf") as doc:
            for page_index, page in enumerate(doc, start=1):
                records.extend(self._parse_page(page, page_index, fuel_pct))

        lines = pd.DataFrame(records)
        return ParsedInvoice(
            supplier_code=self.supplier_code,
            supplier_name=self.display_name,
            invoice_number=_find_invoice_number(text),
            invoice_date=invoice_date,
            delivery_date=None,
            lines=lines,
            charges=pd.DataFrame(),
            metadata={
                "fournisseur": self.display_name,
                "taxe_gazole_pct": fuel_pct,
                "source": "pdf",
                "lignes_decodees": len(lines),
            },
        )

    def _parse_page(self, page: object, page_index: int, fuel_pct: float) -> list[dict[str, object]]:
        words = [
            (x0, y0, x1, y1, text)
            for x0, y0, x1, y1, text, *_ in page.get_text("words")
            if 280 <= y0 <= 780
        ]
        words.sort(key=lambda item: (item[1], item[0]))

        starts: list[tuple[float, str]] = []
        for word in words:
            x0, y0, *_ = word
            if not 38 <= x0 <= 80:
                continue
            reference = self._reference_from_words([word])
            if reference is not None:
                starts.append((y0, reference))

        starts.sort(key=lambda item: item[0])
        table_end_y = self._table_end_y(words, starts[-1][0] if starts else 790)
        product_rows: list[dict[str, object]] = []
        for index, (start_y, reference) in enumerate(starts):
            next_y = starts[index + 1][0] if index + 1 < len(starts) else table_end_y
            items = [word for word in words if start_y - 2 <= word[1] < next_y - 2]
            row = self._row_from_items(reference, items, page_index, fuel_pct)
            if row:
                product_rows.append(row)
        return product_rows

    def _row_from_items(
        self,
        reference: str,
        items: list[tuple],
        page_index: int,
        fuel_pct: float,
    ) -> dict[str, object]:
        designation = " ".join(item[4] for item in items if 85 <= item[0] <= 255)
        if "GAZOLE" in designation.upper():
            return {}

        unit_price = self._number_in_band(items, 480, 512)
        gross_price = self._number_in_band(items, 390, 420)
        q_discount = self._number_in_band(items, 420, 435) or 0.0
        g_discount = self._number_in_band(items, 435, 450) or 0.0
        p_discount = self._number_in_band(items, 450, 463) or 0.0
        e_discount = self._number_in_band(items, 463, 477) or 0.0
        quantity = self._number_in_band(items, 360, 382)
        amount = self._number_in_band(items, 515, 542)
        adjusted_price = unit_price * (1 + fuel_pct / 100) if unit_price is not None else None

        return {
            "reference_fournisseur": reference,
            "designation": _cleanup_designation(designation),
            "quantite": quantity,
            "unite": "",
            "prix_unitaire": unit_price,
            "prix_unitaire_brut": gross_price,
            "prix_unitaire_ajuste": adjusted_price,
            "montant_ht": amount,
            "code_tva": "",
            "reference_key": normalize_key(reference),
            "designation_key": normalize_key(designation),
            "q_discount": q_discount,
            "g_discount": g_discount,
            "p_discount": p_discount,
            "e_discount": e_discount,
            "remise_temp": int(bool(q_discount or p_discount or e_discount)),
            "remise_detail": _remise_detail(q_discount, p_discount, e_discount),
            "taux_gazole_pct": fuel_pct,
            "page": page_index,
        }

    def _reference_from_words(self, words: list[tuple]) -> str | None:
        for word in words:
            cleaned = str(word[4]).strip()
            if cleaned.startswith("BL"):
                continue
            if cleaned.isdigit() and len(cleaned) == 13:
                continue
            if re.fullmatch(r"[A-Z0-9]{4,6}", cleaned):
                return cleaned
        return None

    def _number_in_band(self, items: list[tuple], x_min: float, x_max: float) -> float | None:
        values = [
            parse_decimal(item[4])
            for item in items
            if x_min <= item[0] <= x_max and parse_decimal(item[4]) is not None
        ]
        return values[0] if values else None

    def _table_end_y(self, words: list[tuple], last_row_y: float) -> float:
        footer_markers = {"total", "echeance", "échéance"}
        candidates = [
            word[1]
            for word in words
            if word[1] > last_row_y
            and 35 <= word[0] <= 110
            and str(word[4]).strip().casefold() in footer_markers
        ]
        return min(candidates) if candidates else 790


def _find_invoice_number(text: str) -> str | None:
    match = re.search(r"\bFC\d+\b", text)
    return match.group(0) if match else None


def _find_invoice_date(text: str) -> str | None:
    match = re.search(r"\b\d{2}/\d{2}/\d{4}\b", text)
    return match.group(0) if match else None


def _find_fuel_surcharge_pct(text: str) -> float:
    match = re.search(r"GAZOLE\s*:\s*([0-9]+(?:[,.][0-9]+)?)\s*%", text, re.IGNORECASE)
    if not match:
        return 0.0
    return parse_decimal(match.group(1)) or 0.0


def _parse_french_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d/%m/%Y").date()
    except ValueError:
        return None


def _cleanup_designation(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _remise_detail(q_discount: float, p_discount: float, e_discount: float) -> str:
    parts = []
    if q_discount:
        parts.append(f"Q*={q_discount:g}")
    if p_discount:
        parts.append(f"P={p_discount:g}")
    if e_discount:
        parts.append(f"E={e_discount:g}")
    return ", ".join(parts)
