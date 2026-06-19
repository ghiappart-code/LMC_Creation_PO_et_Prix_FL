from __future__ import annotations

import re
import unicodedata


def normalize_key(value: object) -> str:
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    return re.sub(r"[^a-z0-9]+", "", text)


def normalize_column_name(column: object) -> str:
    text = str(column).strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.replace(" ", "_").replace("-", "_")


def parse_decimal(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace("\u202f", "").replace(" ", "").replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    return float(match.group(0))

