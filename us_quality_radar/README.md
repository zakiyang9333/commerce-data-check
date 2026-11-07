# US Quality Stocks Radar / 美股好公司雷达

Phase 1: stock universe, SEC EDGAR XBRL extraction, ROCE engine. Pure Python 3.9+, no dependencies.
Methodology (Chinese): [METHODOLOGY.md](METHODOLOGY.md).

```sh
# raw/ holds files downloaded unchanged from sec.gov:
#   company_tickers_exchange.json, companyfacts.zip, submissions.zip
python3 -m us_quality_radar.run_phase1 --raw raw --out out --asof 2026-11-07
python3 -m unittest us_quality_radar.tests.test_radar -v
```

Business-quality research only; not investment advice.
