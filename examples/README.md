# Examples

Working files that satisfy the published schemas in [`../schemas/`](../schemas/). They exist so you
can read a real, valid document instead of reconstructing one from a schema, and so you have
something concrete to diff your own files against.

| File | Schema | What it shows |
|---|---|---|
| [`i2v-short.capsule.json`](i2v-short.capsule.json) | [`capsule-manifest-v1`](../schemas/capsule-manifest-v1.json) | A complete image-to-video capsule: declared inputs, resolved render params, preflight gates, QC criteria, tier, and an honest delivery status |

---

## Read this before you copy the capsule

`i2v-short.capsule.json` carries `"status": "development"`. That is not boilerplate.

The **definition** is complete and stable — the inputs, parameters and checks in that file are what
the runtime will use. The **execution path is not in the public download.** The native engine path
for this capsule is not published, and the public experimental backend covers universal `t2v` /
`t2i` only (Linux + NVIDIA, using a torch/diffusers environment you supply).

So the file is a contract you can read, validate against, and build tooling on. It is not a feature
you can run today. Every capsule in this repository states its own status the same way, and the
`status` field is mandatory in the schema precisely so this gap can never be left implicit.

Its `timing` block is `"measurement in progress"` with null figures, for the same reason: no
render-time number has been measured under the published method and reproduced, so no number
appears. See [`../benchmark/`](../benchmark/).

---

## Validating a capsule

The schema is JSON Schema draft 2020-12 and is strict on purpose: `additionalProperties` is `false`
at every level, so a typo in a field name is a validation error rather than a silently ignored
setting.

```sh
pip install jsonschema
```

```python
import json
from jsonschema import Draft202012Validator

schema  = json.load(open("schemas/capsule-manifest-v1.json"))
capsule = json.load(open("examples/i2v-short.capsule.json"))

Draft202012Validator.check_schema(schema)
errors = sorted(Draft202012Validator(schema).iter_errors(capsule),
                key=lambda e: list(e.path))

for e in errors:
    print(list(e.path), e.message)
print("errors:", len(errors))
```

The example above validates with zero errors. If your own capsule does not, the path printed with
each error names the exact field.

---

## Writing your own capsule

Start from the example and change the parts that describe your job. The fields worth thinking about
rather than copying:

**`capsule_id` is public surface.** It is what `veizik run <capsule>` takes. Once published it must
not change; rename by publishing a new id and marking the old one deprecated with `superseded_by`.

**`version` is a reproducibility promise.** Bump the minor for additive parameters. Bump the
**major** whenever a default changes such that an unchanged invocation produces different output —
someone who pinned your version pinned their results.

**Declare every input you read.** The runtime rejects arguments it cannot find in the `inputs`
array rather than passing them through, so an undeclared parameter is an error, not a shortcut.

**Preflight is where you spend your care.** The design rule is to fail before the GPU is touched
wherever the failure is knowable in advance. A refusal that names the unmet condition and the
setting that resolves it costs the user seconds; the same failure discovered as an OOM twenty
minutes into a render costs them the render. Use `on_fail: "refuse"` for conditions that make the
run impossible, `"warn"` for conditions that merely make it worse.

**QC is advisory by default.** Set `enforce: true` only where there is genuinely nothing for a human
to judge — a non-finite value in the output latents, for instance. A black-frame ratio or a static
clip should be *flagged*, because a deliberate fade to black is a legitimate creative result and
deleting it would be the tool overruling the operator.

**`required_tier` is a declaration, not a gate.** It drives display and a local pre-check so the
user is not surprised at the end of a render. The authoritative entitlement decision is made by the
server; a capsule file is not a security boundary. See [`../SECURITY.md`](../SECURITY.md).

**Set `status` honestly.** A capsule whose runtime has not shipped is `research` or `development`,
never `shipped`, no matter how finished the definition looks. Publishing an unshipped capsule as
shipped is treated as a defect here, not as marketing.

**Leave `timing` null unless you measured it.** `measurement_status` exists so an unmeasured capsule
can say so. An estimate that looks plausible is indistinguishable from a claim, and a claim needs a
measurement behind it.

---

## Related

- [`../schemas/capsule-manifest-v1.json`](../schemas/capsule-manifest-v1.json) — the capsule contract
- [`../schemas/run-report-v1.json`](../schemas/run-report-v1.json) — the optional run report a
  consenting user's machine emits after a render; its `workload` fields mirror `params` here, so a
  capsule and its reports are directly comparable
- [`../benchmark/`](../benchmark/) — how numbers are measured and why the timing fields are empty
- [`../TELEMETRY.md`](../TELEMETRY.md) — what is and is not transmitted
