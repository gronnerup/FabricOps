"""Guards on the solution tree itself.

A layer folder maps 1:1 to a workspace, so an item that several workspaces need exists as
one copy per layer - `fabricops_util.Notebook` being the case that prompted this. Copies
drift silently, and the drift only shows up as a notebook behaving differently in Ingest
than in Prepare. This test makes it a PR failure instead.
"""

import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SOLUTION = REPO_ROOT / "solution"

# `.platform` legitimately differs between copies: each item carries its own logicalId.
IGNORED = {".platform", ".DS_Store"}


def item_folders() -> dict[str, list[pathlib.Path]]:
    """Every `<name>.<ItemType>` folder in the solution tree, grouped by folder name."""
    grouped: dict[str, list[pathlib.Path]] = {}
    if not SOLUTION.is_dir():
        return grouped
    for path in SOLUTION.rglob("*"):
        if not path.is_dir() or "." not in path.name:
            continue
        if any(parent.name.count(".") and parent != SOLUTION for parent in path.parents if parent != SOLUTION):
            continue  # inside another item
        grouped.setdefault(path.name, []).append(path)
    return grouped


def content(path: pathlib.Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for entry in sorted(path.rglob("*")):
        if entry.is_file() and entry.name not in IGNORED:
            files[entry.relative_to(path).as_posix()] = entry.read_bytes()
    return files


@unittest.skipUnless(SOLUTION.is_dir(), "solution tree not present")
class SharedItemTests(unittest.TestCase):
    def test_items_duplicated_across_layers_are_identical(self):
        for name, paths in item_folders().items():
            if len(paths) < 2:
                continue
            with self.subTest(item=name):
                reference, *others = sorted(paths)
                expected = content(reference)
                for other in others:
                    self.assertEqual(
                        content(other),
                        expected,
                        f"'{name}' differs between {reference.relative_to(REPO_ROOT)} and "
                        f"{other.relative_to(REPO_ROOT)} - shared items must be byte-identical "
                        f"apart from .platform",
                    )

    def test_every_item_folder_carries_a_platform_file(self):
        for name, paths in item_folders().items():
            for path in paths:
                with self.subTest(item=str(path.relative_to(REPO_ROOT))):
                    self.assertTrue(
                        (path / ".platform").exists(),
                        f"{name} has no .platform - git integration needs one per item",
                    )


if __name__ == "__main__":
    unittest.main()


class NotebookCellOrderTests(unittest.TestCase):
    """`%run` has to precede every use of what it loads.

    Fabric runs cells top to bottom, so a helper referenced above the `%run` that defines it
    fails at run time and cannot be caught by anything offline except this.
    """

    NOTEBOOKS = [
        "solution/engineering/ingest/Rebrickable_Ingest.Notebook",
        "solution/engineering/prepare/Rebrickable_Base.Notebook",
        "solution/engineering/prepare/Rebrickable_Curated.Notebook",
    ]
    HELPERS = (
        "layer_workspace_id",
        "write_schema",
        "lakehouse_tables",
        "current_workspace_id",
        "get_environment",
        "invoke_api",
        "write_delta",
        "copy_tables",
        "refresh_sql_endpoint",
        "create_table_shortcuts",
        "DEFAULT_SCHEMA",
    )

    def content(self, notebook):
        return (REPO_ROOT / notebook / "notebook-content.py").read_text(encoding="utf-8")

    def test_run_precedes_every_helper_use(self):
        for notebook in self.NOTEBOOKS:
            text = self.content(notebook)
            with self.subTest(notebook=notebook):
                self.assertIn("%run fabricops_util", text)
                run_at = text.index("%run fabricops_util")
                for helper in self.HELPERS:
                    for match in re.finditer(rf"^\s*{helper}\b", text[:run_at], re.MULTILINE):
                        self.fail(
                            f"{notebook}: {helper} is used at offset {match.start()}, "
                            f"before %run fabricops_util at {run_at}"
                        )

    def test_the_parameters_cell_holds_only_literals(self):
        """A pipeline overrides the parameters cell wholesale, so a call in it is discarded."""
        for notebook in self.NOTEBOOKS:
            text = self.content(notebook)
            with self.subTest(notebook=notebook):
                self.assertIn("# PARAMETERS CELL", text)
                start = text.index("# PARAMETERS CELL")
                cell = text[start:text.index("# METADATA", start)]
                for helper in self.HELPERS:
                    self.assertNotIn(
                        f"{helper}(", cell,
                        f"{notebook}: the parameters cell calls {helper}()",
                    )

    def test_no_notebook_asks_for_an_audience_that_does_not_exist(self):
        """`pbi` covers Fabric REST too; there is no `fabric` audience key.

        A wrong key fails deep inside Py4J with `"fabric" is not a valid resource`, which
        reads like a service problem rather than a one-word argument mistake.
        """
        valid = ("storage", "pbi", "keyvault", "kusto")
        for path in sorted(SOLUTION.rglob("notebook-content.py")):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"""audience\s*=\s*["'](\w+)["']""", text):
                with self.subTest(notebook=path.parent.name, audience=match.group(1)):
                    self.assertIn(match.group(1), valid)


