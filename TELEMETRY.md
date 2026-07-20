# Veizik Telemetry

Veizik renders on hardware you own. Your prompts, inputs, and outputs stay on your machine.

This document is the complete and binding description of what the Veizik CLI sends off your
machine, what it never sends, how you consent, how you inspect it, and how you delete it. If
this document and any marketing page disagree, **this document governs**.

Operator: **LinkPick** (Republic of Korea), operating the Veizik brand.
Contact: [privacy@veizik.com](mailto:privacy@veizik.com)

---

## 0. Implementation status

Veizik labels every capability with an explicit status. Telemetry is no exception.

| Component | Status |
| --- | --- |
| Two-tier data separation (license operations vs. optional performance data) | Development |
| Two-step consent screen | Development |
| `veizik telemetry` command group | Development |
| `veizik-run-report-v1` schema (this repo, `schemas/run-report-v1.json`) | Public Preview |
| Server-side ingestion + `privacy` field rejection | Development |
| Public aggregate compatibility results | Research |

Status vocabulary used across all Veizik docs: **Research** → **Development** → **Private
Preview** → **Public Preview** → **Shipped**.

Nothing described here is retroactive. No performance data is collected from a build that
did not present you the consent screen described in §3.

---

## 1. The two data categories — never merged

Veizik keeps two strictly separate data categories. They use separate endpoints, separate
storage, separate retention, and separate legal bases. They are never joined into a single
combined profile for analytics purposes.

### Category A — License operations data (required, no consent prompt)

This is the minimum data required to operate a paid license at all: to know that a license
exists, that it is valid, and how many nodes are concurrently running under it. Without it
the product cannot be licensed, so it is processed as necessary for performance of the
contract — not on the basis of consent.

| Field | Purpose |
| --- | --- |
| License id / license hash | Identify the license being validated |
| **Pseudonymous device identifier** | Enforce registered-device and concurrent-node limits |
| Plan / tier | Determine entitlements |
| App version, protocol version | Compatibility and forced-upgrade handling |
| Activation state | Active / revoked / expired |
| Last verification timestamp | Offline grace and re-verification windows |
| Execution lease + lease expiry | Concurrent-node accounting |
| Subscription state | Billing status from the payment processor |

**Concurrency, not machines.** Veizik meters *concurrent execution nodes*. Veizik does not
charge per PC. Your plan states how many devices you may register and how many may render
at the same time.

### Category B — Optional performance & compatibility data (separate explicit consent)

This is the hardware-compatibility corpus: which GPUs actually complete which workloads, and
where they fail. It is collected **only** if you say yes at the second consent step, and it
is entirely severable from the product.

**Declining Category B never disables, degrades, throttles, or time-limits any feature you
purchased.** There is no "share data to unlock" mechanic in Veizik and there never will be.

<a id="collected"></a>

#### B.1 Hardware

GPU vendor · GPU model · VRAM size · GPU count · architecture · driver version · CUDA
runtime version · OS major version · CPU class · RAM size band (bucketed, e.g. `32-64GB`) ·
storage class · power limit · form factor (desktop / laptop / server).

#### B.2 Execution settings

`veizik` version · `capsule_id` · `adapter_id` · model public identifier and model hash ·
precision · quantization mode · attention backend · resolution · frame count · step count ·
batch size · tile settings · offload settings · concurrent job count · cold vs. warm start.

#### B.3 Result

Start / end timestamp · wall-clock duration · model load time · denoise time · VAE and
post-processing time · peak VRAM · peak system RAM · average GPU utilization · power draw
(optional) · success or failure · error code · stage at which the run stopped · OOM flag ·
recover-path flag · user-cancelled flag.

#### B.4 Product signals

First successful render · cumulative successful renders · number of days used · per-feature
use counts · Preview access requests · Pro-tier interest signals.

---

## 2. What Veizik never collects

The following are **never** transmitted under Category A or Category B. They are not
optional-by-default; they are excluded from the payload by an allowlist filter that runs on
your machine before anything is queued (see §5). If any of these ever becomes collectible,
it would require a new, separate, explicitly-worded consent — not a silent schema bump.

| Never collected | |
| --- | --- |
| ✗ Prompt text | ✗ Negative prompt text |
| ✗ Input images or videos | ✗ Generated output (any frame, any thumbnail) |
| ✗ Filenames | ✗ Full or partial local filesystem paths |
| ✗ OS username | ✗ Computer name / hostname |
| ✗ IP address stored as behavioral data | ✗ Full environment variable dump |
| ✗ API keys | ✗ Third-party tokens or credentials |
| ✗ Contents of your model directories | ✗ Terminal input / shell history |
| ✗ Raw crash-dump memory | ✗ Any content of the media you produce |

On IP addresses: an IP is unavoidably visible to the receiving server as a transport-layer
fact of making an HTTPS request. Veizik does not write it into the event record, does not
join it to run reports, and does not use it as an analytics dimension. Transport logs are
kept only for abuse and security purposes on a short rotation.

---

## 3. Consent — two steps, both honest

The consent flow runs once, on first use, before any event can be queued.

### Step 1 — License operations notice

Informational. Explains Category A: what license operations data is processed and why the
product cannot function without it. **One button: Continue.** This step is a notice, not a
consent request, because pretending you can decline data that is strictly necessary for the
licensed service would be a false choice.

### Step 2 — Optional performance data request

A real, free choice. Explains Category B in the same terms as §1, links this document, and
offers **Yes** and **No**. Both buttons proceed to a fully functional product. No dark
patterns: the two buttons have equal visual weight, "No" is not pre-dimmed, and there is no
nag loop. Your answer is recorded with a consent version and timestamp, and you can change
it at any time with `veizik telemetry enable` / `veizik telemetry disable`.

