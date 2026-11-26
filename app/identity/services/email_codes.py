"""Consume email verification hashes while retaining challenge coordination state."""

from app.identity.models import AccountDeletionChallenge, EmailVerification, PasswordReset


def consume_email_code(
    challenge: EmailVerification | PasswordReset | AccountDeletionChallenge,
) -> None:
    # The row is also a lock/proof record; removing the hash makes the code unusable.
    challenge.code_hash = ""
    challenge.save(update_fields=["code_hash"])
