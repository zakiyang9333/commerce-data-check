"""Cycle metrics, cycle-peak signals, Normalized ROCE and the A-E quality
classes. Annual ROCE comes from phase-1 output (unchanged); the extra series
(revenue, gross profit, inventory, capex, operating cash flow) are read from
SEC companyfacts with the same phase-1 extraction rules, and every value
keeps its accession number."""
from datetime import timedelta
from statistics import mean, median, pstdev

from ..roce import Company, ttm
from ..xbrl import d, load_series

EXTRA = {
    "us-gaap": {
        "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                    "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"],
        "cogs": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"],
        "gross_profit": ["GrossProfit"],
        "inventory": ["InventoryNet"],
        "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
        "ocf": ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    },
    "ifrs-full": {
        "revenue": ["Revenue", "RevenueFromContractsWithCustomers"],
        "cogs": ["CostOfSales"],
        "gross_profit": ["GrossProfit"],
        "inventory": ["Inventories"],
        "capex": ["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"],
        "ocf": ["CashFlowsFromUsedInOperatingActivities"],
    },
}
YEAR = timedelta(days=365)
MIN_YEARS_NORMALIZED = 7

# thresholds (our own, documented in METHODOLOGY_PHASE2.md)
PK_VS_5Y_PP, PK_VS_5Y_X = 10.0, 1.3
PK_VS_10Y_PP, PK_VS_10Y_X = 10.0, 1.5
PK_EBIT_VS_REV_PP, PK_EBIT_MIN = 30.0, 0.30
PK_INV_GROWTH, PK_INV_VS_REV_PP = 0.25, 10.0
PK_CAPEX_GROWTH, PK_CAPEX_INTENSITY_X = 0.50, 1.5
PK_GM_PP = 8.0
DRAWDOWN_REL, DRAWDOWN_PP = 0.50, 15.0   # boom/bust: ROCE fell >=50% from an earlier peak and >=15pp
OCF_TO_EBIT_MIN = 0.70


def _fy_value(series, start, end):
    f = series.get(start, end) if series else None
    return f


def _ttm_at(series, end):
    return ttm(series, end) if series and len(series) else None


def _year_ago_end(series, end):
    target = d(end) - YEAR
    ends = sorted({f.end for f in series.durations()}, key=lambda e: abs((d(e) - target).days)) if series else []
    return ends[0] if ends and abs((d(ends[0]) - target).days) <= 10 else None


