from pathlib import Path
import unittest


class BookingScrollTimerRegressionTests(unittest.TestCase):
 def test_load_more_loop_initializes_last_growth_before_first_read(self):
    source = (Path(__file__).parents[1] / "collectors" / "booking_playwright.py").read_text()
    region = source[source.index("def load_all_booking_results"):]
    assignment = region.index("last_growth = start")
    first_read = region.index("time.monotonic() - last_growth")
    self.assertLess(assignment, first_read)


 def test_timer_regression_has_growth_reset_and_two_attempt_bound(self):
    source = (Path(__file__).parents[1] / "collectors" / "booking_playwright.py").read_text()
    region = source[source.index("def load_all_booking_results"):]
    self.assertIn("max_unchanged = 2", region)
    self.assertIn("last_growth = time.monotonic()", region)
