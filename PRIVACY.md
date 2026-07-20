# Veizik Privacy Notice

**Operator:** LinkPick (Republic of Korea), operating the Veizik brand
**Contact:** [privacy@veizik.com](mailto:privacy@veizik.com)
**Applies to:** the Veizik CLI, the Veizik license service, and veizik.com
**Governing framework:** Republic of Korea *Personal Information Protection Act* (PIPA), with
the EU/UK *General Data Protection Regulation* (GDPR) applied to users in those jurisdictions

Veizik is a **local** runtime. Rendering happens on hardware you own. Your prompts, your
input media, and your generated output never leave your machine as part of normal operation.
Veizik is not a cloud render service, and there is no server-side copy of your work.

This notice describes the small amount of data that does leave your machine, and the line
between the part that is required and the part that is optional.

---

## 1. Two categories, kept separate

Veizik processes personal data in two strictly separated categories. They use separate
endpoints, separate storage, separate retention periods, and separate legal bases. They are
not merged into a single profile.

### Category A — License operations data

**Legal basis:** performance of a contract (PIPA Art. 15(1)(4); GDPR Art. 6(1)(b)).
**Consent prompt:** none, because this data is strictly necessary to provide the licensed
service. You are given notice of it, not a false choice about it.

| Data | Why it is necessary |
| --- | --- |
| License id / license hash | Establish that a valid license exists |
| Pseudonymous device identifier | Enforce registered-device and concurrent-node limits |
| Plan / tier | Determine which entitlements to sign |
| App version, protocol version | Compatibility, security updates, forced-upgrade handling |
| Activation state | Distinguish active, revoked, and expired licenses |
| Last verification timestamp | Offline grace periods and re-verification |
| Execution lease and expiry | Count how many nodes are rendering concurrently |
| Subscription state | Reflect billing status from the payment processor |

Endpoint: `api.veizik.com/v1/license/*`

Veizik meters **concurrent execution nodes**, not machines. Your plan states how many devices
you may register and how many may render at the same time. Veizik does not charge per PC.

### Category B — Optional performance and compatibility data

**Legal basis:** consent (PIPA Art. 15(1)(1); GDPR Art. 6(1)(a)).
**Consent prompt:** yes — an explicit, separate, freely refusable request.

