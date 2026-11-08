import unittest

from sportybet_client import MAX_SELECTIONS


class SportyBetSelectionLimitTests(unittest.TestCase):
    def test_normal_multiple_limit_matches_published_kenya_limit(self):
        self.assertEqual(MAX_SELECTIONS, 50)


if __name__ == "__main__":
    unittest.main()
