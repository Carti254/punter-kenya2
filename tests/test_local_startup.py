import os
import unittest
from unittest.mock import patch

import bot


class LocalStartupTests(unittest.TestCase):
    def test_polling_lock_is_optional_without_database_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(bot, "get_connection") as get_connection:
                self.assertFalse(bot.acquire_polling_lock())
                get_connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
