import unittest

from specialist_models import _poisson_over, _poisson_under, _stats_by_team


class SpecialistModelMathTests(unittest.TestCase):
    def test_over_and_under_are_complementary_for_half_goal_line(self):
        over = _poisson_over(2.0, 1.5)
        under = _poisson_under(2.0, 1.5)
        self.assertAlmostEqual(over + under, 1.0, places=10)
        self.assertAlmostEqual(over, 1 - (3 * __import__("math").exp(-2)), places=10)

    def test_integer_line_uses_strict_over_under(self):
        self.assertAlmostEqual(_poisson_over(1.0, 1), 1 - 2 * __import__("math").exp(-1), places=10)
        self.assertAlmostEqual(_poisson_under(1.0, 1), __import__("math").exp(-1), places=10)

    def test_negative_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            _poisson_over(-1, 2.5)
        with self.assertRaises(ValueError):
            _poisson_under(1, -0.5)

    def test_statistics_parser_uses_team_names_and_numeric_values(self):
        parsed = _stats_by_team([
            {"team": {"name": "Home FC"}, "statistics": [
                {"type": "Corner Kicks", "value": 7},
                {"type": "Total Shots", "value": "12"},
                {"type": "Yellow Cards", "value": "2"},
            ]},
            {"team": {"name": "Away FC"}, "statistics": [
                {"type": "Corner Kicks", "value": 4},
            ]},
        ])
        self.assertEqual(parsed["home"]["corner kicks"], 7)
        self.assertEqual(parsed["home"]["total shots"], 12)
        self.assertEqual(parsed["away"]["corner kicks"], 4)


if __name__ == "__main__":
    unittest.main()
