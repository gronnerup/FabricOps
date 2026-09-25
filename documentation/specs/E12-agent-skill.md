# E12 — An agent skill for FabricOps

**Goal:** ship a skill in the public repository so that an agent — Claude Code, or anything
that reads the same format — can drive FabricOps competently without the user learning the
CLI first.

## Why this belongs in the framework

FabricOps is an accelerator people adapt rather than adopt. The expensive part of adapting
it is not running commands, it is *writing the recipe*: knowing which keys exist, which
item types are valid, how layers map to workspaces, what a feature group is. That is
exactly the knowledge a skill can carry, and it is knowledge the repository already holds
in machine-readable form — the recipe schema, the item-type registry, the CLI's own help.

It is also the honest answer to a reader who clones the repo at 21:00 and wants a working
platform before bed.

## Scope

| Capability | Backed by |
| --- | --- |
| Generate or amend a recipe, valid on the first try | the JSON Schema (E01) and the alias table |
| Explain what a plan will do, in the reader's own terms | `fabricops plan --output json` |
| Run setup, feature and teardown safely, with `--dry-run` first | the CLI (E04 exit codes) |
| Diagnose a failed run | the run manifest and the JSONL trace (E03/E04) |
| Migrate a first-generation recipe to canonical keys | `recipe render` and the alias table |
| Answer "which item types can I use here?" | the CLI's item-type registry |

Explicitly **out** of scope: the skill never invents Fabric behaviour. Where it needs a
platform fact it consults Microsoft Learn or the research notes, and where it needs a
credential it stops and asks.

## Design notes

* **Lives in the public repo** (`.claude/skills/fabricops/`) so it travels with the
  framework and is versioned alongside the schema it describes.
* **Generated, not hand-maintained, wherever possible.** The recipe reference the skill
  reads is generated from the schema (E01-S5), so the skill cannot drift from the code.
* **Dry-run by default.** Any destructive verb (`setup --action delete`, `feature delete`,
  `tags sync`) is proposed with the exact command and requires the user to confirm.
* **Redaction-aware.** The skill reads traces and manifests, which are already redacted,
  and never echoes a credential the user pastes.

## User stories

**E12-S1 — Skill scaffold** — AC: `.claude/skills/fabricops/SKILL.md` with the trigger
description, the command surface, and the safety rules; discovered automatically in a
repo checkout.
**E12-S2 — Recipe authoring** — AC: from a plain-language description ("three
environments, seven layers, GitHub"), the skill produces a recipe that passes
`recipe validate` unaided.
**E12-S3 — Diagnosis** — AC: given a failed run, the skill locates the manifest and trace,
names the failing action, and proposes a fix.
**E12-S4 — Migration** — AC: the skill converts a first-generation `infrastructure.json`
to canonical YAML and explains each rename.
