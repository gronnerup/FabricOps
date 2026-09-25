"""Local credentials files: convenience for a local run, never a substitute for a secret store."""

import json
import pathlib
import stat
import tempfile
import unittest

from fabricops.engine import credentials_file


class FindTests(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="fabricops-creds-"))

    def write(self, name, body):
        path = self.dir / name
        path.write_text(json.dumps(body))
        path.chmod(0o600)
        return path

    def test_nothing_there_is_not_an_error(self):
        self.assertIsNone(credentials_file.find(self.dir))
        self.assertIsNone(credentials_file.load(self.dir))

    def test_the_environment_specific_file_wins(self):
        self.write("credentials.json", {"client_id": "shared"})
        self.write("credentials.dev.json", {"client_id": "dev-only"})
        loaded = credentials_file.load(self.dir, environment="dev")
        self.assertEqual(loaded.values["client_id"], "dev-only")

    def test_it_falls_back_to_the_shared_file(self):
        self.write("credentials.json", {"client_id": "shared"})
        loaded = credentials_file.load(self.dir, environment="tst")
        self.assertEqual(loaded.values["client_id"], "shared")

    def test_template_placeholders_are_not_credentials(self):
        """The shipped template must not read as a working identity."""
        self.write("credentials.json", {
            "tenant_id": "00000000-0000-0000-0000-000000000000",
            "client_secret": "YourAppSecret",
            "github_pat": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "client_id": "a-real-looking-id",
        })
        self.assertEqual(credentials_file.load(self.dir).values, {"client_id": "a-real-looking-id"})

    def test_unreadable_json_is_reported_not_raised(self):
        (self.dir / "credentials.json").write_text("{not json")
        loaded = credentials_file.load(self.dir)
        self.assertEqual(loaded.values, {})
        self.assertTrue(loaded.warnings)

    def test_loose_permissions_are_warned_about(self):
        path = self.write("credentials.json", {"client_id": "x"})
        path.chmod(0o644)
        self.assertTrue(any("chmod" in w for w in credentials_file.load(self.dir).warnings))

    def test_owner_only_permissions_are_quiet(self):
        self.write("credentials.json", {"client_id": "x"})
        self.assertFalse(credentials_file.load(self.dir).warnings)


class PrecedenceTests(unittest.TestCase):
    """A flag or an environment variable is deliberate; the file only fills gaps."""

    def test_the_file_never_overrides_an_explicit_value(self):
        local = credentials_file.LocalCredentials(
            {"client_id": "from-file", "client_secret": "file-secret"}, pathlib.Path("x"), []
        )
        merged = credentials_file.merge({"client_id": "from-flag"}, local)
        self.assertEqual(merged["client_id"], "from-flag")
        self.assertEqual(merged["client_secret"], "file-secret")

    def test_an_empty_explicit_value_does_not_block_the_file(self):
        local = credentials_file.LocalCredentials({"client_id": "from-file"}, pathlib.Path("x"), [])
        self.assertEqual(credentials_file.merge({"client_id": None}, local)["client_id"], "from-file")

    def test_no_file_leaves_explicit_values_alone(self):
        self.assertEqual(credentials_file.merge({"client_id": "a"}, None), {"client_id": "a"})


if __name__ == "__main__":
    unittest.main()
