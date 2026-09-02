import unittest
from datetime import datetime, time
from zoneinfo import ZoneInfo

from bot import (
    ScanResult,
    TelegramBot,
    ZeroPriceProduct,
    format_scan_result,
    parse_schedule_time,
    parse_target_chat_id,
    scan_zero_price_products,
)


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

    def test_target_message_can_be_sent_to_a_forum_topic(self):
        bot = TelegramBot("token", "https://example.com", None)
        calls = []
        bot.api_call = lambda method, data, **_kwargs: calls.append((method, data))

        bot.send_message(-1001234567890, "Report", message_thread_id=42)

        self.assertEqual(calls[0][0], "sendMessage")
        self.assertEqual(calls[0][1]["chat_id"], -1001234567890)
        self.assertEqual(calls[0][1]["message_thread_id"], 42)

    def test_scheduled_check_runs_once_per_moscow_day(self):
        bot = TelegramBot(
            "token",
            "https://example.com",
            None,
            target_chat_id=-1001234567890,
            target_message_thread_id=42,
            schedule_time=time(9, 0),
            timezone=ZoneInfo("Europe/Moscow"),
        )
        runs = []
        bot.run_check = lambda *args, **kwargs: runs.append((args, kwargs))
        now = datetime(2026, 9, 2, 9, 0, tzinfo=ZoneInfo("Europe/Moscow"))

        bot.run_scheduled_check_if_due(now)
        bot.run_scheduled_check_if_due(now.replace(hour=10))

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0][0], (-1001234567890, 42))
        self.assertIn("09:00", runs[0][1]["intro"])

    def test_schedule_does_not_run_before_time(self):
        bot = TelegramBot(
            "token",
            "https://example.com",
            None,
            target_chat_id=-1001234567890,
            schedule_time=time(9, 0),
            timezone=ZoneInfo("Europe/Moscow"),
        )
        runs = []
        bot.run_check = lambda *args, **kwargs: runs.append((args, kwargs))

        bot.run_scheduled_check_if_due(
            datetime(2026, 9, 2, 8, 59, tzinfo=ZoneInfo("Europe/Moscow"))
        )

        self.assertEqual(runs, [])

    def test_parses_target_and_schedule_settings(self):
        self.assertEqual(parse_target_chat_id("-1001234567890"), -1001234567890)
        self.assertEqual(parse_target_chat_id("@routesroads_channel"), "@routesroads_channel")
        self.assertEqual(parse_schedule_time("09:00"), time(9, 0))

    def test_ignores_blank_message(self):
        bot = TelegramBot("token", "https://example.com", None)
        bot.handle_message({"chat": {"id": 123}, "text": "   "})


if __name__ == "__main__":
    unittest.main()