def analyse_cycle(cik, cf, cur, annual):
    """cur: phase-1 current row (dict of strings); annual: phase-1 annual rows for this company."""
    co = Company(cik, cf)
    out = {"sources": [], "notes": []}
    tax, unit = co.taxonomy, co.unit
    S = {k: load_series(cf, tax, v, unit) for k, v in EXTRA.get(tax, {}).items()} if tax and unit else {}
    end = cur.get("period_end") or None
    method = cur.get("ebit_method") or "A"

    # ---------------- annual history (phase-1 ROCE + extra series)
    hist = []
    for a in annual:
        fe = a["fy_end"]
        fy_ebit = next((f for f in co.ebit_series(a["ebit_method"]).durations("FY") if f.end == fe), None)
        start = fy_ebit.start if fy_ebit else None
        row = {"fy_end": fe, "label": a["label"], "roce": float(a["roce"]) if a["roce"] not in ("", None) else None,
               "status": a["status"], "ebit": float(a["ebit"]) if a["ebit"] else None,
               "ce_avg": float(a["ce_avg"]) if a["ce_avg"] else None, "accn": a["accn"]}
        for k in ("revenue", "cogs", "gross_profit", "capex", "ocf"):
            f = _fy_value(S.get(k), start, fe) if start else None
            row[k] = f.val if f else None
            if f:
                out["sources"].append(f)
        if row["gross_profit"] is None and row["revenue"] is not None and row["cogs"] is not None:
            row["gross_profit"] = row["revenue"] - row["cogs"]
        inv = S.get("inventory").get(None, fe) if S.get("inventory") else None
        row["inventory"] = inv.val if inv else None
        if inv:
            out["sources"].append(inv)
        hist.append(row)
    out["history"] = hist
    ok = [h for h in hist if h["status"] == "OK" and h["roce"] is not None]
    r = [h["roce"] for h in ok]
    out["n_years_ok"] = len(r)
    out["roce_3y_avg"] = mean(r[-3:]) if len(r) >= 3 else None
    out["roce_5y_avg"] = mean(r[-5:]) if len(r) >= 5 else None
    out["roce_5y_median"] = median(r[-5:]) if len(r) >= 5 else None
    out["roce_10y_avg"] = mean(r[-10:]) if len(r) >= 8 else None
    out["roce_10y_median"] = median(r[-10:]) if len(r) >= 8 else None
    out["roce_volatility_pp"] = pstdev([x * 100 for x in r[-10:]]) if len(r) >= 5 else None
    out["roce_10y_years"] = len(r[-10:])
    mu = mean(r[-10:]) if len(r) >= 5 else None
    out["roce_volatility_cv"] = (out["roce_volatility_pp"] / 100 / mu) if (mu and mu > 0) else None
    # max drawdown of annual ROCE over 10y: separates boom/bust from steady upward drift
    dd_rel, dd_pp, peak = 0.0, 0.0, None
    for x in r[-10:]:
        peak = x if peak is None or x > peak else peak
        if peak and peak > 0:
            dd_pp = max(dd_pp, (peak - x) * 100)
            dd_rel = max(dd_rel, (peak - x) / peak)
    out["roce_max_drawdown_pp"] = dd_pp if len(r) >= 5 else None
    out["roce_max_drawdown_rel"] = dd_rel if len(r) >= 5 else None

    # ---------------- normalized ROCE (full-cycle, capital weighted)
    cyc = [h for h in ok[-10:] if h["ce_avg"] and h["ce_avg"] > 0 and h["ebit"] is not None]
    if len(cyc) >= MIN_YEARS_NORMALIZED:
        out["normalized_roce"] = sum(h["ebit"] for h in cyc) / sum(h["ce_avg"] for h in cyc)
        out["normalized_years"] = f'{cyc[0]["label"]}–{cyc[-1]["label"]}（{len(cyc)} 年）'
        out["normalized_status"] = "OK"
    else:
        out["normalized_roce"], out["normalized_years"] = None, f"{len(cyc)} 年"
        out["normalized_status"] = f"数据不足（完整财年 {len(cyc)} < {MIN_YEARS_NORMALIZED}）"
    margins = [h["ebit"] / h["revenue"] for h in cyc if h["revenue"] and h["revenue"] > 0]
    gms = [h["gross_profit"] / h["revenue"] for h in ok[-10:] if h["gross_profit"] is not None and h["revenue"] and h["revenue"] > 0]

    # ---------------- TTM operating data and year-ago comparison
    sig = {}
    if end:
        E = d(end)
        ebit_s = co.ebit_series(method)
        t_rev = _ttm_at(S.get("revenue"), end)
        ya = _year_ago_end(ebit_s, end)
        t_ebit = _ttm_at(ebit_s, end)
        p_ebit = _ttm_at(ebit_s, ya) if ya else None
        p_rev = _ttm_at(S.get("revenue"), ya) if ya else None
        t_gp = _ttm_at(S.get("gross_profit"), end)
        if t_gp is None and t_rev and S.get("cogs"):
            tc = _ttm_at(S["cogs"], end)
            t_gp = {"value": t_rev["value"] - tc["value"], "parts": tc["parts"]} if tc else None
        t_capex = _ttm_at(S.get("capex"), end)
        p_capex = _ttm_at(S.get("capex"), ya) if ya else None
        inv_now = S["inventory"].instant_near(E, 0) if S.get("inventory") else None
        inv_ago = S["inventory"].instant_near(E - YEAR, 10) if S.get("inventory") else None
        for x in (t_rev, p_rev, t_gp, t_capex, p_capex, p_ebit):
            if x:
                out["sources"] += x.get("parts", [])
        out["sources"] += [x for x in (inv_now, inv_ago) if x]
        g = lambda a, b: (a["value"] / b["value"] - 1) if (a and b and b["value"] > 0) else None
        out.update(
            year_ago_end=ya,
            revenue_ttm=t_rev["value"] if t_rev else None,
            revenue_growth=g(t_rev, p_rev),
            ebit_growth=g(t_ebit, p_ebit) if (p_ebit and p_ebit["value"] > 0) else None,
            gross_margin_ttm=(t_gp["value"] / t_rev["value"]) if (t_gp and t_rev and t_rev["value"] > 0) else None,
            gross_margin_10y_median=median(gms) if len(gms) >= 5 else None,
            ebit_margin_ttm=(t_ebit["value"] / t_rev["value"]) if (t_ebit and t_rev and t_rev["value"] > 0) else None,
            ebit_margin_10y_median=median(margins) if len(margins) >= 5 else None,
            inventory=inv_now.val if inv_now else None,
            inventory_growth=(inv_now.val / inv_ago.val - 1) if (inv_now and inv_ago and inv_ago.val > 0) else None,
            capex_ttm=t_capex["value"] if t_capex else None,
            capex_growth=g(t_capex, p_capex),
        )
        cap_int = [h["capex"] / h["revenue"] for h in ok[-6:-1] if h["capex"] is not None and h["revenue"] and h["revenue"] > 0]
        out["capex_intensity_ttm"] = (t_capex["value"] / t_rev["value"]) if (t_capex and t_rev and t_rev["value"] > 0) else None
        out["capex_intensity_5y_median"] = median(cap_int) if len(cap_int) >= 3 else None
        # margin-based normalized ROCE (cross-check)
        ce_avg = float(cur["ce_avg"]) if cur.get("ce_avg") else None
        if len(margins) >= MIN_YEARS_NORMALIZED and t_rev and ce_avg and ce_avg > 0:
            out["normalized_roce_margin"] = median(margins) * t_rev["value"] / ce_avg
        else:
            out["normalized_roce_margin"] = None

        # ---------------- peak signals
        c = float(cur["roce_ttm"]) if cur.get("roce_ttm") not in ("", None) else None
        if c is not None and out["roce_5y_avg"] is not None and out["roce_5y_avg"] > 0:
            if (c - out["roce_5y_avg"]) * 100 >= PK_VS_5Y_PP and c >= PK_VS_5Y_X * out["roce_5y_avg"]:
                sig["S1_高于5年均值"] = f'当前 {c:.1%} vs 5 年均值 {out["roce_5y_avg"]:.1%}'
        if c is not None and out["roce_10y_median"] is not None and out["roce_10y_median"] > 0:
            if (c - out["roce_10y_median"]) * 100 >= PK_VS_10Y_PP and c >= PK_VS_10Y_X * out["roce_10y_median"]:
                sig["S2_高于10年中位数"] = f'当前 {c:.1%} vs 10 年中位数 {out["roce_10y_median"]:.1%}'
        eg, rg = out["ebit_growth"], out["revenue_growth"]
        if eg is not None and rg is not None and eg >= PK_EBIT_MIN and (eg - rg) * 100 >= PK_EBIT_VS_REV_PP:
            sig["S3_利润增速远超收入"] = f"EBIT 同比 {eg:+.0%}，收入同比 {rg:+.0%}"
        ig = out["inventory_growth"]
        if ig is not None and ig >= PK_INV_GROWTH and (rg is None or (ig - rg) * 100 >= PK_INV_VS_REV_PP):
            sig["S4_库存明显增加"] = f"库存同比 {ig:+.0%}" + (f"，收入同比 {rg:+.0%}" if rg is not None else "")
        cg, ci, cm = out["capex_growth"], out["capex_intensity_ttm"], out["capex_intensity_5y_median"]
        if (cg is not None and cg >= PK_CAPEX_GROWTH) or (ci is not None and cm and ci >= PK_CAPEX_INTENSITY_X * cm):
            sig["S5_资本开支扩张"] = (f"资本开支同比 {cg:+.0%}" if cg is not None else "") + (
                f"；资本开支/收入 {ci:.1%} vs 5 年中位数 {cm:.1%}" if (ci is not None and cm) else "")
        gm, gmm = out["gross_margin_ttm"], out["gross_margin_10y_median"]
        if gm is not None and gmm is not None and (gm - gmm) * 100 >= PK_GM_PP:
            sig["S6_毛利率高于历史"] = f"毛利率 {gm:.1%} vs 10 年中位数 {gmm:.1%}"
    out["peak_signals"] = sig

    # ---------------- cash-flow check of profit quality (5 FY)
    last5 = ok[-5:]
    if len(last5) == 5 and all(h["ocf"] is not None for h in last5):
        se = sum(h["ebit"] for h in last5)
        out["ocf_to_ebit_5y"] = sum(h["ocf"] for h in last5) / se if se > 0 else None
    else:
        out["ocf_to_ebit_5y"] = None
    return out


