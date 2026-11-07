"""Unit tests on a synthetic, clearly fictional company (TestCo).
Numbers are made up to check formulas; they are not market data."""
import unittest
from datetime import date

from us_quality_radar.roce import analyse, ttm
from us_quality_radar.xbrl import load_series
from us_quality_radar import universe as U


def fact(val, end, start=None, form="10-K", filed="2026-02-01", accn="0000000000-26-000001", fy=None, fp="FY"):
    r = {"val": val, "end": end, "form": form, "filed": filed, "accn": accn, "fy": fy, "fp": fp}
    if start:
        r["start"] = start
    return r


def cf(gaap):
    return {"cik": 1, "entityName": "TestCo", "facts": {"us-gaap": {k: {"units": {"USD": v}} for k, v in gaap.items()}}}


def testco(op=True, lc_q3=300):
    years = range(2021, 2026)
    opinc = [fact(v, f"{y}-12-31", f"{y}-01-01") for y, v in zip(years, [200, 250, 300, 350, 400])]
    opinc.append(fact(340, "2024-12-31", "2024-01-01", filed="2025-02-01", accn="old"))  # superseded
    opinc += [fact(280, "2025-09-30", "2025-01-01", form="10-Q", filed="2025-10-30", fp="Q3"),
              fact(330, "2026-09-30", "2026-01-01", form="10-Q", filed="2026-10-30", fp="Q3")]
    assets = [fact(v, f"{y}-12-31") for y, v in zip(range(2020, 2026), [1000, 1100, 1200, 1300, 1400, 1500])]
    assets += [fact(1450, "2025-09-30", form="10-Q"), fact(1600, "2026-09-30", form="10-Q", filed="2026-10-30")]
    lc = [fact(200, f"{y}-12-31") for y in range(2020, 2026)]
    lc += [fact(250, "2025-09-30", form="10-Q"), fact(lc_q3, "2026-09-30", form="10-Q", filed="2026-10-30")]
    g = {"Assets": assets, "LiabilitiesCurrent": lc}
    if op:
        g["OperatingIncomeLoss"] = opinc
    else:
        g["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"] = [
            {**f, "val": f["val"] - 10} for f in opinc]
        g["InterestExpense"] = [{**f, "val": 10} for f in opinc]
    return cf(g)


