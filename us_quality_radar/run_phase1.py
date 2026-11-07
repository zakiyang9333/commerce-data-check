"""Phase 1 batch run: universe -> companyfacts -> ROCE -> audited outputs.

Usage:
  python3 -m us_quality_radar.run_phase1 --raw RAW_DIR --out OUT_DIR [--asof YYYY-MM-DD] [--limit N] [--cik 320193 ...]

RAW_DIR must hold (downloaded from sec.gov, unchanged):
  company_tickers_exchange.json
  companyfacts.zip  (or a companyfacts/ folder of CIK##########.json)
  submissions.zip   (or a submissions/ folder)

Outputs in OUT_DIR:
  universe.csv            every listed company, with group and exclusion reason
  roce_current.csv        one row per covered company (TTM / YTD / adjusted / 5Y)
  roce_annual.csv         one row per company x fiscal year (up to 10 years)
  audit/CIK##########.json  full inputs with filing accession + URL and calc steps
  summary.json, run_log.txt
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import time
import zipfile
from datetime import date, datetime, timezone

from . import universe as U
from .roce import analyse


class Source:
    def __init__(self, raw: str, name: str):
        self.zip = None
        z = os.path.join(raw, f"{name}.zip")
        self.dir = os.path.join(raw, name)
        if os.path.isdir(self.dir):
            pass
        elif os.path.exists(z):
            self.zip = zipfile.ZipFile(z)
            self.names = set(self.zip.namelist())
        else:
            raise SystemExit(f"missing {z} or {self.dir}/")

    def get(self, cik: int):
        fn = f"CIK{cik:010d}.json"
        try:
            if self.zip:
                if fn not in self.names:
                    return None
                return json.loads(self.zip.read(fn))
            p = os.path.join(self.dir, fn)
            return json.load(open(p)) if os.path.exists(p) else None
        except (ValueError, OSError):
            return None


CUR_COLS = ["ticker", "name", "cik", "exchange", "group", "sic", "sic_description", "currency",
            "period_end", "latest_form", "latest_filed", "latest_accn", "ebit_method", "ttm_method",
            "ebit_ttm", "ce_begin_date", "ce_begin", "ce_end", "ce_avg", "roce_ttm", "status", "band",
            "ebit_ttm_adj", "roce_ttm_adj", "roce_ttm_ex_goodwill",
            "ytd_kind", "ytd_start", "ebit_ytd", "roce_ytd", "roce_ytd_annualized",
            "prev_period_end", "roce_ttm_prev",
            "years_available", "roce_5y_avg", "years_ge_20", "trend", "trend_slope_pp", "improving_3y",
            "stability_grade", "data_quality", "watchlist", "rank_eligible", "flags", "data_updated_utc"]
ANN_COLS = ["ticker", "cik", "label", "fy_end", "ebit", "ebit_method", "ebit_adj", "ce_begin", "ce_end",
            "ce_avg", "roce", "roce_adj", "roce_yoy_pp", "status", "form", "filed", "accn"]


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--asof", default=date.today().isoformat())
    ap.add_argument("--limit", type=int)
    ap.add_argument("--cik", type=int, nargs="*")
    a = ap.parse_args(argv)
    asof = date.fromisoformat(a.asof)
    os.makedirs(os.path.join(a.out, "audit"), exist_ok=True)
    log = open(os.path.join(a.out, "run_log.txt"), "a")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def L(msg):
        line = f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}"
        print(line, flush=True)
        log.write(line + "\n")

    L(f"=== run {now} asof {asof} ===")
    for fn in ("company_tickers_exchange.json", "companyfacts.zip", "submissions.zip"):
        p = os.path.join(a.raw, fn)
        if os.path.exists(p):
            st = os.stat(p)
            L(f"input {fn} bytes={st.st_size} mtime={datetime.fromtimestamp(st.st_mtime, timezone.utc):%Y-%m-%dT%H:%MZ}"
              + (f" sha256={sha256(p)}" if st.st_size < 50_000_000 else ""))
    tex = json.load(open(os.path.join(a.raw, "company_tickers_exchange.json")))
    subs_src, facts_src = Source(a.raw, "submissions"), Source(a.raw, "companyfacts")
    ciks = sorted({int(r[0]) for r in tex["data"]})
    subs = {c: subs_src.get(c) for c in ciks}
    uni = U.build(tex, {c: s for c, s in subs.items() if s})
    with open(os.path.join(a.out, "universe.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(uni[0].keys()))
        w.writeheader()
        w.writerows(uni)
    inc = [u for u in uni if not u["excluded"]]
    L(f"universe: {len(uni)} CIKs, included {len(inc)}, excluded {len(uni) - len(inc)}")
    if a.cik:
        inc = [u for u in inc if u["cik"] in set(a.cik)]
    if a.limit:
        inc = inc[: a.limit]

    cur_rows, ann_rows, counts = [], [], {}
    t0 = time.time()
    for i, u in enumerate(inc, 1):
        cf = facts_src.get(u["cik"])
        if cf is None:
            r = {"cik": u["cik"], "status": "N/A:无 companyfacts（未提交 XBRL）", "flags": [], "group": u["group"], "rank_eligible": False}
        else:
            try:
                r = analyse(u["cik"], cf, asof, group=u["group"])
            except Exception as ex:  # keep the batch alive, record the failure
                r = {"cik": u["cik"], "status": f"N/A:计算错误 {type(ex).__name__}: {ex}", "flags": [], "group": u["group"], "rank_eligible": False}
        r.update(ticker=u["ticker"], name=u["name"], exchange=u["exchange"], sic=u["sic"],
                 sic_description=u["sic_description"], data_updated_utc=now)
        st = r["status"].split(":")[0]
        counts[st] = counts.get(st, 0) + 1
        row = {k: r.get(k) for k in CUR_COLS}
        row["flags"] = " ".join(r.get("flags", []))
        cur_rows.append(row)
        for h in r.get("annual", []):
            ann_rows.append({"ticker": u["ticker"], "cik": u["cik"], **{k: h.get(k) for k in ANN_COLS if k not in ("ticker", "cik")}})
        with open(os.path.join(a.out, "audit", f"CIK{u['cik']:010d}.json"), "w") as f:
            json.dump(r, f, ensure_ascii=False, indent=1, default=str)
        if i % 250 == 0:
            L(f"{i}/{len(inc)} done, {time.time() - t0:.0f}s")

    cur_rows.sort(key=lambda r: (not r["rank_eligible"], -(r["roce_ttm"] or -9e9)))
    for name, rows, cols in (("roce_current.csv", cur_rows, CUR_COLS), ("roce_annual.csv", ann_rows, ANN_COLS)):
        with open(os.path.join(a.out, name), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
    elig = [r for r in cur_rows if r["rank_eligible"]]
    summary = {
        "run_utc": now, "asof": asof.isoformat(), "universe_total": len(uni), "covered": len(inc),
        "status_counts": counts, "rank_eligible_general": len(elig),
        "roce_ge_40": sum(1 for r in elig if r["roce_ttm"] >= 0.40),
        "roce_30_40": sum(1 for r in elig if 0.30 <= r["roce_ttm"] < 0.40),
        "five_years_ge_20": sum(1 for r in cur_rows if r["stability_grade"] == "A"),
        "watchlist": sum(1 for r in cur_rows if r["watchlist"]),
        "groups": {g: sum(1 for u in uni if not u["excluded"] and u["group"] == g) for g in ("general", "financial", "reit")},
    }
    json.dump(summary, open(os.path.join(a.out, "summary.json"), "w"), ensure_ascii=False, indent=1)
    L(f"done in {time.time() - t0:.0f}s: {json.dumps(summary, ensure_ascii=False)}")


if __name__ == "__main__":
    sys.exit(main())
