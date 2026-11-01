"""Money units. Graph budgets (and settings.meta.daily_budget) are in paise; Meta insight `spend` and the
autopause thresholds are in rupees. Convert here, nowhere else."""
from __future__ import annotations

PAISE_PER_RUPEE = 100


def paise_to_inr(paise: float) -> float:
    return paise / PAISE_PER_RUPEE


def inr_to_paise(inr: float) -> int:
    return round(inr * PAISE_PER_RUPEE)


def rupees(paise: int) -> str:
    """A paise amount as '1,234.50' for reports."""
    return f"{paise_to_inr(paise):,.2f}"
