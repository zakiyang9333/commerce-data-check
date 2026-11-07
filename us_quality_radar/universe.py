"""Build the stock universe from SEC files.

Inputs (SEC, public):
- company_tickers_exchange.json: {"fields": [cik, name, ticker, exchange], "data": [...]}
- submissions/CIK##########.json: sic, sicDescription, entityType, formType history ...

Rules (section 2 of the spec):
- Keep listings on Nasdaq, NYSE, NYSE American; one row per company (CIK).
- Keep common stock tickers only (drop warrants, units, rights, preferreds).
- Exclude ETFs / funds / unit trusts / commodity pools, closed-end funds,
  blank-check SPACs, companies in liquidation.
- Banks, insurers, brokers and other financials -> group "financial";
  REITs -> group "reit". They get ROCE but are ranked separately.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional

EXCHANGES = {
    "nasdaq": "Nasdaq", "nyse": "NYSE",
    "nyse american": "NYSE American", "nyseamerican": "NYSE American", "nyse mkt": "NYSE American",
    "amex": "NYSE American", "nyse arca": "NYSE Arca",
}
MAJOR = {"Nasdaq", "NYSE", "NYSE American"}

FUND_FORMS = {"N-CSR", "N-CSRS", "N-PORT", "NPORT-P", "485BPOS", "N-2", "N-1A", "N-CEN", "N-30D", "497K", "497"}
OPERATING_FORMS = {"10-K", "10-Q", "20-F", "40-F", "10-K/A", "10-Q/A", "20-F/A", "40-F/A", "10-KT"}
FUND_NAME = re.compile(r"\b(ETF|ETN|FUND|ISHARES|SPDR|PROSHARES|INVESCO DB|DIREXION|VANECK|WISDOMTREE|GRAYSCALE)\b", re.I)
LIQUIDATING = re.compile(r"LIQUIDATING|IN LIQUIDATION", re.I)


def norm_exchange(x: Optional[str]) -> Optional[str]:
    if not x:
        return None
    return EXCHANGES.get(x.strip().lower(), x.strip())


def is_common_ticker(t: str, exchange: Optional[str]) -> bool:
    """Heuristic share-class filter, documented in METHODOLOGY.md."""
    t = t.upper()
    if "-" in t or "." in t:
        suffix = re.split(r"[-.]", t, maxsplit=1)[1]
        if suffix in {"A", "B", "C"}:          # BRK-B, BF-B, etc.: common share classes
            return True
        return False                           # -WS/-WT warrants, -U units, -R rights, -P* preferreds
    if exchange == "Nasdaq" and len(t) == 5 and t[-1] in "WURZ":
        return False                           # Nasdaq 5th-letter codes: warrant/unit/right/misc
    return True


def sic_group(sic: Optional[int]) -> str:
    """Map SIC to ranking group. Real estate operators (6500-6553) stay general."""
    if sic is None:
        return "general"
    if sic == 6798:
        return "reit"
    if sic == 6770:
        return "spac"
    if 6000 <= sic <= 6499 or 6700 <= sic <= 6799:
        return "financial"
    return "general"


def build(tickers_exchange: dict, submissions: Dict[int, dict]) -> List[dict]:
    fields = tickers_exchange["fields"]
    rows = [dict(zip(fields, r)) for r in tickers_exchange["data"]]
    by_cik: Dict[int, dict] = {}
    for r in rows:
        cik = int(r["cik"])
        ex = norm_exchange(r.get("exchange"))
        e = by_cik.setdefault(cik, {"cik": cik, "name": r["name"], "tickers": [], "all_tickers": [], "exchanges": set()})
        e["all_tickers"].append(f"{r['ticker']}@{ex}")
        if ex in MAJOR and is_common_ticker(r["ticker"], ex):
            e["tickers"].append(r["ticker"])
            e["exchanges"].add(ex)
    out = []
    for cik, e in by_cik.items():
        sub = submissions.get(cik) or {}
        sic = int(sub["sic"]) if str(sub.get("sic") or "").isdigit() else None
        forms = set((sub.get("filings", {}).get("recent", {}) or {}).get("form", []) or [])
        rec = {
            "cik": cik, "name": e["name"],
            "ticker": e["tickers"][0] if e["tickers"] else "",
            "tickers": " ".join(e["tickers"]), "all_listings": " ".join(e["all_tickers"]),
            "exchange": "/".join(sorted(e["exchanges"])),
            "sic": sic, "sic_description": sub.get("sicDescription", ""),
            "entity_type": sub.get("entityType", ""), "state_of_incorporation": sub.get("stateOfIncorporation", ""),
            "fiscal_year_end": sub.get("fiscalYearEnd", ""),
            "filer_type": _filer_type(forms),
            "group": sic_group(sic), "excluded": False, "exclude_reason": "",
        }
        reason = _exclude_reason(rec, e, forms, bool(sub))
        if reason:
            rec["excluded"], rec["exclude_reason"] = True, reason
        out.append(rec)
    return sorted(out, key=lambda r: (r["excluded"], r["ticker"] or r["name"]))


def _filer_type(forms: Iterable[str]) -> str:
    forms = set(forms)
    if forms & {"10-Q", "10-K"}:
        return "10-K/10-Q"
    if "20-F" in forms:
        return "20-F"
    if "40-F" in forms:
        return "40-F"
    return "other"


def _exclude_reason(rec: dict, e: dict, forms: set, have_sub: bool) -> str:
    if not e["tickers"]:
        return "无 Nasdaq/NYSE/NYSE American 普通股代码"
    if not have_sub:
        return "缺少 SEC submissions 资料"
    if forms & FUND_FORMS and not (forms & OPERATING_FORMS):
        return "基金/ETF（申报 N 系列表格）"
    if rec["sic"] in (6221, 6726) or FUND_NAME.search(rec["name"]):
        return "ETF/基金/信托（SIC 或名称）"
    if rec["group"] == "spac":
        return "SPAC 空白支票公司（SIC 6770）"
    if LIQUIDATING.search(rec["name"]):
        return "清算中（名称）"
    if rec["entity_type"] and rec["entity_type"].lower() == "investment":
        return f"非经营实体（{rec['entity_type']}）"
    return ""
