from __future__ import annotations

import re
from os import PathLike


def extract_pdf_text_and_rows(file_or_path) -> tuple[str, list[str]]:
    """Extrait le texte PDF et des lignes visuelles triées par position.

    PyMuPDF (`fitz`) est utilisé en priorité car les factures fournisseurs sont
    souvent tabulaires. L'ordre textuel brut peut mélanger les colonnes, alors
    que les positions des mots permettent de reconstruire les lignes produit.
    """
    try:
        import fitz

        data = _pdf_bytes(file_or_path)
        doc = fitz.open(stream=data, filetype="pdf")
        text = "\n".join(page.get_text() for page in doc)
        rows: list[str] = []
        for page in doc:
            words = [
                {"x0": item[0], "top": item[1], "x1": item[2], "text": item[4]}
                for item in page.get_text("words")
            ]
            rows.extend(_visual_rows(words, top_min=285, top_max=560))
        return text, rows
    except Exception:
        pass

    try:
        from pypdf import PdfReader

        reader = PdfReader(file_or_path)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        return text, _invoice_rows_from_text(text)
    except Exception as exc:
        raise ValueError(f"Impossible de lire le texte du PDF : {exc}") from exc


def _pdf_bytes(file_or_path) -> bytes:
    if isinstance(file_or_path, bytes):
        return file_or_path
    if isinstance(file_or_path, (str, PathLike)):
        with open(file_or_path, "rb") as handle:
            return handle.read()
    if hasattr(file_or_path, "seek"):
        file_or_path.seek(0)
    data = file_or_path.read()
    if hasattr(file_or_path, "seek"):
        file_or_path.seek(0)
    return data


def _visual_rows(words: list[dict], top_min: float, top_max: float) -> list[str]:
    selected = [word for word in words if top_min <= float(word["top"]) <= top_max]
    clusters: list[list[dict]] = []
    for word in sorted(selected, key=lambda item: (float(item["top"]), float(item["x0"]))):
        if not clusters or abs(float(clusters[-1][0]["top"]) - float(word["top"])) > 3:
            clusters.append([word])
        else:
            clusters[-1].append(word)
    return [" ".join(word["text"] for word in sorted(cluster, key=lambda item: float(item["x0"]))) for cluster in clusters]


def _invoice_rows_from_text(text: str) -> list[str]:
    rows: list[str] = []
    capture = False
    current = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("Bon de livraison client"):
            capture = True
            continue
        if line.startswith("BIO et conversion") or line.startswith("IBAN:"):
            break
        if not capture:
            continue
        if re.match(r"^[A-Z0-9]{4,}\s+", line):
            if current:
                rows.append(current)
            current = line
        elif current:
            current = f"{current} {line}"
    if current:
        rows.append(current)
    return rows

