"""Industry classification with source and confidence for every company.

Order of evidence:
1. S&P 500 constituents list (public GICS Sector + Sub-Industry; GitHub
   datasets/s-and-p-500-companies, mirrored from Wikipedia)  -> 高
2. SEC SIC code -> our GICS crosswalk                           -> 中 / 中低 / 低
3. Neither -> Unclassified (never guessed from the company name).
Radar segments (GPU, SaaS, ...) come from segments.py, labelled separately.
"""
import csv
import os

from .gics_tree import SUB, IND, TREE, SECTOR_CN
from .sic_crosswalk import SIC
from . import segments as SG
from .cyclical import cyclical_class, LABEL

HERE = os.path.dirname(__file__)
SP_SOURCE = "S&P 500 公开 GICS（datasets/s-and-p-500-companies @05c998c，源自 Wikipedia）"


def load_sp500(path=os.path.join(HERE, "data", "sp500_gics.csv")):
    by_cik, by_tk = {}, {}
    with open(path) as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        rec = (r["GICS Sector"], r["GICS Sub-Industry"])
        if r.get("CIK"):
            by_cik[int(r["CIK"])] = rec
        by_tk[r["Symbol"].replace(".", "-")] = rec
    return by_cik, by_tk


def _levels_from_sub(sub):
    sec, grp, ind = SUB[sub]
    return sec, grp, ind, sub


def classify(cik, tickers, sic, phase1_group, sp=None):
    sp = sp or load_sp500()
    by_cik, by_tk = sp
    out = {"gics_sector": "Unclassified", "gics_industry_group": "Unclassified", "gics_industry": "Unclassified",
           "gics_sub_industry": "Unclassified", "class_source": "", "class_confidence": "无"}
    hit = by_cik.get(int(cik)) or next((by_tk[t] for t in tickers if t in by_tk), None)
    if hit and hit[1] in SUB:
        s, g, i, su = _levels_from_sub(hit[1])
        out.update(gics_sector=s, gics_industry_group=g, gics_industry=i, gics_sub_industry=su,
                   class_source=SP_SOURCE, class_confidence="高")
    elif sic is not None and sic in SIC:
        v = SIC[sic]
        src = f"SEC SIC {sic} → GICS 对照表（本项目自建）"
        if v in SUB:
            s, g, i, su = _levels_from_sub(v)
            out.update(gics_sector=s, gics_industry_group=g, gics_industry=i, gics_sub_industry=su,
                       class_source=src, class_confidence="中")
        elif v.startswith("I:"):
            i = v[2:]
            s, g = IND[i]
            out.update(gics_sector=s, gics_industry_group=g, gics_industry=i, class_source=src, class_confidence="中低")
        elif v.startswith("S:"):
            out.update(gics_sector=v[2:], class_source=src, class_confidence="低")
    # radar segment (spec's special sub-segments)
    seg = next((SG.TICKER_SEG[t] for t in tickers if t in SG.TICKER_SEG), "")
    seg_src = SG.SOURCE if seg else ""
    if not seg:
        if out["gics_industry"] == "Semiconductors & Semiconductor Equipment":
            seg = {"Semiconductors": "半导体（未细分）", "Semiconductor Materials & Equipment": "半导体设备与材料（未细分）"}.get(
                out["gics_sub_industry"], "半导体（未细分）")
        elif out["gics_industry"] == "Software":
            seg = "软件（未细分）"
    out["radar_segment"], out["segment_source"] = seg, seg_src
    # radar sector: GICS sector, but semiconductors shown on their own (spec)
    is_semi = out["gics_industry"] == "Semiconductors & Semiconductor Equipment" or seg in SG.SEMI_SEGMENTS
    out["radar_sector"] = "Semiconductors" if is_semi else out["gics_sector"]
    out["radar_sector_cn"] = SECTOR_CN.get(out["radar_sector"], out["radar_sector"])
    cc = cyclical_class(out["gics_sector"], out["gics_industry_group"], out["gics_industry"],
                        out["gics_sub_industry"], seg, phase1_group)
    out["cyclical_class"], out["cyclical_label"] = cc, LABEL[cc]
    return out
