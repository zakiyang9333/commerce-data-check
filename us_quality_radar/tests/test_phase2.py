"""Phase-2 tests on the fictional TestCo from test_radar (made-up numbers)."""
import unittest
from datetime import date

from us_quality_radar.roce import analyse
from us_quality_radar.phase2.classify import classify
from us_quality_radar.phase2.cycle import analyse_cycle, peak_and_quality
from us_quality_radar.tests.test_radar import testco, fact


def p1_rows(cf):
    r = analyse(1, cf, date(2026, 11, 7))
    cur = {k: ("" if v is None else str(v)) for k, v in r.items() if not isinstance(v, (list, dict))}
    cur["rank_eligible"] = str(r["rank_eligible"])
    ann = [{k: ("" if v is None else str(v)) for k, v in h.items() if not isinstance(v, dict)} for h in r["annual"]]
    return cur, ann


class TestClassify(unittest.TestCase):
    def test_sp500_wins(self):
        c = classify(1045810, ["NVDA"], 3674, "general")
        self.assertEqual(c["gics_sub_industry"], "Semiconductors")
        self.assertEqual(c["class_confidence"], "高")
        self.assertEqual(c["radar_sector"], "Semiconductors")
        self.assertEqual(c["radar_segment"], "GPU")

    def test_sic_fallback_and_unclassified(self):
        c = classify(999999999, ["ZZZZ"], 7372, "general")
        self.assertEqual(c["gics_industry"], "Software")
        self.assertEqual(c["gics_sub_industry"], "Unclassified")
        self.assertEqual(c["class_confidence"], "中低")
        u = classify(999999999, ["ZZZZ"], None, "general")
        self.assertEqual(u["gics_sector"], "Unclassified")
        self.assertEqual(u["cyclical_class"], "未知")

    def test_memory_strong_cyclical(self):
        self.assertEqual(classify(723125, ["MU"], 3674, "general")["cyclical_class"], "A")
        self.assertEqual(classify(999999999, ["BNK"], 6022, "financial")["cyclical_class"], "D")


class TestCycle(unittest.TestCase):
    def test_metrics_and_class(self):
        cf = testco()
        g = cf["facts"]["us-gaap"]
        g["Revenues"] = {"units": {"USD": [{**f, "val": f["val"] * 4} for f in g["OperatingIncomeLoss"]["units"]["USD"]]}}
        g["NetCashProvidedByUsedInOperatingActivities"] = {"units": {"USD": [{**f, "val": f["val"] * 0.9} for f in g["OperatingIncomeLoss"]["units"]["USD"]]}}
        cur, ann = p1_rows(cf)
        m = analyse_cycle(1, cf, cur, ann)
        self.assertAlmostEqual(m["roce_5y_avg"], sum([200/850, 250/950, 300/1050, 350/1150, 400/1250]) / 5)
        self.assertEqual(m["normalized_status"][:4], "数据不足")  # only 5 years
        self.assertAlmostEqual(m["ocf_to_ebit_5y"], 0.9)
        peak_and_quality(cur, "C", m)
        # current 36%, 5y avg 27.6%, median 28.6%, cash flow OK, no peak -> A
        self.assertEqual(m["quality_class"], "A")

    def test_normalized_needs_seven_years(self):
        cf = testco()
        g = cf["facts"]["us-gaap"]
        for y, v in ((2018, 100), (2019, 120), (2020, 150)):
            g["OperatingIncomeLoss"]["units"]["USD"].append(fact(v, f"{y}-12-31", f"{y}-01-01"))
        for y, v in ((2017, 700), (2018, 800), (2019, 900)):
            g["Assets"]["units"]["USD"].append(fact(v, f"{y}-12-31"))
            g["LiabilitiesCurrent"]["units"]["USD"].append(fact(100, f"{y}-12-31"))
        cur, ann = p1_rows(cf)
        m = analyse_cycle(1, cf, cur, ann)
        self.assertEqual(m["normalized_status"], "OK")
        ok = [h for h in m["history"] if h["status"] == "OK"]
        self.assertAlmostEqual(m["normalized_roce"], sum(h["ebit"] for h in ok) / sum(h["ce_avg"] for h in ok))


if __name__ == "__main__":
    unittest.main()
