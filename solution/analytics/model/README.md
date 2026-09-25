# Model

Semantic models. Deployed to the `Model` workspace.

`Rebrickable.SemanticModel` is **Direct Lake on OneLake** over the `Curated` lakehouse in
the **Store** workspace. It needs no refresh: the Orchestrate pipeline lands new Delta
files and the model reads them.

## The M expression points at Store, explicitly

The lakehouse lives in `Store`, and the model lives in `Model`, so the expression names
the Store workspace by name:

```yaml
find_replace:
  - find: "<placeholder>"
    replace: { dev: "$workspace.Store" }
```

`$workspace.$id` would be wrong here. It resolves to *the workspace the item is being
deployed into* — Model — and there is no lakehouse there. Two further rules on the
dynamic-token syntax, both of which cost a deployment to learn:

* `$workspace.<name>` is valid; `$workspace.<name>.$id` is **not**. The `.$id` suffix is
  only legal after an `$items.` segment, as in
  `$workspace.Store.$items.Lakehouse.Curated.$id`.
* The connection also needs the lakehouse id, not just the workspace, so both are
  parameterised. See `automation/resources/parameters/parameter.yml`.

## Why these ids block the sync

Both ids in the expression are declared `blocks_sync: true` in the recipe's `references:`
block, exactly like the report's semantic model and the pipeline's notebooks. Without it the
model syncs happily while pointing at a workspace and a lakehouse that do not exist — and
then it cannot be repaired, because Fabric will not update an item whose *current* state is
broken. A correct commit is not enough; the workspace copy has to be deleted and recreated.

So the model waits instead, and says what to run:

```
Model · Git integration... ✔ (connected; model: Curated lakehouse points at 0000…a002,
        but this environment has e22a31e7… - run `fabricops references sync
        --environment dev --apply` and commit)
```

Store deploys before Model, so the lakehouse it needs is already there to resolve against.

## TMDL: `ref table` sits at depth 0

The one thing that will stop the model opening. In `definition/model.tmdl`, `ref table`
lines and model-level `annotation` lines are **not** indented under `model`:

```tmdl
model Model
	culture: en-US
	defaultPowerBIDataSourceVersion: powerBI_V3

ref table Sets
ref table Themes

annotation PBI_QueryOrder = ["Sets","Themes"]
```

Indenting them under `model` gives `Unexpected line type: ReferenceObject` when Power BI
Desktop opens the project, and the error does not say which line. The bundled SpaceParts
sample is the reference to check against if a model will not open.

## Measures in a report need the model published first

Writing a PBIR visual against a measure that does not yet exist in a *published* model
types the field as `Column`, and the visual then renders nothing. Deploy the model, then
author the report — or hand-correct `"Measure"` and the `sortDefinition` in the visual
JSON, which is what `Rebrickable.Report` carries.
