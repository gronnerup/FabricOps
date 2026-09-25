# Legacy scripts

The first-generation implementations, kept for one release so a pipeline can fall back
if needed. They read the same recipe files and are unchanged.

`fabric_setup.py` is superseded by `automation/scripts/fabric_setup.py`, which is a thin
entry point over the `fabricops` package (see `documentation/specs/E03`). The new path
adds dependency-ordered planning, `--dry-run`, structured logging with redaction, a run
manifest and non-zero exit codes on failure.

Run the legacy version explicitly if you need it:

    python automation/scripts/legacy/fabric_setup.py --environment dev

`fabric_release.py` is superseded by `automation/scripts/fabric_release.py`, which is a
thin entry point over the `fabricops` package (see `documentation/specs/E09`). The new
path moves deployment policy into the recipe, generates its parameter entries into an
overlay instead of editing the committed `parameter.yml`, uses fabric-cicd's native
`semantic_model_binding` in place of the hand-written binding step, orders layers by their
dependencies and exits non-zero when a layer fails.

One argument changed meaning: `--repo_path` used to point at the solution folder, and the
layer directory was that plus the lower-cased layer name. It now points at the repository
root, because the layer directory comes from the recipe's `git.directory`. The shim
detects the old value and adjusts, with a note on stderr.

    python automation/scripts/legacy/fabric_release.py --environment tst --repo_path ./solution

## Superseded utilities

`utils_build_parameter_file_dynamic.py` and `utils_build_parameter_file.py` are no longer
called by any pipeline. They upserted generated entries into the committed
`automation/resources/parameters/parameter.yml` and copied it into every layer folder.
`fabricops release` now renders generated entries into
`automation/resources/parameters/generated/dynamic.parameter.yml`, pulled in by the
committed file's `extend:` list, and passes one shared parameter file to fabric-cicd via
`parameter_file_path` (documentation/specs/E09 section 3). Running the old scripts would
rewrite the file the new design promises not to touch.

They are left in place rather than deleted so an older pipeline definition still works.
