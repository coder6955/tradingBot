import unittest

from app.config import settings
from app.services.account_funds_service import AccountFundsService


class CountingMarginsProvider:
    def __init__(self) -> None:
        self.calls = 0

    def margins(self):
        self.calls += 1
        return {"equity": {"available": {"cash": 10000}}}


class AccountFundsServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        AccountFundsService._cached_margins = None
        AccountFundsService._cached_margins_at = None

    def tearDown(self) -> None:
        AccountFundsService._cached_margins = None
        AccountFundsService._cached_margins_at = None

    def test_available_cash_reuses_short_lived_margins_cache(self) -> None:
        original_ttl = settings.account_funds_cache_ttl_seconds
        provider = CountingMarginsProvider()
        try:
            object.__setattr__(settings, "account_funds_cache_ttl_seconds", 10)
            service = AccountFundsService(kite_provider=provider)  # type: ignore[arg-type]

            first = service.available_cash()
            second = service.available_cash()
        finally:
            object.__setattr__(
                settings, "account_funds_cache_ttl_seconds", original_ttl
            )

        self.assertEqual(first, 10000)
        self.assertEqual(second, 10000)
        self.assertEqual(provider.calls, 1)


if __name__ == "__main__":
    unittest.main()
