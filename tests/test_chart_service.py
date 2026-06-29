import unittest

from app.services.chart_service import ChartService


class ChartServiceTests(unittest.TestCase):
    def test_build_series_returns_expected_points(self) -> None:
        service = ChartService()
        points = service.build_series([100, 105, 103, 108], "close")
        self.assertEqual(len(points), 4)
        self.assertEqual(points[0]["y"], 100)


if __name__ == "__main__":
    unittest.main()
