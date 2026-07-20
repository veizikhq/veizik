# Security Policy

Veizik runs on hardware you own and holds a license credential on that machine. That makes two
things security-relevant: **what the client does to your machine**, and **what the license and
telemetry services do with the small amount of data they receive**. This document states how to
report a problem in either, and — just as importantly — what we already know is not defensible so
you do not spend your time on it.

This is a **Public Preview**. The policy below describes the process we actually operate today,
not an aspirational program.

---

## Reporting a vulnerability

**Email: security@veizik.com**

Please do **not** open a public GitHub issue for a suspected vulnerability. Use the issue tracker
for bugs, and email for anything with a security consequence. If you are unsure which it is, email
us; a misrouted report is a far better outcome than a public one.

Include whatever you have. A report is useful even when it is incomplete:

- What you attacked (CLI command, license API endpoint, pack install path, telemetry submission).
- The version — `veizik status` prints it, or paste the release tag.
- Reproduction steps, or the request you sent. A `curl` line is ideal.
- What you observed, and what you expected instead.
- Impact as you see it. Your read is a data point even if we score it differently.

If you want to encrypt, say so in a first email with no details and we will exchange a key.

**What to expect.** These are targets we intend to meet, not a contractual SLA:

| Stage | Target |
|---|---|
| Acknowledgement that a human read it | 3 business days |
| Initial triage and severity assessment | 7 days |
| Fix or documented mitigation for a confirmed server-side issue | as fast as the severity warrants; a live entitlement bypass is fixed and deployed the same day, not batched into a release |
| Fix for a confirmed client-side issue | next release, unless it is remotely exploitable |

We will tell you what we concluded, including when we conclude the report is not a vulnerability
and why. Silence is a failure on our part — if a week passes with nothing from us, resend.

**Credit.** With your permission we credit reporters in the release notes for the fix. Tell us the
name or handle you want, or that you would rather stay anonymous.

**Bounty.** There is no monetary bug bounty at Preview stage. We would rather say so plainly than
imply one exists.

---

## Supported versions

Veizik is in Public Preview and ships from a single release line. Only the latest published release
receives security fixes.

| Version | Supported |
|---|---|
| Latest published release (`v0.1.x` Public Preview) | Yes |
| Any earlier Preview build | No — upgrade with `veizik update` |

Server-side components (the license API and the telemetry endpoint) are not versioned by the
client; they are patched in place, and a fix reaches every user without an update on your side.

---

## Threat model

Being explicit about what the design does and does not defend against is the point of this section.
A report that lands inside a **known-accepted** limitation will be closed as such, and we would
rather you know that before spending an evening on it.

### What is defended

**Integrity of what you install.** Runtime packs are distributed with a detached **Ed25519**
signature over a manifest that pins each file's SHA-256. The client verifies the signature against
a public key embedded in the client before anything is written to disk. Verification failure is a
refusal to install, not a warning you can click past. If the `cryptography` library is not present,
the client refuses to install rather than silently skipping the check. `veizik pack verify`
re-checks file contents and signature for everything already installed, at any time.

**Entitlement decisions.** Which tier you hold, how many machines are registered, and how many
concurrent runs you may have are decided by the server and returned signed. The client renders that
answer; it does not compute it.

**Confidentiality of your work.** Prompts, input files, generated outputs and local file paths are
never transmitted — not under license operations, and not under optional performance reporting. The
run report schema (`schemas/run-report-v1.json`) is a strict allowlist with `additionalProperties:
false` at every level, and carries a `privacy` object whose four members must all be `false`. A
report claiming otherwise is **rejected at ingestion rather than sanitized and stored**, so an
accidental content leak surfaces as a hard failure instead of a quiet spill.

### Signature means authenticity, not authorization

This distinction is load-bearing and is a common source of misdirected reports.

A valid signature proves a pack **came from us and was not modified in transit**. It proves nothing
about whether *you* are allowed to run it. Those are separate mechanisms:

- **Authenticity** — Ed25519 signature over the pack manifest. Enforced by the client.
- **Authorization** — tier, registered-machine count, concurrent-node count. Enforced by the
  **server**, which issues signed entitlements and holds the seat state.

A client that has been patched to skip the signature check has defeated authenticity on *its own
machine only*. It has not gained entitlement, because entitlement is not a client-side boolean —
it is a server-issued grant. This is why we treat the two categories very differently below.

### Client-side patching is known-accepted

Veizik ships as code that runs on your computer. Any local check in it can be patched out by
someone with a debugger and enough time. We know this. It is a property of local software, not a
defect we can fix by trying harder in the client.

**Therefore the following are known-accepted and out of scope**, and reports of them will be closed
without a fix:

- Patching the binary or Python source to bypass a local tier check, watermark, or feature gate.
- Removing or stubbing the signature verification call in your own copy.
- Forging a local entitlement file to make `veizik status` display a tier you do not hold.
- Extracting the embedded public key. It is public; it is meant to be readable.

What is **in scope** is anything that turns a local bypass into a **server-side** one — obtaining
an entitlement you did not pay for, exceeding the concurrency the server granted, acting on another
account, or reading another user's data. Those are real vulnerabilities and we want them.