@unittest.skipUnless(SOLUTION.is_dir(), "solution tree not present")
class PlatformFileFormatTests(unittest.TestCase):
    """`.platform` must be byte-identical to what Fabric writes, or it never looks synced.

    Fabric writes these files with **no** trailing newline. Most editors and most JSON
    formatters add one. Git integration compares bytes, so a single extra `\\n` makes the
    item show as *Modified* in every workspace connected to the branch - permanently, and
    on every run:

        Store · git wins over workspace changes to: Curated, Base, Landing

    Those three lakehouses have nothing but a `.platform`, so the newline was the entire
    difference. Committing from Fabric strips it, which is how it was found: the diff was
    one removed blank line.
    """

    def platform_files(self):
        return sorted(SOLUTION.rglob(".platform"))

    def test_there_are_platform_files_to_check(self):
        self.assertTrue(self.platform_files())

    def test_none_of_them_ends_with_a_newline(self):
        offenders = [
            str(path.relative_to(REPO_ROOT))
            for path in self.platform_files()
            if path.read_bytes().rstrip(b"\r\n") != path.read_bytes()
        ]
        self.assertEqual(
            offenders,
            [],
            "These .platform files end with a newline, so Fabric will report their items as "
            "modified in every connected workspace, for ever. Strip the trailing newline: "
            + ", ".join(offenders),
        )

    def test_none_of_them_uses_windows_line_endings(self):
        offenders = [
            str(path.relative_to(REPO_ROOT))
            for path in self.platform_files()
            if b"\r\n" in path.read_bytes()
        ]
        self.assertEqual(offenders, [], f"CRLF in: {', '.join(offenders)}")


# The repository is shaped like the three-layer floor from Episode 2: `store/`, and a
# responsibility folder for everything that lives inside `engineering/` and `analytics/`.
# A tier is only a choice of which of these get a workspace bound to them.
RESPONSIBILITIES = {
    "engineering": {"ingest", "prepare", "orchestrate", "core"},
    "analytics": {"model", "present"},
}


@unittest.skipUnless(SOLUTION.is_dir(), "solution tree not present")
class ResponsibilityPlacementTests(unittest.TestCase):
    """Every item under a combined layer folder must sit inside a responsibility folder.

    Fabric puts a new item at the workspace root unless told otherwise. On a workspace
    bound to `engineering/`, that lands the item directly under `engineering/` where it
    belongs to no leaf - and the day the tier scales up it is a manual sort. The platform
    cannot enforce placement. The repo can, on every commit, which turns the convention
    into a guarantee and makes "a rebind, not a move" true.
    """

    def test_the_combined_folders_exist(self):
        for group in RESPONSIBILITIES:
            self.assertTrue((SOLUTION / group).is_dir(), group)

    def test_nothing_sits_directly_under_a_combined_folder(self):
        stray = []
        for group, allowed in RESPONSIBILITIES.items():
            for entry in sorted((SOLUTION / group).iterdir()):
                if entry.name in IGNORED:
                    continue
                if entry.name not in allowed:
                    stray.append(str(entry.relative_to(REPO_ROOT)))
        self.assertEqual(
            stray, [],
            "These belong to no responsibility. Move each into one of its group's folders "
            "(engineering: ingest, prepare, orchestrate, core; analytics: model, present): "
            + ", ".join(stray),
        )

    def test_every_responsibility_folder_holds_items_not_loose_files(self):
        for group, allowed in RESPONSIBILITIES.items():
            for leaf in allowed:
                folder = SOLUTION / group / leaf
                if not folder.is_dir():
                    continue      # empty folders are not committed by Fabric
                for entry in folder.iterdir():
                    if entry.name in IGNORED or entry.name == "README.md":
                        continue
                    with self.subTest(path=str(entry.relative_to(REPO_ROOT))):
                        self.assertTrue(entry.is_dir(), "loose file at responsibility level")


