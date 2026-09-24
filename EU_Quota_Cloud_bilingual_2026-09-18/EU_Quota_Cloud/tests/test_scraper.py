import unittest
from types import SimpleNamespace
from unittest.mock import patch

import scraper


class ScraperMetadataTest(unittest.TestCase):
    def test_eu_update_date_with_hyphen(self):
        self.assertEqual(
            scraper.parse_tariff_quota_update("Last tariff quota update: 09-09-2026"),
            "2026-09-09",
        )

    def test_eu_update_date_with_spacing_and_slash(self):
        self.assertEqual(
            scraper.parse_tariff_quota_update("LAST   TARIFF QUOTA UPDATE ： 09/09/2026"),
            "2026-09-09",
        )

    def test_missing_eu_update_date(self):
        self.assertIsNone(scraper.parse_tariff_quota_update("Quota consultation"))

    def test_fetches_update_date_from_consultation_page(self):
        calls = []

        class Response:
            text = "<html>Last tariff quota update: 09-09-2026</html>"

            def raise_for_status(self):
                return None

        fake_requests = SimpleNamespace(get=lambda url, **kwargs: calls.append(url) or Response())
        fake_soup = lambda html, parser: SimpleNamespace(
            get_text=lambda **kwargs: "Last tariff quota update: 09-09-2026"
        )
        with patch.object(scraper, "requests", fake_requests), patch.object(scraper, "BeautifulSoup", fake_soup):
            self.assertEqual(scraper.fetch_tariff_quota_update(), "2026-09-09")
        self.assertEqual(calls, [scraper.CONSULTATION_URL])


PAGE = """Order number
099802
Validity period
01-10-2026  -  31-12-2026
Origin
Japan
Initial amount
100000  Kilogram
Amount
120000  Kilogram
Balance
60000  Kilogram
Transferred Amount
{transferred}
Closed for drawing requests
No
Exhaustion date

Critical
No
Total awaiting allocation (indicative)
6000
Blocking period
"""


class ScraperCarryOverTest(unittest.TestCase):
    def test_transferred_amount_is_the_rate_denominator(self):
        r = scraper.parse_quota_detail(PAGE.format(transferred="20000  Kilogram"), "099802")
        self.assertEqual(r["quarterly_kg"], 100000)
        self.assertEqual(r["amount_kg"], 120000)
        self.assertEqual(r["transferred_kg"], 20000)
        self.assertAlmostEqual(r["remaining_rate"], 54000 / 120000)
        self.assertAlmostEqual(r["consumption_rate"], 1 - 54000 / 120000)

    def test_blank_transferred_amount_means_zero(self):
        r = scraper.parse_quota_detail(PAGE.format(transferred=""), "099802")
        self.assertEqual(r["transferred_kg"], 0)
        self.assertEqual(r["amount_kg"], 120000)


if __name__ == "__main__":
    unittest.main()