class TestROCE(unittest.TestCase):
    def setUp(self):
        self.r = analyse(1, testco(), date(2026, 11, 7))

    def test_ttm_formula(self):
        self.assertEqual(self.r["period_end"], "2026-09-30")
        self.assertEqual(self.r["ttm_method"], "YTD(9M)+FY-YTDprior")
        self.assertAlmostEqual(self.r["ebit_ttm"], 330 + 400 - 280)

    def test_average_capital(self):
        self.assertEqual(self.r["ce_begin_date"], "2025-09-30")
        self.assertAlmostEqual(self.r["ce_avg"], (1200 + 1300) / 2)
        self.assertAlmostEqual(self.r["roce_ttm"], 450 / 1250)
        self.assertEqual(self.r["status"], "OK")
        self.assertEqual(self.r["band"], "优秀")

    def test_ytd_matched_capital(self):
        self.assertAlmostEqual(self.r["roce_ytd"], 330 / ((1300 + 1300) / 2))
        self.assertAlmostEqual(self.r["roce_ytd_annualized"], self.r["roce_ytd"] * 365 / 273)

    def test_annual_and_restatement(self):
        a = {h["label"]: h for h in self.r["annual"]}
        self.assertAlmostEqual(a["FY2024"]["ebit"], 350)  # latest filed wins
        self.assertAlmostEqual(a["FY2025"]["roce"], 400 / 1250)
        self.assertAlmostEqual(a["FY2021"]["roce"], 200 / 850)
        self.assertEqual(self.r["years_ge_20"], 5)
        self.assertEqual(self.r["stability_grade"], "A")
        self.assertEqual(self.r["trend"], "上升")

    def test_sources_traceable(self):
        s = self.r["sources"]
        self.assertTrue(all(x["url"].startswith("https://www.sec.gov/Archives/edgar/data/1/") for x in s))
        self.assertNotIn("old", [x["accn"] for x in s])

    def test_negative_capital_is_nm(self):
        r = analyse(1, testco(lc_q3=1700), date(2026, 11, 7))
        self.assertTrue(r["status"].startswith("N/M"))
        self.assertIsNone(r["roce_ttm"])
        self.assertFalse(r["rank_eligible"])
        self.assertIn("CE_NEGATIVE_OR_ZERO", r["flags"])

    def test_near_zero_capital_not_ranked(self):
        c = testco(lc_q3=1560)  # CE end 40
        for x in c["facts"]["us-gaap"]["LiabilitiesCurrent"]["units"]["USD"]:
            if x["end"] == "2025-09-30":
                x["val"] = 1350  # CE begin 100 -> avg 70 < 10% of avg assets 1525
        r = analyse(1, c, date(2026, 11, 7))
        self.assertEqual(r["status"], "N/M:资本基数接近零")
        self.assertFalse(r["rank_eligible"])
        self.assertIn("CE_NEAR_ZERO", r["flags"])

    def test_method_b_fallback(self):
        r = analyse(1, testco(op=False), date(2026, 11, 7))
        self.assertEqual(r["ebit_method"], "B")
        self.assertIn("EBIT_FALLBACK", r["flags"])
        self.assertAlmostEqual(r["ebit_ttm"], 450)

    def test_missing_begin_capital_is_na(self):
        c = testco()
        c["facts"]["us-gaap"]["Assets"]["units"]["USD"] = [x for x in c["facts"]["us-gaap"]["Assets"]["units"]["USD"] if x["end"] != "2025-09-30"]
        r = analyse(1, c, date(2026, 11, 7))
        self.assertIsNone(r["roce_ttm"])
        self.assertTrue(r["status"].startswith("N/A"))

    def test_sum_of_quarters(self):
        q = [fact(v, e, s, form="10-Q") for v, s, e in [(10, "2025-10-01", "2025-12-31"), (20, "2026-01-01", "2026-03-31"),
                                                     (30, "2026-04-01", "2026-06-30"), (40, "2026-07-01", "2026-09-30")]]
        s = load_series(cf({"OperatingIncomeLoss": q}), "us-gaap", ["OperatingIncomeLoss"], "USD")
        t = ttm(s, "2026-09-30")
        self.assertEqual(t["method"], "sum4Q")
        self.assertEqual(t["value"], 100)


class TestUniverse(unittest.TestCase):
    def test_ticker_filter(self):
        self.assertTrue(U.is_common_ticker("BRK-B", "NYSE"))
        self.assertFalse(U.is_common_ticker("JPM-PC", "NYSE"))
        self.assertFalse(U.is_common_ticker("ABC-WT", "NYSE"))
        self.assertFalse(U.is_common_ticker("ABCDW", "Nasdaq"))
        self.assertTrue(U.is_common_ticker("GOOGL", "Nasdaq"))

    def test_groups_and_exclusions(self):
        tex = {"fields": ["cik", "name", "ticker", "exchange"], "data": [
            [1, "Op Co", "OPC", "Nasdaq"], [2, "Blank Check Acq", "BCAU", "Nasdaq"], [2, "Blank Check Acq", "BCA", "Nasdaq"],
            [3, "Some Bank", "BNK", "NYSE"], [4, "Some REIT", "RT", "NYSE"], [5, "OTC Co", "OTCC", "OTC"],
            [6, "Gold ETF Trust", "GLX", "NYSE"]]}
        subs = {1: {"sic": "7372", "entityType": "operating"}, 2: {"sic": "6770", "entityType": "operating"},
                3: {"sic": "6022", "entityType": "operating"}, 4: {"sic": "6798", "entityType": "operating"},
                5: {"sic": "7372"}, 6: {"sic": "6221", "entityType": "operating"}}
        u = {r["cik"]: r for r in U.build(tex, subs)}
        self.assertFalse(u[1]["excluded"])
        self.assertTrue(u[2]["excluded"])
        self.assertEqual(u[3]["group"], "financial")
        self.assertEqual(u[4]["group"], "reit")
        self.assertTrue(u[5]["excluded"])
        self.assertTrue(u[6]["excluded"])


if __name__ == "__main__":
    unittest.main()
