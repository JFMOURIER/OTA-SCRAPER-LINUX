from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.playwright_safe import prepare_profile_directory


class ProfileLockTests(unittest.TestCase):
    def test_stale_lock_is_preserved_not_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            lock = profile / "SingletonLock"
            lock.symlink_to("old-host-999999")
            prepare_profile_directory(profile)
            self.assertFalse(lock.exists())
            preserved = list(profile.glob("SingletonLock.stale_*") )
            self.assertEqual(len(preserved), 1)
            self.assertTrue(preserved[0].is_symlink())

    def test_active_browser_lock_is_left_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            lock = profile / "SingletonLock"
            lock.symlink_to("host-12345")
            with patch("services.playwright_safe._profile_lock_owner", return_value=(12345, "host-12345")):
                with self.assertRaisesRegex(RuntimeError, "in use"):
                    prepare_profile_directory(profile)
            self.assertTrue(lock.is_symlink())


if __name__ == "__main__":
    unittest.main()
