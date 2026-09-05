# Agent developer experience implementation

Scope: seven improvements requested by the maintainer. Existing inbound webhook work is preserved.

- [x] First-run wizard: demo / existing mailbox / own domain, generated secrets and restricted agent key.
- [x] Browser mailbox connection links; bring-your-own OAuth in core; shared-provider domain isolation.
- [x] Safe send retries; new-mail waits; persistent reply waits and leased event acknowledgement.
- [x] SDK/MCP ergonomics: async, pagination, timeouts, files, HTTP transport; release automation.
- [x] Inbox-scoped read-only / review / allowlisted autonomous permission presets.
- [x] CLI and console diagnostics with an explicit demo roundtrip.
- [x] Three runnable, fixture-backed examples and CI smoke coverage; installation and release docs.

Local verification completed on 2026-09-05:

- Standalone OSS core: 185 tests passed. Combined core/cloud repository: 193 tests passed.
- Added browser-link expiry/org-isolation and browser-bound, single-use OAuth callback regression tests.
- Gitleaks 8.30.1 found no secrets in either repository's staged changes. Actionlint 1.7.12 accepted core CI/release
  and cloud CI workflows. These are local checks, not a claim that remote CI or a security audit has completed.
- pip-audit found no known vulnerabilities in the core environment's 85 third-party distributions; the two
  editable local projects were excluded from that advisory check. npm's production dependency audit also passed.
- Ruff and whitespace checks passed; generated async source matches its sync generator.
- SDK wheel imported successfully on Python 3.10 with MCP 1.29.1 and 2.1.1. Native async and per-caller HTTP
  MCP isolation are integration-tested. TypeScript build and live API smoke passed.
- Clean server wheel contains migrations through 0011 and first-run/console templates.
- Docker image 0.2.0 built locally; fresh isolated wizard completed migrations, secret/key provisioning and
  outbound Mailpit + synthetic SMTP reply roundtrip. All three fixture examples also ran in separate processes.
- SDK wheels/npm tarball, MCPB, skill ZIP and registry manifest built in `dist/`. Official MCPB validation and
  registry schema/hash checks passed. Package archives include the Apache license.
- `dist/agentbox-demo.cast` records actual workflow output; `dist/agentbox-demo.mp4` is a 30-second summary of
  verified results. Neither contains API keys. `scripts/record_demo.py` makes them reproducible.
- Isolated test stacks stopped; their volumes/private configuration are retained. Existing stack untouched.

Live Gmail/Microsoft consent and internet delivery, desktop-client
installation and external catalog approval remain account-dependent checks, not claims made by local tests.
The [maintainer checklist](pre-release-checklist.md) provides concrete tasks and pass criteria for these checks.

Publishing to npm/PyPI and external catalogs requires registry authorization and release approval.
Do not represent queued/provider-accepted mail as delivered, or SMTP as exactly-once.
