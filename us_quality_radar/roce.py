"""ROCE engine. One function, one caliber, for every company.

ROCE = EBIT / Average Capital Employed
Capital Employed (CE) = Total Assets - Current Liabilities
Average CE = (CE at period start + CE at period end) / 2

EBIT method A (primary): OperatingIncomeLoss, the reported operating
income from continuing operations.
EBIT method B (fallback, flagged): pre-tax income from continuing
operations + interest expense, used only when method A is not tagged.

TTM = latest FY, or YTD(current) + FY(prior) - YTD(prior same length),
or, as a last resort, the sum of four contiguous quarters (Q4 derived as
FY - 9M). The method used is stored with the result.

Nothing is estimated: a missing input yields None (shown as N/A) and the
reason is recorded.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from . import concepts as C
from .xbrl import Fact, Series, d, load_series, pick_taxonomy, pick_unit

YEAR = timedelta(days=365)


# ---------------------------------------------------------------- periods
def _prior_fy(series: Series, start: str) -> Optional[Fact]:
    """FY that ends the day before `start` (±7 days)."""
    return series.duration_near("FY", d(start) - timedelta(days=1), tol_days=7)


def derived_quarters(series: Series) -> List[Tuple[str, str, float, list]]:
    """Discrete quarters: tagged 3-month facts plus Q4 = FY - 9M."""
    out = {(f.start, f.end): (f.start, f.end, f.val, [f], [(1, f.start, f.end)]) for f in series.durations("Q")}
    for fy in series.durations("FY"):
        nine = series.duration_near("9M", d(fy.end) - timedelta(days=91), start=d(fy.start), tol_days=10)
        if nine and nine.start == fy.start:
            q4s = (d(nine.end) + timedelta(days=1)).isoformat()
            key = (q4s, fy.end)
            if key not in out:
                out[key] = (q4s, fy.end, fy.val - nine.val, [fy, nine],
                            [(1, fy.start, fy.end), (-1, nine.start, nine.end)])
    return sorted(out.values(), key=lambda t: t[1])


def ttm(series: Series, end: str) -> Optional[dict]:
    """Trailing-twelve-month value of a duration series ending at `end`."""
    for f in series.durations("FY"):
        if f.end == end:
            return {"value": f.val, "method": "FY", "parts": [f], "terms": [(1, f.start, f.end)]}
    E = d(end)
    for kind in ("9M", "H", "Q"):
        for ytd in [f for f in series.durations(kind) if f.end == end]:
            fy_prior = _prior_fy(series, ytd.start)
            if not fy_prior:
                continue
            prev = series.duration_near(kind, E - YEAR, start=d(fy_prior.start), tol_days=10)
            if prev and prev.end < ytd.start:
                return {"value": ytd.val + fy_prior.val - prev.val,
                        "method": f"YTD({kind})+FY-YTDprior", "parts": [ytd, fy_prior, prev],
                        "terms": [(1, ytd.start, ytd.end), (1, fy_prior.start, fy_prior.end), (-1, prev.start, prev.end)]}
    # last resort: four contiguous discrete quarters
    qs = derived_quarters(series)
    by_end = {q[1]: q for q in qs}
    chain = []
    cur = by_end.get(end)
    while cur and len(chain) < 4:
        chain.append(cur)
        want = d(cur[0]) - timedelta(days=1)
        cur = next((q for q in qs if abs((d(q[1]) - want).days) <= 7), None)
    if len(chain) == 4:
        parts = [p for q in chain for p in q[3]]
        return {"value": sum(q[2] for q in chain), "method": "sum4Q", "parts": parts,
                "terms": [t for q in chain for t in q[4]]}
    return None


def ytd(series: Series, end: str) -> Optional[dict]:
    """Fiscal year-to-date (longest non-FY duration ending `end` that starts at a fiscal-year start)."""
    for kind in ("9M", "H", "Q"):
        for f in series.durations(kind):
            if f.end == end and _prior_fy(series, f.start) is not None:
                return {"value": f.val, "kind": kind, "start": f.start, "days": f.days, "parts": [f]}
    for kind in ("9M", "H", "Q"):  # first fiscal year on file: no prior FY to anchor
        for f in series.durations(kind):
            if f.end == end:
                return {"value": f.val, "kind": kind, "start": f.start, "days": f.days, "parts": [f],
                        "note": "fiscal-year start not confirmed by a prior FY"}
    return None


class Company:
    """Loads every series once and answers period questions."""

    def __init__(self, cik: int, cf: dict):
        self.cik = int(cik)
        self.name = cf.get("entityName", "")
        self.taxonomy = pick_taxonomy(cf)
        self.unit = pick_unit(cf, self.taxonomy) if self.taxonomy else None
        self.s: Dict[str, Series] = {}
        if self.taxonomy and self.unit:
            for key, names in C.CONCEPTS[self.taxonomy].items():
                self.s[key] = load_series(cf, self.taxonomy, names, self.unit)
        self.interest_tagged = len(self.s.get("interest", Series([]))) > 0
        self.ebit_b = self._build_method_b()
        shares = cf.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding", {}).get("units", {}).get("shares", [])
        self.shares = max(shares, key=lambda r: (r.get("end", ""), r.get("filed", ""))) if shares else None

    def _build_method_b(self) -> Series:
        pre, intr = self.s.get("pretax"), self.s.get("interest")
        if not pre:
            return Series([])
        out = []
        for (start, end), f in pre.by_key.items():
            if not start:
                continue
            i = intr.get(start, end) if intr else None
            if i is None and self.interest_tagged:
                continue  # interest is tagged elsewhere but missing here: do not guess
            val = f.val + (i.val if i else 0.0)
            out.append(Fact("Pretax+InterestExpense" if i else "Pretax(interest not tagged)", val, end, start,
                            f.accn, f.form, f.filed, f.fy, f.fp, f.unit))
        return Series(out)

    # ------------------------------------------------------------ capital
    def ce_at(self, target: date, tol_days: int = 0) -> Optional[dict]:
        a = self.s["assets"].instant_near(target, tol_days)
        if not a:
            return None
        lc = self.s["liab_cur"].get(None, a.end)
        if not lc:
            return None
        return {"date": a.end, "assets": a.val, "liab_cur": lc.val, "ce": a.val - lc.val, "parts": [a, lc]}

    def inst(self, key: str, end: str) -> Optional[Fact]:
        s = self.s.get(key)
        return s.get(None, end) if s else None

    def ebit_series(self, method: str) -> Series:
        return self.s["ebit_op"] if method == "A" else self.ebit_b

    def latest_end(self, method: str) -> Optional[str]:
        ends = sorted({f.end for f in self.ebit_series(method).durations()}, reverse=True)
        for e in ends:
            if self.ce_at(d(e)) and ttm(self.ebit_series(method), e):
                return e
        return None


def _avg_ce(co: Company, end: str, begin_target: date) -> dict:
    endp = co.ce_at(d(end))
    begp = co.ce_at(begin_target, tol_days=10)
    if not endp or not begp:
        return {"ok": False, "end": endp, "begin": begp,
                "reason": "缺少期末资本" if not endp else "缺少期初资本（一年前资产负债表）"}
    return {"ok": True, "end": endp, "begin": begp, "avg": (endp["ce"] + begp["ce"]) / 2,
            "avg_assets": (endp["assets"] + begp["assets"]) / 2}


def _ratio(ebit: Optional[float], cap: dict) -> Tuple[Optional[float], str]:
    """Returns (roce, status). status: OK / N/M:<reason> / N/A:<reason>."""
    if ebit is None:
        return None, "N/A:缺少EBIT"
    if not cap.get("ok"):
        return None, "N/A:" + cap.get("reason", "缺少资本")
    if cap["begin"]["ce"] <= 0 or cap["end"]["ce"] <= 0:
        return None, "N/M:资本为零或负数"
    if (cap["avg"] < C.NEAR_ZERO_CE_SHARE_OF_ASSETS * cap["avg_assets"]
            or cap["begin"]["ce"] < C.NEAR_ZERO_CE_SHARE_OF_ASSETS * cap["begin"]["assets"]
            or cap["end"]["ce"] < C.NEAR_ZERO_CE_SHARE_OF_ASSETS * cap["end"]["assets"]):
        return ebit / cap["avg"], "N/M:资本基数接近零"
    return ebit / cap["avg"], "OK"


def _one_off_at(co: Company, start: str, end: str) -> Tuple[Dict[str, float], List[Fact]]:
    """One-off items for one period, combined without double counting:
    a combined restructuring+impairment tag wins; otherwise a total
    impairment tag wins over its components; restructuring is added
    separately. Disposal gains are taken only when positive (removed)."""
    def g(key):
        s = co.s.get(key)
        return s.get(start, end) if s else None
    items, facts = {}, []

    def put(name, f, sign=1.0):
        if f is not None and f.val:
            items[name] = items.get(name, 0.0) + sign * f.val
            facts.append(f)
    combo = g("restr_impair_combo")
    gw = g("impair_gw")
    if combo is not None:
        # ASC 350-20-45-2: goodwill impairment is its own income-statement line,
        # so it is added on top of a combined restructuring/asset-impairment tag.
        put("restructuring+impairment", combo)
        put("goodwill_impairment", gw)
    else:
        intang = g("impair_intang")
        parts = [gw, intang] if intang is not None else [gw, g("impair_intang_indef"), g("impair_intang_fin")]
        parts += [g("impair_lla"), g("impair_lla_disp")]
        parts = [f for f in parts if f is not None]
        tot = g("impair_total")
        comp_sum = sum(f.val for f in parts)
        # total tag and component tags describe the same charges: take the larger, never both
        if tot is not None and tot.val >= comp_sum:
            put("impairment", tot)
        else:
            for f in parts:
                put("impairment", f)
        put("restructuring", g("restructuring"))
    gain = g("disposal_gain")
    if gain is not None and gain.val > 0:
        put("disposal_gain(removed)", gain, -1.0)
    return items, facts


def _adjust(co: Company, terms) -> dict:
    """Adjusted EBIT = EBIT + impairments + restructuring - disposal gains,
    measured over exactly the same periods (same signed terms) as the EBIT
    figure. An item not tagged for a period counts as 0 (recorded)."""
    total: Dict[str, float] = {}
    facts: List[Fact] = []
    for sign, start, end in terms or []:
        items, fs = _one_off_at(co, start, end)
        for k, v in items.items():
            total[k] = total.get(k, 0.0) + sign * v
        facts += fs
    return {"delta": sum(total.values()), "items": total, "sources": facts}


def analyse(cik: int, cf: dict, asof: date, group: str = "general") -> dict:
    co = Company(cik, cf)
    res: dict = {"cik": co.cik, "entity": co.name, "taxonomy": co.taxonomy, "currency": co.unit,
                 "group": group, "flags": [], "notes": [], "sources": []}
    if not co.taxonomy or not co.unit or "assets" not in co.s:
        res.update(status="N/A:无XBRL财务数据", rank_eligible=False)
        return res
    ea, eb = co.latest_end("A"), co.latest_end("B")
    if ea and (not eb or ea >= eb):
        method, end = "A", ea
    elif eb:
        method, end = "B", eb
        res["flags"].append("EBIT_FALLBACK")
        res["notes"].append("未标注 OperatingIncomeLoss，EBIT=税前持续经营利润+利息费用（含非经营性项目）")
        if not co.interest_tagged:
            res["flags"].append("INTEREST_NOT_TAGGED")
    else:
        res.update(status="N/A:无法取得EBIT与同日资产负债表", rank_eligible=False)
        if not co.s["liab_cur"].by_key:
            res["status"] = "N/A:资产负债表未区分流动负债"
        return res
    ser = co.ebit_series(method)
    res["ebit_method"] = method
    res["period_end"] = end
    src_end_fact = max((f for f in ser.durations() if f.end == end), key=lambda f: f.filed)
    res["latest_form"], res["latest_filed"], res["latest_accn"] = src_end_fact.form, src_end_fact.filed, src_end_fact.accn
    res["annual_only"] = not (ser.durations("Q") or ser.durations("H") or ser.durations("9M"))

    # --- current TTM ROCE
    t = ttm(ser, end)
    cap = _avg_ce(co, end, d(end) - YEAR)
    roce, status = _ratio(t["value"] if t else None, cap)
    res.update(ttm_method=t["method"] if t else None, ebit_ttm=t["value"] if t else None,
               ce_begin=cap["begin"]["ce"] if cap.get("begin") else None,
               ce_begin_date=cap["begin"]["date"] if cap.get("begin") else None,
               ce_end=cap["end"]["ce"] if cap.get("end") else None,
               ce_avg=cap.get("avg"), roce_ttm=roce, status=status)
    if t:
        res["sources"] += t["parts"]
    for side in ("begin", "end"):
        if cap.get(side):
            res["sources"] += cap[side]["parts"]

    # --- Adjusted TTM ROCE (EBIT adjustments only; capital unchanged)
    adj = _adjust(co, t["terms"] if t else [])
    res["adj_items_ttm"] = adj["items"]
    res["sources"] += adj["sources"]
    if t:
        res["ebit_ttm_adj"] = t["value"] + adj["delta"]
        res["roce_ttm_adj"], _ = _ratio(res["ebit_ttm_adj"], cap)
    # reference only: capital excluding goodwill
    gw_e, gw_b = (co.inst("goodwill", cap["end"]["date"]) if cap.get("end") else None,
                  co.inst("goodwill", cap["begin"]["date"]) if cap.get("begin") else None)
    if cap.get("ok") and t:
        ce_ex = ((cap["end"]["ce"] - (gw_e.val if gw_e else 0)) + (cap["begin"]["ce"] - (gw_b.val if gw_b else 0))) / 2
        res["roce_ttm_ex_goodwill"] = t["value"] / ce_ex if ce_ex > 0 else None

    # --- YTD ROCE (matched-period average capital)
    y = ytd(ser, end)
    if y:
        fy_prior = _prior_fy(ser, y["start"])
        beg = co.ce_at(d(y["start"]) - timedelta(days=1), tol_days=7)
        endc = co.ce_at(d(end))
        capy = {"ok": bool(beg and endc), "begin": beg, "end": endc,
                "reason": "缺少财年初资本"}
        if capy["ok"]:
            capy["avg"] = (beg["ce"] + endc["ce"]) / 2
            capy["avg_assets"] = (beg["assets"] + endc["assets"]) / 2
        r, st = _ratio(y["value"], capy)
        res.update(ytd_kind=y["kind"], ytd_start=y["start"], ytd_days=y["days"], ebit_ytd=y["value"],
                   roce_ytd=r, roce_ytd_status=st,
                   roce_ytd_annualized=(r * 365 / y["days"]) if r is not None else None)

    # --- annual history (up to 10 FY)
    hist = []
    fys = ser.durations("FY")
    other = co.ebit_series("B" if method == "A" else "A")
    fy_ends = sorted({f.end for f in fys} | {f.end for f in other.durations("FY")})
    for fe in fy_ends[-10:]:
        f = next((x for x in fys if x.end == fe), None)
        m = method
        if f is None:
            f = next((x for x in other.durations("FY") if x.end == fe), None)
            m = "B" if method == "A" else "A"
        capf = _avg_ce(co, fe, d(fe) - YEAR)
        r, st = _ratio(f.val, capf)
        a = _adjust(co, [(1, f.start, f.end)])
        ra, _ = _ratio(f.val + a["delta"], capf)
        hist.append({"fy_end": fe, "label": f"FY{d(fe).year}", "ebit": f.val, "ebit_method": m,
                     "ebit_adj": f.val + a["delta"], "adj_items": a["items"],
                     "ce_begin": capf["begin"]["ce"] if capf.get("begin") else None,
                     "ce_end": capf["end"]["ce"] if capf.get("end") else None,
                     "ce_avg": capf.get("avg"), "roce": r, "roce_adj": ra, "status": st,
                     "accn": f.accn, "form": f.form, "filed": f.filed})
        res["sources"].append(f)
    for i, h in enumerate(hist):
        prev = hist[i - 1] if i else None
        h["roce_yoy_pp"] = ((h["roce"] - prev["roce"]) * 100) if (prev and h["roce"] is not None and prev["roce"] is not None and h["status"] == "OK" and prev["status"] == "OK") else None
    res["annual"] = hist
    if len({h["ebit_method"] for h in hist}) > 1:
        res["flags"].append("EBIT_METHOD_MIXED_IN_HISTORY")

    # --- prior quarter TTM (for quarter-over-quarter comparison)
    ends = sorted({f.end for f in ser.durations() if f.end < end}, reverse=True)
    for pe in ends[:3]:
        pt = ttm(ser, pe)
        pc = _avg_ce(co, pe, d(pe) - YEAR)
        if pt and pc.get("ok"):
            pr, pst = _ratio(pt["value"], pc)
            res.update(prev_period_end=pe, roce_ttm_prev=pr, prev_status=pst)
            break

    _five_year(res)
    _flags(co, res, cap, t, adj, end, asof)
    _grade(res)
    res["sources"] = _dedup_sources(co.cik, res["sources"])
    return res


def _five_year(res: dict) -> None:
    last5 = [h for h in res["annual"]][-5:]
    ok = [h for h in last5 if h["status"] == "OK"]
    res["years_available"] = len(ok)
    res["roce_5y_avg"] = sum(h["roce"] for h in ok) / len(ok) if ok else None
    res["years_ge_20"] = sum(1 for h in ok if h["roce"] >= 0.20)
    res["five_years_all_ge_20"] = len(last5) == 5 and len(ok) == 5 and res["years_ge_20"] == 5
    last3 = last5[-3:]
    res["last3_all_ge_20"] = len(last3) == 3 and all(h["status"] == "OK" and h["roce"] >= 0.20 for h in last3)
    # trend: least-squares slope over available OK years, pp per year
    if len(ok) >= 3:
        xs = list(range(len(ok)))
        ys = [h["roce"] * 100 for h in ok]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
        res["trend_slope_pp"] = slope
        res["trend"] = "上升" if slope > 1 else ("下降" if slope < -1 else "稳定")
    else:
        res["trend_slope_pp"], res["trend"] = None, "N/A"
    imp = [h for h in last5 if h["roce"] is not None]
    res["improving_3y"] = len(imp) >= 3 and imp[-1]["roce"] > imp[-2]["roce"] > imp[-3]["roce"]


def _flags(co: Company, res: dict, cap: dict, t: Optional[dict], adj: dict, end: str, asof: date) -> None:
    f = res["flags"]
    if res["status"].startswith("N/M:资本为零"):
        f.append("CE_NEGATIVE_OR_ZERO")
    if res["status"].startswith("N/M:资本基数接近零"):
        f.append("CE_NEAR_ZERO")
    ac, lc = co.inst("assets_cur", end), co.inst("liab_cur", end)
    if ac and lc and ac.val - lc.val < 0:
        f.append("NWC_NEGATIVE")
    ce_end = res.get("ce_end")
    gw = co.inst("goodwill", end)
    if gw and ce_end and ce_end > 0 and gw.val > C.GOODWILL_HEAVY_SHARE_OF_CE * ce_end:
        f.append("GOODWILL_HEAVY")
    eq = co.inst("equity", end)
    if eq and eq.val < 0:
        f.append("EQUITY_NEGATIVE")
    lease = co.inst("lease_liab", end)
    if lease and ce_end and ce_end > 0 and lease.val > C.LEASE_HEAVY_SHARE_OF_CE * ce_end:
        f.append("LEASE_HEAVY")
    ebit = t["value"] if t else None
    if ebit:
        imp = sum(v for k, v in adj["items"].items() if "impairment" in k)
        if imp > C.ONE_OFF_SHARE_OF_EBIT * abs(ebit):
            f.append("BIG_IMPAIRMENT")
        gain = -adj["items"].get("disposal_gain(removed)", 0.0)
        if gain > C.ONE_OFF_SHARE_OF_EBIT * abs(ebit):
            f.append("ONE_OFF_GAIN")
        if abs(adj["delta"]) > 0.20 * abs(ebit):
            f.append("LARGE_ADJUSTMENT")
    acq = ttm(co.s["acquisitions"], end) if co.s.get("acquisitions") else None
    if acq and cap.get("ok") and cap["avg"] > 0 and acq["value"] > C.ACQUISITION_SHARE_OF_CE * cap["avg"]:
        f.append("BIG_ACQUISITION")
    if cap.get("ok") and cap["begin"]["ce"] > 0 and cap["end"]["ce"] > C.CE_JUMP_RATIO * cap["begin"]["ce"]:
        f.append("CE_JUMP")
    limit = C.STALE_DAYS_ANNUAL_ONLY if res.get("annual_only") else C.STALE_DAYS
    # cross-check: operating income vs pre-tax + interest. Opposite signs with a
    # large gap usually means a tagging error in the filing (e.g. a loss tagged
    # as positive). Such companies are not ranked until checked by hand.
    if res.get("ebit_method") == "A" and t:
        tb = ttm(co.ebit_b, end)
        res["ebit_ttm_check_pretax_plus_interest"] = tb["value"] if tb else None
        if tb and t["value"] * tb["value"] < 0 and abs(t["value"] - tb["value"]) > 0.5 * abs(t["value"]):
            f.append("EBIT_CONFLICT")
            res["notes"].append("营业利润与（税前利润+利息）符号相反且差距大：可能是大额非经营损益，也可能是标签错误，需人工核对")
    # operating profit larger than revenue is impossible for a normal business:
    # treat as a tagging error and keep it out of the ranking
    if t and t["value"] > 0 and co.s.get("revenue"):
        tr = ttm(co.s["revenue"], end)
        res["revenue_ttm"] = tr["value"] if tr else None
        if tr and tr["value"] >= 0 and t["value"] > tr["value"]:
            f.append("EBIT_GT_REVENUE")
            res["notes"].append("TTM 营业利润大于营业收入，疑似申报标签错误，不参与排名")
    if (asof - d(end)).days > limit:
        f.append("STALE")
        last_assets = co.s["assets"].instants()[-1].end if co.s["assets"].instants() else None
        if last_assets and last_assets > end and not co.inst("liab_cur", last_assets):
            res["notes"].append(f"最新资产负债表（{last_assets}）未区分流动负债，只能显示 {end} 的旧数据，不参与排名")
        else:
            res["notes"].append(f"最新可计算期末 {end} 距今超过 {limit} 天，不参与排名")
    if res.get("annual_only"):
        f.append("NO_QUARTERLY_XBRL")
    if res.get("ttm_method") == "sum4Q":
        f.append("TTM_FROM_QUARTERS")
    if res.get("roce_ttm_prev") is not None and res.get("roce_ttm") is not None and res["roce_ttm"] - res["roce_ttm_prev"] <= -0.05:
        f.append("ROCE_DROP_QOQ")
    # cyclical peak needs industry data: phase 2


SEVERE = {"CE_NEGATIVE_OR_ZERO", "CE_NEAR_ZERO", "ONE_OFF_GAIN", "LARGE_ADJUSTMENT", "STALE", "EBIT_CONFLICT", "EBIT_GT_REVENUE"}
ONE_OFF_OR_BASE = {"ONE_OFF_GAIN", "LARGE_ADJUSTMENT", "CE_NEAR_ZERO", "CE_JUMP", "BIG_IMPAIRMENT", "EQUITY_NEGATIVE"}


def _grade(res: dict) -> None:
    r = res.get("roce_ttm")
    ok = res.get("status") == "OK"
    res["rank_eligible"] = ok and res["group"] == "general" and not ({"STALE", "EBIT_GT_REVENUE"} & set(res["flags"]))
    res["band"] = C.band(r) if ok else res.get("status", "N/A").split(":")[0]
    # stability grade (section 5)
    g = None
    if res.get("five_years_all_ge_20"):
        g = "A"
    elif res.get("last3_all_ge_20") and (res.get("trend_slope_pp") or 0) > 0:
        g = "B"
    elif ok and r is not None and r >= 0.30:
        g = "D" if (set(res["flags"]) & ONE_OFF_OR_BASE) else "C"
    res["stability_grade"] = g
    # data quality A/B/C
    q = "A"
    if res.get("ebit_method") == "B" or res.get("ttm_method") == "sum4Q" or res.get("annual_only") \
            or "EBIT_METHOD_MIXED_IN_HISTORY" in res["flags"]:
        q = "B"
    if res.get("years_available", 0) < 3 or "STALE" in res["flags"] or not ok:
        q = "C"
    res["data_quality"] = q
    # watchlist (module D)
    res["watchlist"] = bool(res["rank_eligible"] and r is not None and r >= 0.30
                            and res.get("years_ge_20", 0) >= 3 and not (set(res["flags"]) & SEVERE))


def _dedup_sources(cik: int, facts: List[Fact]) -> List[dict]:
    seen, out = set(), []
    for f in facts:
        key = (f.concept, f.start, f.end, f.accn)
        if key not in seen:
            seen.add(key)
            out.append(f.source(cik))
    return sorted(out, key=lambda s: (s["end"], s["concept"]))