@unittest.skipUnless(SOLUTION.is_dir(), "solution tree not present")
class NotebookMarkdownBlankLineTests(unittest.TestCase):
    """A blank line in a markdown cell is `# ` (hash, space), never a bare `#`.

    Fabric drops a bare `#` on import, so a notebook committed with one shows as *Modified*
    in every connected workspace until someone commits it back from Fabric - whose diff is
    the removed line. Same class as the `.platform` trailing newline: a byte convention
    that never looks synced. Fabric's own exports use `# ` for the blank, so that is the
    rule. Code cells are untouched; a bare `#` is a legal Python comment there.
    """

    MARK = re.compile(r"^# (MARKDOWN|CELL|METADATA) \*{5,}\s*$")

    def offenders(self, path):
        in_md, bad = False, []
        for number, line in enumerate(path.read_text().split("\n"), 1):
            m = self.MARK.match(line)
            if m:
                in_md = m.group(1) == "MARKDOWN"
                continue
            if in_md and line == "#":
                bad.append(number)
        return bad

    def test_no_bare_hash_lines_in_markdown_cells(self):
        found = {
            str(p.relative_to(REPO_ROOT)): self.offenders(p)
            for p in sorted(SOLUTION.rglob("notebook-content.py"))
            if self.offenders(p)
        }
        self.assertEqual(
            found, {},
            "Bare '#' lines in markdown cells; Fabric will strip them and report the notebook "
            "as modified for ever. Write blank markdown lines as '# ' (with a trailing space).",
        )


# Item types git integration can hold. Extend when a new type joins the solution.
FABRIC_ITEM_TYPES = {
    "Lakehouse", "Warehouse", "Notebook", "DataPipeline", "SemanticModel", "Report", "Dataflow",
    "Environment", "SparkJobDefinition", "KQLDatabase", "KQLQueryset", "KQLDashboard", "Eventstream",
    "Eventhouse", "MirroredDatabase", "SQLDatabase", "VariableLibrary", "MLModel", "MLExperiment",
    "Reflex", "PaginatedReport", "GraphQLApi", "CopyJob", "MountedDataFactory", "ApacheAirflowJob",
    "UserDataFunction", "DigitalTwinBuilder",
}
ENVIRONMENT_TOKEN = re.compile(r"(?i)(?:^|[\s_\-\[(])(dev|tst|test|uat|prd|prod)(?:$|[\s_\-\])])")


def item_platform_files():
    return sorted(p for p in SOLUTION.rglob(".platform") if p.parent.name.count(".") >= 1)


@unittest.skipUnless(SOLUTION.is_dir(), "solution tree not present")
class NamingConventionTests(unittest.TestCase):
    """The naming discipline from Episode 2 and 3, checked on every pull request."""

    def test_the_type_suffix_is_a_fabric_item_type(self):
        # `Curated.Lakehous` is a folder git integration will never sync into a workspace.
        bad = [str(p.parent.relative_to(REPO_ROOT)) for p in item_platform_files()
               if p.parent.name.rsplit(".", 1)[1] not in FABRIC_ITEM_TYPES]
        self.assertEqual(bad, [], "unknown item type suffix (extend FABRIC_ITEM_TYPES if it is new): " + ", ".join(bad))

    def test_the_platform_type_matches_the_folder_suffix(self):
        # displayName and folder name are *allowed* to differ: the post says rename an item
        # by changing displayName, not the folder, and Fabric binds the folder by logicalId
        # rather than renaming it after. The type is a different matter - a folder called
        # X.Notebook whose .platform says Lakehouse is a mismatch git integration cannot
        # resolve.
        import json as _json

        bad = []
        for p in item_platform_files():
            declared = (_json.loads(p.read_text()).get("metadata") or {}).get("type")
            suffix = p.parent.name.rsplit(".", 1)[1]
            if declared and declared != suffix:
                bad.append(f"{p.parent.relative_to(REPO_ROOT)} (.platform type {declared!r})")
        self.assertEqual(bad, [], ".platform type and folder suffix differ: " + "; ".join(bad))

    def test_no_environment_token_in_an_item_name(self):
        # The workspace carries the environment. `Curated`, never `Curated_dev_v2`.
        bad = [str(p.parent.relative_to(REPO_ROOT)) for p in item_platform_files()
               if ENVIRONMENT_TOKEN.search(p.parent.name.rsplit(".", 1)[0])]
        self.assertEqual(bad, [], "environment named in an item, where the workspace already carries it: " + ", ".join(bad))
