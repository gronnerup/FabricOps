"""Secrets must never reach a log sink - by construction, then by pattern."""

import unittest

from fabricops.obs.redaction import Secret, redact, register_secret, render_argv, to_process_argv

SECRET_VALUE = "s3cr3t-client-secret-value"


class SecretTypeTests(unittest.TestCase):
    def test_str_and_repr_are_masked(self):
        secret = Secret(SECRET_VALUE, "client_secret")
        self.assertEqual(str(secret), "***")
        self.assertNotIn(SECRET_VALUE, repr(secret))
        self.assertIn("client_secret", repr(secret))

    def test_reveal_returns_the_value(self):
        self.assertEqual(Secret(SECRET_VALUE).reveal(), SECRET_VALUE)

    def test_f_string_cannot_leak(self):
        secret = Secret(SECRET_VALUE)
        self.assertNotIn(SECRET_VALUE, f"secret is {secret}")

    def test_a_very_short_secret_is_masked_but_not_scrubbed_globally(self):
        """A one-character value must never be substituted out of unrelated text."""
        short = Secret("s", "client_secret")
        self.assertEqual(str(short), "***")
        self.assertEqual(redact("client_secret=abc123def456").split("=")[0], "client_secret")
        self.assertNotIn("***ecret", redact("this sentence has many s characters"))

    def test_empty_secret_is_falsey(self):
        self.assertFalse(Secret(None))
        self.assertTrue(Secret("x" * 8))


class ArgvRenderingTests(unittest.TestCase):
    def test_render_masks_secret_elements(self):
        argv = ["fab", "auth", "login", "-u", "client-id", "-p", Secret(SECRET_VALUE), "--tenant", "tenant-id"]
        rendered = render_argv(argv)
        self.assertNotIn(SECRET_VALUE, rendered)
        self.assertIn("-p ***", rendered)
        self.assertIn("client-id", rendered)

    def test_process_argv_reveals_and_keeps_elements_separate(self):
        argv = ["fab", "mkdir", "Sales - Store [dev].Workspace", "-P", "capacityname=Trial 01", Secret(SECRET_VALUE)]
        process = to_process_argv(argv)
        self.assertEqual(process[2], "Sales - Store [dev].Workspace")
        self.assertEqual(process[-1], SECRET_VALUE)
        self.assertEqual(len(process), len(argv))

    def test_render_quotes_values_with_spaces(self):
        self.assertIn("'Sales - Store [dev].Workspace'", render_argv(["fab", "exists", "Sales - Store [dev].Workspace"]))


class RedactTextTests(unittest.TestCase):
    def test_registered_value_is_scrubbed_anywhere(self):
        register_secret("another-secret-token-value")
        self.assertNotIn("another-secret-token-value", redact("failed for another-secret-token-value in body"))

    def test_pattern_denylist(self):
        cases = [
            "credentialDetails.servicePrincipalSecret=abc123def456",
            "client_secret: hunter2hunter2",
            "Authorization: Bearer abc.def.ghi",
            "token=ghp_0123456789abcdefghijklmnopqrstuvwx",
            "https://x/y?sig=ABCdef123%2Fxyz456789",
        ]
        for case in cases:
            with self.subTest(case=case):
                self.assertIn("***", redact(case))

    def test_secret_shaped_key_keeps_its_key_name(self):
        self.assertTrue(redact("client_secret=abc123def456").startswith("client_secret="))

    def test_disabled_redaction_is_a_passthrough(self):
        secret = Secret("explicitly-unredacted-value")  # registers the value
        self.assertIn(secret.reveal(), redact(f"value {secret.reveal()}", enabled=False))


if __name__ == "__main__":
    unittest.main()


class ProseIsNotASecretTests(unittest.TestCase):
    """Redaction has to survive ordinary English, or it destroys error messages.

    The real case: `Failed to get access token: Something went wrong` became
    `... token: ***`, which hid the one message that said the session had expired.
    """

    def test_a_sentence_containing_token_is_left_alone(self):
        message = (
            "[AuthenticationFailed] Failed to get access token: Something went wrong "
            "while trying to acquire a token. Please try to run `fab auth logout`."
        )
        self.assertEqual(redact(message), message)

    def test_prose_mentioning_a_key_or_a_secret_is_left_alone(self):
        for message in (
            "The key: value pairs below are documented in the recipe reference.",
            "No secret: this is a public sample.",
            "Please run fab auth login to acquire new tokens",
        ):
            with self.subTest(message=message):
                self.assertEqual(redact(message), message)

    def test_unambiguous_assignments_are_still_redacted(self):
        for message, expected in (
            ("client_secret=abc123supersecret", "client_secret=***"),
            ("password: hunter2hunter2", "password: ***"),
            ("access_token: eyJabcdefghij", "access_token: ***"),
            ("token=abcdefghijklmnop", "token=***"),
            ('"token": "ghp_abcdefghijklmnop"', '"token": ***'),
        ):
            with self.subTest(message=message):
                self.assertEqual(redact(message), expected)

    def test_a_short_value_after_an_ambiguous_word_is_not_a_secret(self):
        """`key = 1` in a sample is not a credential."""
        self.assertEqual(redact("key = 1"), "key = 1")
