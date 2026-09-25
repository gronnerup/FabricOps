"""The public-sync gate: allowlist, denied paths, denied values, secret shapes."""

import pathlib
import shutil
import tempfile
import unittest

from fabricops.errors import RecipeError
from fabricops.sanitise import ExportPolicy, exportable_files, export, scan, summarise

POLICY = """
apiVersion: fabricops/v1
kind: ExportPolicy
include:
  - automation/src/**
  - documentation/**
  - README.md
exclude:
  - "**/.DS_Store"
deny_paths:
  - automation/credentials/credentials.json
deny_values:
  - 11111111-2222-3333-4444-555555555555
  - Contoso-Trial-01
secret_exceptions:
  - documentation/fixtures/**
allow_guids:
  - aaaaaaaa-0000-1111-2222-bbbbbbbbbbbb
"""


class SanitiseTestCase(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="fabricops-sanitise-"))
        self.policy_path = self.write("automation/resources/export.yml", POLICY)
        self.policy = ExportPolicy.load(self.policy_path)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, relative: str, text: str) -> pathlib.Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


class AllowlistTests(SanitiseTestCase):
    def test_only_allowlisted_paths_are_exportable(self):
        self.write("automation/src/fabricops/cli.py", "print('hi')\n")
        self.write("automation/resources/solutions/internal/platform.yml", "capacity: Contoso-Trial-01\n")
        self.write("README.md", "# FabricOps\n")

        relative = [path.relative_to(self.root).as_posix() for path in exportable_files(self.root, self.policy)]
        self.assertIn("automation/src/fabricops/cli.py", relative)
        self.assertIn("README.md", relative)
        self.assertNotIn("automation/resources/solutions/internal/platform.yml", relative)

    def test_a_new_internal_folder_is_excluded_by_default(self):
        self.write("automation/secrets-i-forgot-about/notes.md", "tenant stuff\n")
        relative = [path.relative_to(self.root).as_posix() for path in exportable_files(self.root, self.policy)]
        self.assertEqual(relative, [])

    def test_exclusions_win_over_inclusions(self):
        self.write("documentation/.DS_Store", "junk")
        self.write("documentation/specs/E01.md", "content\n")
        relative = [path.relative_to(self.root).as_posix() for path in exportable_files(self.root, self.policy)]
        self.assertEqual(relative, ["documentation/specs/E01.md"])


