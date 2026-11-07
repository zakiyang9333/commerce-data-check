"""Extract clean, de-duplicated period facts from SEC companyfacts JSON.

companyfacts layout: facts[taxonomy][concept]["units"][unit] -> list of
{start?, end, val, accn, fy, fp, form, filed, frame?}. Duration facts carry
`start`; instant (balance sheet) facts do not.

De-duplication rule (fixed, documented in METHODOLOGY.md): for the same
period the value from the most recently *filed* 10-K/10-Q (incl. amendments)
wins, i.e. "as most recently reported". The accession number is kept so
every number can be traced back to its filing.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

ANNUAL_FORMS = {"10-K", "10-K/A", "10-KT", "10-KT/A", "20-F", "20-F/A", "40-F", "40-F/A"}
QUARTER_FORMS = {"10-Q", "10-Q/A", "10-QT", "10-QT/A"}
ALLOWED_FORMS = ANNUAL_FORMS | QUARTER_FORMS


def d(s: str) -> date:
    return date.fromisoformat(s)


def period_kind(start: str, end: str) -> Optional[str]:
    """Classify a duration by length. Ranges allow 52/53-week fiscal years."""
    days = (d(end) - d(start)).days + 1
    if 80 <= days <= 100:
        return "Q"
    if 170 <= days <= 195:
        return "H"
    if 260 <= days <= 290:
        return "9M"
    if 350 <= days <= 380:
        return "FY"
    return None


@dataclass(frozen=True)
class Fact:
    concept: str
    val: float
    end: str
    start: Optional[str]
    accn: str
    form: str
    filed: str
    fy: Optional[int]
    fp: Optional[str]
    unit: str

    @property
    def kind(self) -> Optional[str]:
        return period_kind(self.start, self.end) if self.start else "I"

    @property
    def days(self) -> Optional[int]:
        return (d(self.end) - d(self.start)).days + 1 if self.start else None

    def source(self, cik: int) -> dict:
        out = asdict(self)
        out["url"] = filing_url(cik, self.accn)
        return out


def filing_url(cik: int, accn: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn.replace('-', '')}/"


def pick_unit(cf: dict, taxonomy: str) -> Optional[str]:
    """Reporting currency: the monetary unit used by the 'Assets' concept
    (falls back to the most common unit). Ratios are unit-free, but EBIT and
    capital must share one unit."""
    facts = cf.get("facts", {}).get(taxonomy, {})
    for c in ("Assets",):
        units = facts.get(c, {}).get("units", {})
        if units:
            return max(units, key=lambda u: len(units[u]))
    counts: Dict[str, int] = {}
    for body in facts.values():
        for u, rows in body.get("units", {}).items():
            if u not in ("shares", "pure") and "/" not in u:
                counts[u] = counts.get(u, 0) + len(rows)
    return max(counts, key=counts.get) if counts else None


def pick_taxonomy(cf: dict) -> Optional[str]:
    facts = cf.get("facts", {})
    if "us-gaap" in facts and "Assets" in facts["us-gaap"]:
        return "us-gaap"
    if "ifrs-full" in facts and "Assets" in facts["ifrs-full"]:
        return "ifrs-full"
    return "us-gaap" if "us-gaap" in facts else ("ifrs-full" if "ifrs-full" in facts else None)


class Series:
    """One economic item, merged from an ordered list of equivalent concepts.

    For each period the highest-priority concept that has a value wins; the
    concept actually used is recorded on the Fact (audit trail)."""

    def __init__(self, facts: Iterable[Fact]):
        self.by_key: Dict[Tuple[Optional[str], str], Fact] = {}
        for f in facts:
            self.by_key[(f.start, f.end)] = f

    def __len__(self) -> int:
        return len(self.by_key)

    def durations(self, kind: Optional[str] = None) -> List[Fact]:
        out = [f for f in self.by_key.values() if f.start and (kind is None or f.kind == kind)]
        return sorted(out, key=lambda f: (f.end, f.start))

    def instants(self) -> List[Fact]:
        return sorted((f for f in self.by_key.values() if not f.start), key=lambda f: f.end)

    def get(self, start: Optional[str], end: str) -> Optional[Fact]:
        return self.by_key.get((start, end))

    def instant_near(self, target: date, tol_days: int = 10) -> Optional[Fact]:
        best = None
        for f in self.instants():
            gap = abs((d(f.end) - target).days)
            if gap <= tol_days and (best is None or gap < abs((d(best.end) - target).days)):
                best = f
        return best

    def duration_near(self, kind: str, end: date, start: Optional[date] = None, tol_days: int = 10) -> Optional[Fact]:
        best, best_gap = None, None
        for f in self.durations(kind):
            gap = abs((d(f.end) - end).days)
            if start is not None:
                gap += abs((d(f.start) - start).days)
                if abs((d(f.start) - start).days) > tol_days:
                    continue
            if abs((d(f.end) - end).days) > tol_days:
                continue
            if best is None or gap < best_gap:
                best, best_gap = f, gap
        return best


def load_concept(cf: dict, taxonomy: str, concept: str, unit: str) -> List[Fact]:
    rows = cf.get("facts", {}).get(taxonomy, {}).get(concept, {}).get("units", {}).get(unit, [])
    chosen: Dict[Tuple[Optional[str], str], Fact] = {}
    for r in rows:
        form = r.get("form", "")
        if form not in ALLOWED_FORMS or r.get("val") is None or "end" not in r:
            continue
        start = r.get("start")
        if start and period_kind(start, r["end"]) is None:
            continue  # odd-length periods (e.g. stub/transition) are not used
        f = Fact(concept, float(r["val"]), r["end"], start, r.get("accn", ""), form,
                 r.get("filed", ""), r.get("fy"), r.get("fp"), unit)
        key = (start, r["end"])
        old = chosen.get(key)
        if old is None or (f.filed, f.accn) > (old.filed, old.accn):
            chosen[key] = f
    return list(chosen.values())


def load_series(cf: dict, taxonomy: str, concepts: Sequence[str], unit: str) -> Series:
    merged: Dict[Tuple[Optional[str], str], Fact] = {}
    for c in reversed(concepts):  # lower priority first, overwritten by higher
        for f in load_concept(cf, taxonomy, c, unit):
            merged[(f.start, f.end)] = f
    return Series(merged.values())