Consent is requested separately per purpose, and consent to Category B is never bundled into
the purchase, the EULA acceptance, or the license activation.

---

## 4. CLI commands

```sh
veizik telemetry status       # consent state, versions, queue depth, full field list
veizik telemetry enable       # grant consent for Category B
veizik telemetry disable      # withdraw consent, stop all future sending
veizik telemetry show-last    # print the exact JSON of the most recent run report
veizik telemetry queue        # list pending reports waiting to be sent
veizik telemetry send         # flush the queue now instead of waiting for the batch window
veizik telemetry export       # write all locally held telemetry to a file you choose
veizik telemetry delete       # request server-side deletion + purge the local spool
```

### `status` output contract

`veizik telemetry status` must print, every time, without truncation:

- current consent state (`enabled` / `disabled`)
- the **consent version** you agreed to and the **timestamp** of that agreement
- number of reports pending in the local queue
- the full list of fields that **are** collected, each marked `✓`
- the full list of fields that are **never** collected, each marked `✗`

The `✗` list is printed even when telemetry is disabled. You should never have to read a
website to find out what a binary on your machine sends.

### `disable` behavior

`disable` takes effect immediately: no further reports are generated or transmitted from
that moment, including reports already generated but not yet sent. It then asks a single
question — whether to also delete the locally queued reports — and honors either answer. It
never sends "one last batch" on the way out.

### `show-last` and `export`

`show-last` prints the literal payload, not a summary of it. `export` writes everything
Veizik holds locally in machine-readable form. Both exist so that the claim in §2 is
verifiable by you rather than merely asserted by us.

---

## 5. How a report travels

```
render completes
  → run report written to local JSON
  → sensitive-field allowlist check   (client-side; anything not on the allowlist is dropped)
  → local queue (spool)
  → compressed batch
  → server schema validation          (telemetry.veizik.com)
  → raw storage (short retention)
  → aggregation
```

Operational rules:

- **Local spool cap:** 100 reports or 10 MB, whichever comes first. Oldest reports are
  discarded past the cap — telemetry never grows without bound on your disk.
- **Batch window:** approximately every 24 hours, or immediately on `veizik telemetry send`.
- **Failure is inert.** A telemetry transmission failure never blocks, delays, retries into,
  or otherwise affects a render. If the telemetry endpoint is down, unreachable, or blocked
  by your firewall, Veizik renders exactly as normal.
- **Server-side rejection.** The ingestion endpoint validates every report against
  `schemas/run-report-v1.json`. Reports whose `privacy` object does not consist of four
  `false` values are **rejected outright**, not sanitized and stored. This makes an
  accidental content leak a hard ingestion failure rather than a silent data spill.

---

## 6. Endpoint separation

| Purpose | Endpoint |
| --- | --- |
| License operations (Category A) | `api.veizik.com/v1/license/*` |
| Optional performance data (Category B) | `telemetry.veizik.com/v1/events` |

These are separate services with separate storage. The separation is load-bearing in two
directions:

1. **Telemetry outage must never stop a licensed render.** The render path has no dependency
   on `telemetry.veizik.com`.
2. **The analytics corpus is not the license database.** Category B is not stored joined to
   your license record for analytics use.

You may block `telemetry.veizik.com` at your firewall with no effect on the product other
than that no performance data is sent.

---

## 7. Retention

| Data | Retention |
| --- | --- |
| Category A — license operations | Life of the license plus the period required for tax, accounting, and dispute handling |
| Category B — raw run reports | **30–90 days**, then deleted |
| Category B — aggregates | Retained long-term; not reducible to an individual installation |
| Transport / security logs | Short rotation, security and abuse purposes only |

Published compatibility results are only ever released as aggregates above a **minimum
sample threshold**. A GPU model, driver version, or configuration reported by too few
installations is not published, because a small enough bucket is an identifier.

---

## 8. Access, export, and deletion

- **Inspect locally:** `veizik telemetry show-last`, `veizik telemetry queue`
- **Export locally:** `veizik telemetry export`
- **Delete:** `veizik telemetry delete` — purges the local spool and submits a server-side
  deletion request keyed to your `installation_id`. All raw run reports carrying that
  installation id are removed.
- **By email:** [privacy@veizik.com](mailto:privacy@veizik.com), for access, export,
  correction, deletion, restriction, objection, or consent withdrawal.

Aggregates already computed cannot be un-computed after the fact; they contain no field that
identifies an installation, which is why they are safe to retain. Deletion removes your raw
records and stops your data contributing to any future aggregate.

---

## 9. Terminology

Veizik does not describe this data as **anonymous**. The device identifier is a
**pseudonymous** identifier: it is a hash rather than a name, but it is stable and can in
principle be associated with a license and therefore an account holder. Under Korea's
Personal Information Protection Act (PIPA) and the GDPR, that is pseudonymized personal data,
not anonymous data, and it retains the corresponding protections and your corresponding
rights. Calling it "anonymous" would be both inaccurate and a way of quietly reducing what
you are owed.

---

## 10. Summary

- Two categories, kept apart. License operations is necessary; performance data is optional.
- Declining performance data costs you **nothing you paid for**.
- Prompts, inputs, outputs, paths, and filenames are never sent — and the server refuses any
  report claiming otherwise.
- You can print, export, and delete everything from the CLI.
- Concurrency is the billing unit. Veizik does not charge per PC.

---

See also: [PRIVACY.md](PRIVACY.md) · [schemas/run-report-v1.json](schemas/run-report-v1.json)

© 2026 Veizik (operated by LinkPick, Republic of Korea)