def peak_and_quality(cur, cyc_class, m):
    """Adds peak_alert and quality_class to metrics m."""
    c = float(cur["roce_ttm"]) if cur.get("roce_ttm") not in ("", None) else None
    n_sig = len(m["peak_signals"])
    rel = {"S1_高于5年均值", "S2_高于10年中位数"} & set(m["peak_signals"])
    if (cyc_class in ("A", "B") and n_sig >= 2 and rel) or n_sig >= 3:
        m["peak_alert"] = "可能处于周期高峰"
    else:
        m["peak_alert"] = ""
    norm = m.get("normalized_roce")
    vol = m.get("roce_volatility_pp")
    avg5, med5 = m.get("roce_5y_avg"), m.get("roce_5y_median")
    # peak dependence demotes quality only for cyclical industries (A/B); for weak-cyclical
    # businesses the alert is still shown, but a jump in returns there is not a commodity cycle
    peak_dep = (cyc_class in ("A", "B") and bool(m["peak_alert"])) or \
               (cyc_class == "A" and (norm is None or (c is not None and c > 1.5 * norm)))
    cv = m.get("roce_volatility_cv")
    ddr, ddp = m.get("roce_max_drawdown_rel"), m.get("roce_max_drawdown_pp")
    high_vol = ddr is not None and ddr >= DRAWDOWN_REL and ddp >= DRAWDOWN_PP
    m["high_volatility"] = high_vol
    m["peak_dependence"] = peak_dep
    ocf_ok = m.get("ocf_to_ebit_5y") is not None and m["ocf_to_ebit_5y"] >= OCF_TO_EBIT_MIN
    m["ocf_verified"] = "通过" if ocf_ok else ("未通过" if m.get("ocf_to_ebit_5y") is not None else "N/A（现金流数据不全）")
    hist = [h for h in m["history"] if h["status"] == "OK" and h["roce"] is not None][-5:]
    q, why = "", ""
    if cur.get("rank_eligible") != "True":
        q, why = "不适用", "不在普通企业排名范围（金融/REIT、N/A、N/M 或过期）"
    elif c is None:
        q, why = "不适用", "无当前 ROCE"
    elif c >= 0.30:
        if m["n_years_ok"] < 3:
            q, why = "数据不足", "完整财年 ROCE 少于 3 年"
        elif peak_dep or high_vol:
            q, why = "C", "当前 ≥30%，但" + ("存在周期高峰依赖" if peak_dep else f"历史波动大（10 年内 ROCE 曾从高点回落 {ddp:.0f}pp，即 {ddr:.0%}）")
        elif avg5 is not None and med5 is not None and avg5 >= 0.25 and med5 >= 0.20 and ocf_ok:
            q, why = "A", "当前 ≥30%，5 年均值 ≥25%，中位数 ≥20%，无周期高峰依赖，经营现金流验证通过"
        elif avg5 is not None and avg5 >= 0.20:
            q, why = "B", "当前 ≥30%，5 年均值 ≥20%" + ("" if ocf_ok else f"（现金流验证：{m['ocf_verified']}）")
        else:
            q, why = "C", "当前 ≥30%，但 5 年均值不足 20%，高回报尚未持续"
    else:
        if len(hist) >= 4:
            xs = list(range(len(hist)))
            ys = [h["roce"] * 100 for h in hist]
            mx, my = mean(xs), mean(ys)
            slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
            downs = sum(1 for a, b in zip(ys, ys[1:]) if b < a)
            ups = sum(1 for a, b in zip(ys, ys[1:]) if b > a)
            m["trend_5y_slope_pp"] = slope
            hist10 = [h["roce"] for h in m["history"] if h["status"] == "OK" and h["roce"] is not None]
            early_high = (max(hist10[:-2]) if len(hist10) > 2 else 0) >= 0.20 or (m.get("roce_10y_avg") or 0) >= 0.20
            if slope >= 2 and downs <= 1 and ys[-1] - ys[0] >= 5 and c >= 0.10:
                q, why = "D", f"近 {len(hist)} 年 ROCE 斜率 +{slope:.1f}pp/年，{ups} 次上升"
            elif early_high and slope <= -2 and ys[-1] < ys[-2] < ys[-3] and c * 100 < (avg5 or 0) * 100 - 5:
                q, why = "E", f"历史曾 ≥20%，近 {len(hist)} 年斜率 {slope:.1f}pp/年且连续下降"
    m["quality_class"], m["quality_reason"] = q, why
    return m
