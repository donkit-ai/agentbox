# Release and catalog handoff: v0.2.0

The code prepares artifacts; it does not imply publication or catalog approval. npm authentication was not
available in the development environment. The PyPI name `agentbox` already exists and ownership is unverified;
the workflow publishes only the requested `agentbox-sdk`. The server remains available through Docker/Git and
its wheel is a GitHub release artifact. Confirm ownership of `@agentbox` on npm before the first SDK release.

## Local verification and artifacts

```sh
uv sync --extra dev
uv run ruff check .
uv run pytest -q --tb=short --show-capture=no
uv run python scripts/check_release.py
uv build --out-dir dist/server
uv build sdk/python --out-dir dist/sdk
cd sdk/typescript
npm ci && npm run build && npm pack --pack-destination ../../dist
cd ../..
uv run python scripts/build_agent_bundle.py --output dist
```

The MCPB installs the pinned SDK with `uv`, which must be available on the user's machine. It is a local bundle,
not a hosted gateway, and requires SDK v0.2.0 to be published first. `server.json` references the exact MCPB
release URL and includes its SHA-256. Upload the same bytes to that URL; rebuilding an archive changes its hash.
The wheel includes migrations and console/templates; installed servers do not require a source checkout.

## GitHub / package registries

`.github/workflows/release.yml` first runs the full reusable CI, then builds version-matched wheels, source
archives, npm tarball, skill ZIP and MCPB. A manual run without publishing creates artifacts only. To publish:

1. Review and push the code; create a GitHub `release` environment with required reviewers.
2. Configure a PyPI pending/trusted publisher for `agentbox-sdk`: repository `donkit-ai/agentbox`, workflow
   `release.yml`, environment `release`. Configure npm Trusted Publishing for the same repo/workflow/environment,
   with permission to publish `@agentbox/sdk`. The first npm package/namespace creation may need interactive setup.
3. Enable repository variables `PUBLISH_PYPI`, `PUBLISH_NPM`, `PUBLISH_IMAGE` individually with value `true` only
   after each destination is authorized. Push tag `v0.2.0` and approve the environment jobs. Docker publishes
   `ghcr.io/donkit-ai/agentbox:0.2.0` and `:0.2` for amd64/arm64, with provenance/SBOM.
4. Download `agentbox-release` artifacts and attach them to the matching GitHub release. Keep bundle/hash paired.
5. Verify installation from each public registry in a clean environment before announcing availability.

Publish uses short-lived OIDC credentials rather than stored registry tokens. npm needs CLI >=11.5.1 and Node
>=22.14; the workflow uses Node 24. See [PyPI trusted publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/)
and [npm trusted publishing](https://docs.npmjs.com/trusted-publishers/).

## Catalogs

- ClawHub: review `skills/agentbox/SKILL.md`, sign in with the intended publisher account and publish that folder
  as v0.2.0. The release ZIP contains only the skill. Do not include operator keys or demo connection links.
- Hermes: the skill is installable by copying the folder now. `distribution/hermes/manifest.yaml` is a prepared
  MCP catalog submission; submit it through Nous's reviewed `optional-mcps/` process after SDK publication.
  It is not an approved catalog entry until upstream accepts it. [Hermes catalog docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp/).
- Official MCP Registry: after uploading the MCPB to its release URL, authenticate the publisher and submit
  generated `dist/server.json` using `mcp-publisher publish`. The `io.github.donkit-ai/agentbox` namespace must be
  authorized by a GitHub account with the Owner role in `donkit-ai`.
  [Registry authentication](https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/authentication.mdx),
  [registry package types](https://modelcontextprotocol.io/registry/package-types).
- Smithery: use the local bundle path, `smithery mcp publish ./dist/agentbox-0.2.0.mcpb -n YOUR_NAMESPACE/agentbox`,
  after signing in. Its authenticated URL publication path requires OAuth, so do not register the static-Bearer
  HTTP endpoint as if it supported OAuth. [Smithery publishing](https://smithery.ai/docs/build/publish).

## Demonstration

Show the local wizard, scoped-key creation, quote start, a repeated start yielding the same message ID, fixture
reply injection and resumed matched wait. Then show a support draft waiting for human approval and CSV totals.
Only use synthetic mailboxes; never show `operator.json`, `agent.env` or a live connection URL in a recording.
The integration test `test_three_runnable_examples` exercises all three paths in separate processes.

To record the real fixture workflows on a fresh local demo stack:

```sh
uv run --extra dev python scripts/record_demo.py --setup agentbox-local \
  --output dist/agentbox-demo.cast --video dist/agentbox-demo.mp4
```

The `.cast` file contains actual command output with idle gaps compressed. The optional 30-second MP4 summarizes
verified results, not a desktop capture; rendering requires ffmpeg and DejaVu Sans Mono. Neither output includes
credentials. The recorder creates synthetic demo inboxes, revokes its extra agent keys, and leaves test mail in
that isolated stack. Existing output files are never overwritten. Stop that stack with
`docker compose --project-directory agentbox-local down` when finished (volumes are retained).

## Limits of verification

See the [maintainer acceptance checklist](pre-release-checklist.md) for account-dependent tasks and pass criteria.

Mock IMAP/OAuth and local SMTP tests do not prove live Gmail/Microsoft consent policies, real deliverability,
external catalog approval, or desktop-client installation. Complete those account-dependent checks before
claiming production onboarding is verified end to end.
