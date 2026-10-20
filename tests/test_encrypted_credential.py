"""At-rest encrypted credentials: only failure paths are covered here.

Decrypting a real token requires the WorkBuddy client at runtime, which tests must
never touch. These cases pin the contract for when the client or helper is absent.
"""
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from codex_monitor import buddy_quota


class EncryptedCredentialTests(unittest.TestCase):
    def test_env_override_pointing_nowhere_yields_no_executable(self):
        with patch.dict(os.environ, {"WORKBUDDY_EXE": str(Path("missing") / "WorkBuddy.exe")}):
            self.assertIsNone(buddy_quota.workbuddy_executable())

    def test_missing_client_reports_missing_program(self):
        with patch.object(buddy_quota, "workbuddy_executable", return_value=None):
            with self.assertRaises(buddy_quota.QuotaFailure) as caught:
                buddy_quota.decrypt_token(Path("auth.info"))
        self.assertEqual(str(caught.exception), buddy_quota.ENCRYPTED_NO_EXE)

    def test_missing_helper_reports_decrypt_failure(self):
        client = Path("WorkBuddy.exe")
        with patch.object(buddy_quota, "workbuddy_executable", return_value=client):
            with patch.object(buddy_quota.Path, "is_file", return_value=False):
                with self.assertRaises(buddy_quota.QuotaFailure) as caught:
                    buddy_quota.decrypt_token(Path("auth.info"))
        self.assertEqual(str(caught.exception), buddy_quota.ENCRYPTED_FAILED)


if __name__ == "__main__":
    unittest.main()
