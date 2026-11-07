"""Phase 2 batch run. Reads a phase-1 run folder (read-only) and SEC
companyfacts; writes a new, separate output folder.

python3 -m us_quality_radar.phase2.run_phase2 --phase1 OUT1 --raw RAW --out OUT2
"""
import argparse, csv, hashlib, json, os, time, zipfile
from collections import defaultdict
from datetime import datetime, timezone
from statistics import mean, median

from .classify import classify, load_sp500
from .cycle import analyse_cycle, peak_and_quality
from .names_cn import NAME_CN

COLS = ["ticker", "name", "name_cn", "cik", "group", "radar_sector", "radar_sector_cn", "gics_sector", "gics_industry_group",
        "gics_industry", "gics_sub_industry", "radar_segment", "class_source", "class_confidence", "segment_source",
        "cyclical_class", "cyclical_label", "rank_eligible", "status", "period_end", "roce_ttm",
        "roce_3y_avg", "roce_5y_avg", "roce_5y_median", "roce_10y_avg", "roce_10y_median", "roce_10y_years", "roce_volatility_pp", "roce_volatility_cv", "roce_max_drawdown_pp", "roce_max_drawdown_rel", "high_volatility",
        "normalized_roce", "normalized_years", "normalized_status", "normalized_roce_margin",
        "revenue_ttm", "revenue_growth", "ebit_growth", "gross_margin_ttm", "gross_margin_10y_median", "ebit_margin_ttm",
        "ebit_margin_10y_median", "inventory_growth", "capex_growth", "capex_intensity_ttm", "capex_intensity_5y_median",
        "ocf_to_ebit_5y", "ocf_verified", "peak_signals", "peak_alert", "peak_dependence", "trend_5y_slope_pp",
        "quality_class", "quality_reason", "stability_grade_phase1", "years_ge_20", "flags_phase1", "notes"]


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase1", required=True)
    ap.add_argument("--raw", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    assert os.path.abspath(a.out) != os.path.abspath(a.phase1), "phase 2 must not write into the phase-1 folder"
    os.makedirs(os.path.join(a.out, "audit2"), exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    p1 = {fn: sha(os.path.join(a.phase1, fn)) for fn in ("roce_current.csv", "roce_annual.csv", "universe.csv")}
    cur = list(csv.DictReader(open(os.path.join(a.phase1, "roce_current.csv"))))
    ann = defaultdict(list)
    for r in csv.DictReader(open(os.path.join(a.phase1, "roce_annual.csv"))):
        ann[r["cik"]].append(r)
    uni = {r["cik"]: r for r in csv.DictReader(open(os.path.join(a.phase1, "universe.csv")))}
    z = zipfile.ZipFile(os.path.join(a.raw, "companyfacts.zip"))
    names = set(z.namelist())
    sp = load_sp500()
    rows, t0 = [], time.time()
    for i, r in enumerate(cur, 1):
        tickers = uni.get(r["cik"], {}).get("tickers", r["ticker"]).split() or [r["ticker"]]
        sic = int(r["sic"]) if (r.get("sic") or "").isdigit() else None
        c = classify(r["cik"], tickers, sic, r["group"], sp)
        row = {"ticker": r["ticker"], "name": r["name"], "name_cn": NAME_CN.get(r["ticker"], ""), "cik": r["cik"],
               "group": r["group"], "rank_eligible": r["rank_eligible"], "status": r["status"], "period_end": r["period_end"],
               "roce_ttm": r["roce_ttm"], "stability_grade_phase1": r["stability_grade"], "years_ge_20": r["years_ge_20"],
               "flags_phase1": r["flags"], **c}
        m = {}
        fn = f"CIK{int(r['cik']):010d}.json"
        if r["status"] == "OK" and fn in names:
            try:
                m = analyse_cycle(int(r["cik"]), json.loads(z.read(fn)), r, sorted(ann[r["cik"]], key=lambda x: x["fy_end"]))
                peak_and_quality(r, c["cyclical_class"], m)
            except Exception as ex:
                m = {"notes": [f"第二阶段计算错误 {type(ex).__name__}: {ex}"], "quality_class": "数据不足"}
        else:
            m = {"quality_class": "不适用", "quality_reason": "第一阶段 ROCE 不是有效值（N/A 或 N/M）"}
        if c["radar_segment"] in ("存储芯片",):
            m.setdefault("notes", []).append("DRAM/NAND 价格数据未接入（无授权数据源），周期判断只用财报中的毛利率、库存和资本开支")
        row.update({k: v for k, v in m.items() if k in COLS})
        row["peak_signals"] = " | ".join(f"{k}: {v}" for k, v in m.get("peak_signals", {}).items())
        row["notes"] = " ; ".join(m.get("notes", []))
        rows.append(row)
        audit = {**row, "phase1_inputs": p1, "history": m.get("history", []),
                 "sources": [{**s.__dict__, "url": f"https://www.sec.gov/Archives/edgar/data/{int(r['cik'])}/{s.accn.replace('-', '')}/"}
                             for s in m.get("sources", [])]}
        with open(os.path.join(a.out, "audit2", fn), "w") as f:
            json.dump(audit, f, ensure_ascii=False, indent=1, default=str)
        if i % 1000 == 0:
            print(f"{i}/{len(cur)} {time.time() - t0:.0f}s", flush=True)

    fmt = lambda v: "" if v is None else (f"{v:.6g}" if isinstance(v, float) else v)
    with open(os.path.join(a.out, "phase2_companies.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: fmt(r.get(k)) for k in COLS})

    # industry boards (rank-eligible general companies only)
    el = [r for r in rows if r["rank_eligible"] == "True"]
    def board(key):
        g = defaultdict(list)
        for r in el:
            g[r[key]].append(r)
        out = []
        for k, rs in g.items():
            v = [float(x["roce_ttm"]) for x in rs]
            out.append({"key": k, "n": len(rs), "mean": mean(v), "median": median(v),
                        "n_ge_30": sum(x >= 0.30 for x in v), "n_ge_40": sum(x >= 0.40 for x in v),
                        "n_5y_ge_20": sum(1 for x in rs if x["stability_grade_phase1"] == "A")})
        return sorted(out, key=lambda x: -x["median"])
    boards = {"sector": board("radar_sector"), "industry": board("gics_industry"), "segment": board("radar_segment")}
    json.dump(boards, open(os.path.join(a.out, "industry_boards.json"), "w"), ensure_ascii=False, indent=1)
    cnt = lambda f: sum(1 for r in rows if f(r))
    summary = {
        "run_utc": now, "phase1_folder": os.path.basename(os.path.abspath(a.phase1)), "phase1_sha256": p1,
        "companies": len(rows), "rank_eligible": len(el),
        "classified_sector": cnt(lambda r: r["gics_sector"] != "Unclassified"),
        "classified_sub_industry": cnt(lambda r: r["gics_sub_industry"] != "Unclassified"),
        "eligible_classified_sector": sum(1 for r in el if r["gics_sector"] != "Unclassified"),
        "eligible_classified_sub_industry": sum(1 for r in el if r["gics_sub_industry"] != "Unclassified"),
        "confidence": {k: cnt(lambda r, k=k: r["class_confidence"] == k) for k in ("高", "中", "中低", "低", "无")},
        "eligible_confidence": {k: sum(1 for r in el if r["class_confidence"] == k) for k in ("高", "中", "中低", "低", "无")},
        "quality_class": {k: cnt(lambda r, k=k: r.get("quality_class") == k) for k in ("A", "B", "C", "D", "E", "数据不足")},
        "peak_alert": cnt(lambda r: r.get("peak_alert")),
        "normalized_ok": sum(1 for r in el if r.get("normalized_status") == "OK"),
        "cyclical": {k: sum(1 for r in el if r["cyclical_class"] == k) for k in ("A", "B", "C", "D", "未知")},
        "elapsed_s": round(time.time() - t0),
    }
    json.dump(summary, open(os.path.join(a.out, "summary2.json"), "w"), ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
