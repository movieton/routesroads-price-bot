import unittest

from bot import ScanResult, ZeroPriceProduct, format_scan_result, scan_zero_price_products


class ScannerTests(unittest.TestCase):
    def test_scans_all_pages_and_finds_zero_variant(self):
        pages = [
            {
                "products": [
                    {
                        "title": "Paid product",
                        "handle": "paid",
                        "variants": [{"title": "Default Title", "price": "10.00"}],
                    },
                    {
                        "title": "Mixed product",
                        "handle": "mixed",
                        "variants": [
                            {"title": "Small", "price": "0.00"},
                            {"title": "Large", "price": "20.00"},
                        ],
                    },
                ]
            },
            {
                "products": [
                    {
                        "title": "Free product",
                        "handle": "free-product",
                        "variants": [{"title": "Default Title", "price": "0"}],
                    }
                ]
            },
        ]

        def fake_fetcher(_url):
            return pages.pop(0)

        result = scan_zero_price_products(
            "https://example.com", fetcher=fake_fetcher, page_size=2
        )

        self.assertEqual(result.checked_products, 3)
        self.assertEqual(len(result.zero_price_products), 2)
        self.assertEqual(result.zero_price_products[0].variants, ("Small",))
        self.assertEqual(result.zero_price_products[1].variants, ())
        self.assertEqual(
            result.zero_price_products[1].url,
            "https://example.com/products/free-product",
        )

    def test_formats_empty_result(self):
        message = format_scan_result(ScanResult(7, ()))[0]
        self.assertIn("не найдено", message)
        self.assertIn("7", message)

    def test_escapes_telegram_html(self):
        result = ScanResult(
            1,
            (ZeroPriceProduct("A & B <test>", "https://example.com/a", ("S & M",)),),
        )
        message = format_scan_result(result)[0]
        self.assertIn("A &amp; B &lt;test&gt;", message)
        self.assertIn("S &amp; M", message)


if __name__ == "__main__":
    unittest.main()
