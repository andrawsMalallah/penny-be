import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cryptography.fernet import Fernet, InvalidToken
from django.test import SimpleTestCase

from config.environment import load_local_environment


class LocalEnvironmentTests(SimpleTestCase):
    def test_environment_wins_and_password_interpolation_is_disabled(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "PENNY_SECRET_KEY=file-key\nPENNY_DB_PASSWORD='literal${HOME}value'\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"PENNY_SECRET_KEY": "process-key"}, clear=True):
                load_local_environment(path)
                self.assertEqual(os.environ["PENNY_SECRET_KEY"], "process-key")
                self.assertEqual(os.environ["PENNY_DB_PASSWORD"], "literal${HOME}value")

    def test_file_key_survives_separate_loads(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("PENNY_SECRET_KEY=stable-test-key\n", encoding="utf-8")
            for _ in range(2):
                with patch.dict(os.environ, {}, clear=True):
                    load_local_environment(path)
                    self.assertEqual(os.environ["PENNY_SECRET_KEY"], "stable-test-key")

    def test_fernet_dependency_round_trip_and_tamper_rejection(self) -> None:
        cipher = Fernet(Fernet.generate_key())
        encrypted = cipher.encrypt(b"synthetic-enrollment-secret")
        self.assertNotIn(b"synthetic-enrollment-secret", encrypted)
        self.assertEqual(cipher.decrypt(encrypted), b"synthetic-enrollment-secret")
        with self.assertRaises(InvalidToken):
            Fernet(Fernet.generate_key()).decrypt(encrypted)
