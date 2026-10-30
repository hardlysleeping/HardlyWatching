"""`count` in a request body: 0 means "every video", not "missing".

The selector offers 5 / 10 / 25 / 50 / All, and All is the value 0. Both the
page and the server used `x or 5`, so All silently queued the newest five and
the one option that asks for everything did nothing.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class CountParamTests(unittest.TestCase):
    def setUp(self):
        import server
        self.count_param = server.count_param

    def test_zero_survives(self):
        self.assertEqual(self.count_param({"count": 0}), 0, "All must stay All")
        self.assertEqual(self.count_param({"count": "0"}), 0)

    def test_plain_numbers(self):
        self.assertEqual(self.count_param({"count": 25}), 25)
        self.assertEqual(self.count_param({"count": "50"}), 50)

    def test_missing_or_blank_takes_the_default(self):
        self.assertEqual(self.count_param({}), 5)
        self.assertEqual(self.count_param({"count": None}), 5)
        self.assertEqual(self.count_param({"count": ""}), 5)

    def test_junk_and_negatives_are_safe(self):
        self.assertEqual(self.count_param({"count": "abc"}), 5)
        self.assertEqual(self.count_param({"count": -3}), 0)

    def test_default_is_overridable(self):
        self.assertEqual(self.count_param({}, default=10), 10)


if __name__ == "__main__":
    unittest.main()