The practical consequence: an entitlement is only meaningful when the server has issued it. Any
feature whose value depends on server-held state degrades gracefully against a patched client, and
anything that depends purely on a local check is understood to be an honesty mechanism rather than
a security control. We prefer to describe it that way rather than market client-side gates as
protection.

### Separation of the license and telemetry paths

The two services are separated deliberately, so that a compromise or a subpoena of one does not
yield the other's data.

| | License / entitlement API | Telemetry endpoint |
|---|---|---|
| Purpose | Activation, tier, machine registration, concurrency | Optional hardware-compatibility and performance reports |
| Consent | Required for the product to function (contract basis) | **Separate, optional, revocable** — a distinct second consent step |
| Identifier | License identifier + pseudonymous device identifier | Pseudonymous installation identifier only |
| Carries a license key? | Yes | **No** |
| Effect of declining | Product cannot verify entitlement | **None — every purchased feature keeps working** |
| Turn it off | `veizik deactivate` | `veizik telemetry disable` |

Declining or disabling telemetry never locks a feature. That is a design commitment, and treating
telemetry consent as a paywall would be a bug worth reporting.

### Secret handling

- **The pack signing private key is offline.** It does not live on a build server, in CI, or in any
  environment reachable from the network. Only the **public** key is distributed, embedded in the
  client. There is no key material in this repository, and there is nothing here to leak.
- **No secrets in the public repo.** Service credentials live in the deployment platform's secret
  store and are referenced by environment variable. If you ever find a credential committed here,
  that is a valid, high-severity report — send it to security@veizik.com rather than opening an
  issue, and we will rotate first and discuss second.
- **Your license key is a secret.** It is stored under your home directory with owner-only
  permissions. Do not paste it into issues, logs, or screenshots. If you leak one, run
  `veizik deactivate` and contact support for a rotation. Bug reports never need the key itself.

### Known limitations, stated rather than hidden

- Device identity is client-asserted. There is no server-issued hardware attestation yet, so the
  registered-machine and concurrency caps raise the cost of sharing a seat rather than making it
  impossible. Server-side atomic seat accounting is in place; hardware attestation is planned.
- Experimental universal `t2v` / `t2i` execute inside a Python environment **you** provide. We do
  not audit your torch/diffusers install, and model weights you supply are code-adjacent — treat
  third-party weights with the same caution you would give any downloaded executable.
- The public download does not include the native CUDA engine assets, so the client-side surface
  in the current release is smaller than the eventual product's.

---

## What we would especially like reported

Ranked by how much we want to hear about it:

1. **Entitlement or payment bypass** — obtaining a paid tier without paying, upgrading a key you do
   not own, exceeding server-granted concurrency, or getting the server to bind a purchase to the
   wrong key or account.
2. **Cross-account access** — reading, modifying, revoking, or enumerating another user's keys,
   sessions, machines, or reports.
3. **Content leakage** — any path by which a prompt, input file, generated output, filename, local
   path, hostname, or username reaches our servers. This contradicts an explicit promise, and we
   treat it as high severity regardless of how narrow the trigger is.
4. **Supply chain and update integrity** — anything that gets unsigned, substituted, or downgraded
   content installed; signature verification that can be skipped without patching the client; a
   hostile mirror that survives verification; installer weaknesses in `install.sh`.
5. **Local privilege or code execution** — path traversal in pack extraction, writes outside the
   install directory, unsafe deserialization, injection reachable from a manifest, capsule file, or
   any other input the client parses.
6. **Telemetry consent violations** — data collected without the second consent, collection
   continuing after `telemetry disable`, or a feature that is locked because telemetry was declined.

### Out of scope

Beyond the known-accepted client-patching items above:

- Missing hardening headers, TLS configuration nits, or scanner output with no demonstrated impact.
- Denial of service through raw volume, or any test that degrades service for other users.
- Social engineering of our staff, users, or support channels.
- Physical attacks, and attacks requiring an already-compromised machine or a malicious local admin.
- Vulnerabilities in third-party dependencies with no Veizik-specific exploit path — report those
  upstream; tell us if we ship an affected version and we will update.
- Findings from a machine or account that is not yours.

---

## Safe harbor

We will not pursue legal action against you, and we will not ask a platform to act against you, for
**good-faith security research** conducted under this policy. If a third party brings action
against you for research that stayed within these bounds, we will make it known that your activity
was authorized.

Good faith means:

- You test only against **your own** account, machine, license, and data.
- You do not access, modify, or retain another user's data. If you encounter it accidentally, you
  stop, do not save it, and tell us what you saw so we can assess exposure.
- You do not degrade, disrupt, or overload the service. No stress testing, no automated scanning
  that generates meaningful load, no destructive proof-of-concepts.
- You do not extort, and you do not attach a deadline-plus-payment demand to a report.
- You give us a reasonable opportunity to fix before publishing. **90 days** is our default; we are
  usually much faster, and we are happy to agree on a shorter window when a fix ships quickly. If
  we go quiet, publishing after 90 days is reasonable and we will not treat it as hostile.
- You comply with applicable law. Nothing here authorizes activity that is unlawful where you are.

If you are unsure whether something is in bounds, ask first at security@veizik.com. We would rather
answer a question than argue about a boundary afterward.

---

*Reviewed as of the current Public Preview release. Substantive changes to this policy appear in
the release notes.*