class ScanTests(SanitiseTestCase):
    def test_a_denied_path_the_allowlist_already_excludes_is_a_note(self):
        """It cannot leak, so failing the gate on it would make the gate permanently red."""
        self.write("automation/credentials/credentials.json", '{"tenant": "x"}')
        [finding] = scan(self.root, self.policy)
        self.assertEqual(finding.kind, "denied path (not exportable)")
        self.assertTrue(finding.informational)

    def test_a_denied_path_inside_the_export_set_still_fails(self):
        """This one really would be published, so it has to block."""
        self.write("documentation/specs/credentials.json", '{"tenant": "x"}')
        self.policy.deny_paths.append("documentation/specs/credentials.json")
        [finding] = [f for f in scan(self.root, self.policy) if "denied path" in f.kind]
        self.assertEqual(finding.kind, "denied path")
        self.assertFalse(finding.informational)

    def test_tenant_values_are_reported_with_a_line_number(self):
        self.write("documentation/specs/E01.md", "line one\ncapacity: Contoso-Trial-01\n")
        findings = scan(self.root, self.policy)
        self.assertEqual(findings[0].kind, "tenant value")
        self.assertEqual(findings[0].line, 2)
        self.assertIn("Contoso-Trial-01", str(findings[0]))

    def test_secret_shapes_are_reported(self):
        self.write("automation/src/fabricops/thing.py", 'TOKEN = "ghp_0123456789abcdefghijklmnopqrstuvwx"\n')
        findings = scan(self.root, self.policy)
        self.assertEqual(findings[0].kind, "possible secret")

    def test_pattern_checks_are_relaxed_where_declared_but_values_are_not(self):
        self.write("documentation/fixtures/example.md", 'client_secret=abcdef123456\ncapacity: Contoso-Trial-01\n')
        kinds = [finding.kind for finding in scan(self.root, self.policy)]
        self.assertEqual(kinds, ["tenant value"], "prose about secrets is fine; a tenant value is not")

    def test_passing_a_credential_between_variables_is_not_a_finding(self):
        """`client_secret = args.client_secret` is how the code works, not a leak."""
        self.write(
            "automation/src/fabricops/thing.py",
            "client_secret = args.client_secret\n"
            "credentials = Credentials(client_secret=Secret(os.environ['CLIENT_SECRET']))\n"
            'payload["credentialDetails.servicePrincipalSecret"] = credentials.client_secret\n',
        )
        self.assertEqual(scan(self.root, self.policy), [])

    def test_a_hardcoded_literal_credential_is_a_finding(self):
        self.write("automation/src/fabricops/thing.py", 'PASSWORD = "Sup3rSecretValue!"\n')
        findings = scan(self.root, self.policy)
        self.assertEqual([finding.kind for finding in findings], ["possible secret"])

    def test_obvious_placeholders_are_not_findings(self):
        self.write(
            "automation/src/fabricops/thing.py",
            'PASSWORD = "<your-password>"\nSECRET = "{env:CLIENT_SECRET}"\nKEY = "xxxxxxxxxx"\n',
        )
        self.assertEqual(scan(self.root, self.policy), [])

    def test_an_inline_marker_accepts_one_line(self):
        self.write(
            "automation/src/fabricops/thing.py",
            'EXAMPLE = "ghp_0123456789abcdefghijklmnopqrstuvwx"  # sanitise: allow\n',
        )
        self.assertEqual(scan(self.root, self.policy), [])

    def test_strict_mode_flags_unlisted_guids(self):
        self.write("documentation/specs/E01.md", "id: aaaaaaaa-0000-1111-2222-bbbbbbbbbbbb\nid: 12345678-1234-1234-1234-123456789abc\n")
        self.assertEqual(scan(self.root, self.policy), [])
        strict = scan(self.root, self.policy, strict=True)
        self.assertEqual([finding.line for finding in strict], [2])

    def test_a_clean_tree_has_no_findings(self):
        self.write("automation/src/fabricops/cli.py", "print('hi')\n")
        self.write("documentation/specs/E01.md", 'capacity: "{env:FABRIC_CAPACITY}"\n')
        self.assertEqual(scan(self.root, self.policy), [])

    def test_summary_counts_by_kind(self):
        self.write("automation/credentials/credentials.json", "{}")
        self.write("documentation/a.md", "Contoso-Trial-01\nContoso-Trial-01\n")
        self.assertEqual(
            summarise(scan(self.root, self.policy)),
            {"denied path (not exportable)": 1, "tenant value": 2},
        )


class ExportTests(SanitiseTestCase):
    def test_export_copies_only_the_allowlisted_tree(self):
        self.write("automation/src/fabricops/cli.py", "print('hi')\n")
        self.write("automation/resources/solutions/internal/platform.yml", "internal\n")
        target = self.root.parent / (self.root.name + "-export")
        try:
            copied = export(self.root, target, self.policy)
            self.assertEqual([path.as_posix() for path in copied], ["automation/src/fabricops/cli.py"])
            self.assertTrue((target / "automation/src/fabricops/cli.py").exists())
            self.assertFalse((target / "automation/resources/solutions").exists())
        finally:
            shutil.rmtree(target, ignore_errors=True)


class PolicyTests(SanitiseTestCase):
    def test_a_missing_policy_is_an_actionable_error(self):
        with self.assertRaises(RecipeError) as ctx:
            ExportPolicy.load(self.root / "nope.yml")
        self.assertIn("export.yml", ctx.exception.hint or "")


if __name__ == "__main__":
    unittest.main()
