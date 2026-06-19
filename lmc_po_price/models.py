from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class ParsedInvoice:
    supplier_code: str
    supplier_name: str
    invoice_number: str | None
    invoice_date: date | None
    delivery_date: date | None
    lines: pd.DataFrame
    charges: pd.DataFrame = field(default_factory=pd.DataFrame)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowConfig:
    supplier_code: str
    price_increase_threshold: float = 0.05
    price_decrease_threshold: float = -0.10
    abnormal_ratio: float = 0.30


@dataclass(frozen=True)
class WorkflowResult:
    invoice: ParsedInvoice
    all_lines: pd.DataFrame
    matched: pd.DataFrame
    unmatched: pd.DataFrame
    ambiguous: pd.DataFrame
    price_changes: pd.DataFrame
    purchase_order_review: pd.DataFrame
    sale_flag_review: pd.DataFrame