This is the hardware-compatibility corpus: which GPUs actually complete which workloads, at
what memory ceiling, and where they fail. It covers hardware class, execution settings,
timing and success/failure results, and coarse feature-use counters. The full field list is
in [TELEMETRY.md §1](TELEMETRY.md#collected) and the machine-readable definition is
[`schemas/run-report-v1.json`](schemas/run-report-v1.json).

Endpoint: `telemetry.veizik.com/v1/events`

> **Declining Category B never disables, degrades, throttles, or time-limits any feature you
> purchased.** There is no "share your data to unlock" mechanic in Veizik. Under the GDPR,
> consent conditioned on access to a paid service is not freely given and therefore not valid
> consent; we would rather have valid consent than more data. If you say no, you keep
> everything you bought.

The separation is also operational: a telemetry outage cannot block a render, because the
render path has no dependency on `telemetry.veizik.com`. You may block that host at your
firewall with no effect other than that no performance data is sent.

---

## 2. The pseudonymous device identifier — stated honestly

Veizik identifies an installation using a **pseudonymous** identifier: a hash derived on your
machine, not your name, email, hostname, or hardware serial number.

We do **not** call this "anonymous," and you should be suspicious of any vendor that does.
The identifier is stable across runs, and it can in principle be associated with a license,
and a license can be associated with an account holder. That combination capability is
exactly what distinguishes pseudonymized data from anonymized data.

Under PIPA and the GDPR, pseudonymized data is still **personal data**. It keeps the full
protections of this notice and you keep the full set of rights in §5. Treating it as
"anonymous" would be a quiet way of reducing what you are owed, so we do not do it.

---

## 3. What Veizik never collects

The following never leave your machine. They are excluded by an allowlist filter that runs
locally before anything is queued for transmission, and the receiving server independently
rejects any report that claims to contain them.

- ✗ Prompt text and negative prompt text
- ✗ Input images, video, or audio
- ✗ Generated output — including thumbnails and single frames
- ✗ Filenames and full or partial local filesystem paths
- ✗ Your OS username or computer name
- ✗ IP addresses stored or used as behavioral data
- ✗ Full environment variable dumps
- ✗ API keys and third-party tokens
- ✗ Contents of your model directories
- ✗ Terminal input and shell history
- ✗ Raw crash-dump memory

Every run report carries a `privacy` object with four boolean fields, all of which must be
`false`. The ingestion service **rejects** any report where they are not — the report is
refused, not cleaned up and stored. This makes an accidental content leak a hard, visible
ingestion failure rather than a silent data spill.

On IP addresses: an IP is unavoidably visible to any server you make an HTTPS request to. It
is not written into your event records, not joined to your run reports, and not used as an
analytics dimension. Transport and security logs are kept on a short rotation for abuse
prevention only.

---

## 4. Consent

Consent for Category B is requested once, before any performance data can be queued, through
a two-step screen:

1. **License operations notice** — what Category A is and why it is unavoidable. One button:
   Continue. This is a notice, not a consent request.
2. **Optional performance data** — what Category B is, with **Yes** and **No** given equal
   weight. Both answers lead to a fully functional product.

Consent is specific to this purpose. It is not bundled into the purchase, the EULA, or the
license activation, and it is not implied by continued use. Your answer is stored with a
consent version and timestamp, and you can change it at any time:

```sh
veizik telemetry status     # what you consented to, when, and exactly which fields
veizik telemetry enable
veizik telemetry disable    # takes effect immediately; no farewell batch
```

Withdrawal is as easy as granting, and withdrawal costs you no functionality.

---

## 5. Your rights

Under PIPA and the GDPR you may request access, correction, deletion, restriction of
processing, objection to processing, portability, and withdrawal of consent. Most of these
are available directly from the CLI, without asking us or waiting on us:

| Right | How |
| --- | --- |
| See exactly what was sent | `veizik telemetry show-last`, `veizik telemetry queue` |
| Export your data | `veizik telemetry export` |
| Delete your data | `veizik telemetry delete` — purges the local spool and submits a server-side deletion request keyed to your `installation_id` |
| Withdraw consent | `veizik telemetry disable` |
| Anything else | [privacy@veizik.com](mailto:privacy@veizik.com) |

We respond to email requests within 30 days. Deletion removes the raw run reports associated
with your installation id and stops your data contributing to any future aggregate.
Aggregates already computed are retained: they contain no field that identifies an
installation, which is the reason they are safe to keep and the reason they cannot be
selectively unwound.

---

## 6. Retention

| Data | Retention |
| --- | --- |
| Category A — license operations | Life of the license, plus the period required for tax, accounting, and dispute handling |
| Category B — raw run reports | **30–90 days**, then deleted |
| Category B — aggregates | Long-term; not reducible to an individual installation |
| Transport / security logs | Short rotation, security and abuse purposes only |

Published compatibility results are released only as aggregates above a **minimum sample
threshold**. A GPU model, driver version, or configuration reported by too few installations
is withheld, because a sufficiently small bucket is itself an identifier.

---

## 7. Processors and transfers

| Function | Provider | Data involved |
| --- | --- | --- |
| Payments | Polar (Merchant of Record) | Billing and subscription data, held by the processor under its own privacy policy |
| Website and API hosting | Infrastructure providers under data-processing agreements | Category A; Category B on a separate service |

Veizik does not sell personal data, does not share it with advertising networks, and does not
use it for advertising profiling.

Data may be processed outside your country of residence, including in the Republic of Korea.
Where required for transfers of EU/UK personal data, we rely on the appropriate safeguards,
including Standard Contractual Clauses.

---

## 8. Children

Veizik is a professional tool and is not directed at children. We do not knowingly collect
personal data from children under 14 (Republic of Korea) or under 16 (GDPR, where the local
age applies).

---

## 9. Changes

Material changes to what is collected require a **new consent version**, presented to you
before any collection under the new terms. A schema revision is never used as a way to widen
collection under a consent you already gave. This notice is versioned in the repository, so
its change history is inspectable rather than announced.

---

## 10. Summary

- Veizik renders locally. Your prompts, inputs, and outputs stay on your machine.
- Required license data and optional performance data are separate, and stay separate.
- Optional means optional: refusing it costs you nothing you paid for.
- The device identifier is **pseudonymous**, not anonymous, and is treated as personal data.
- You can inspect, export, and delete from your own terminal.

---

See also: [TELEMETRY.md](TELEMETRY.md) · [schemas/run-report-v1.json](schemas/run-report-v1.json)

© 2026 Veizik (operated by LinkPick, Republic of Korea)
