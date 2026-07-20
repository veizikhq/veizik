# Supported environments (v0.1.0 Public Preview)

Honest status. "Internal" means verified repeatedly on our own hardware; **no
environment has enough external reproductions yet to be called Certified** —
that is what this preview is for.

| Environment | Tier | Evidence | Notes |
|---|---|---|---|
| Linux x86_64 + NVIDIA 24 GB (RTX 3090/4090 class) | **Experimental** (internal-verified) | repeated internal runs (LTX-13B, CogVideoX, Step-Video) | primary target; `doctor` + `login` + universal render path |
| Windows 11 + WSL2 + NVIDIA | **Experimental** | internal runs on WSL2 | CUDA-on-WSL required; report issues |
| macOS (Apple Silicon) | **Waitlist** | not shipped | `doctor`/`login` run, render path not shipped — email support@veizik.com with subject "Waitlist: macos" |
| NVIDIA < 16 GB | **Untested** | none | smaller profiles planned; `doctor` will tell you what it detects |

Tier ladder (promotion rules): Experimental → Community-Verified (3+ external
successes) → Beta-Compatible (10+ external, ≥90% success) → Certified (30+
external, ≥95%). Share your run via a
[compatibility report issue](https://github.com/veizikhq/veizik/issues).

We never claim "works on every GPU". If your environment is not on this table,
assume Untested and try `veizik doctor` first — it costs nothing.
