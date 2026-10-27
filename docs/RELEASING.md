# Releasing PhyAgentOS to GitHub and PyPI

[English](RELEASING.md) | [简体中文](RELEASING_zh.md)

`release-build.yml` builds and tests a tag. `publish-pypi.yml` publishes the
**existing GitHub Release wheel and source distribution** to `PhyAgentOS-ai` on
PyPI, then checks their hashes and installs from the official index in a fresh
Python 3.12 environment. It does not rebuild the package or upload `SHA256SUMS`.

## One-time authentication

Configure the repository Actions secret `PYPI_API_TOKEN` with a PyPI API token
authorized to publish `PhyAgentOS-ai`. Prefer a token scoped to this project.
The `pypi` job environment identifies production publication; the token is used
only in the publish job. Never commit a token or paste it into a workflow input.

## Publish a new version

1. Update the package/runtime/bridge versions, README badges, release highlights,
   News, changelog and versioned installation examples. Review and merge the
   release changes from `dev` into `main`.
2. Create an **annotated** `vMAJOR.MINOR.PATCH` tag on the intended release commit,
   following the previous release's annotation format. The tagged tree must
   contain the publishing workflow. Do not move a published tag.
3. Wait for **Build release package** to succeed. Download its
   `phyagentos-vMAJOR.MINOR.PATCH` artifact containing the wheel, source archive
   and `SHA256SUMS`.
4. Create a **draft** GitHub Release for that tag and attach all three files
   unchanged. Complete the release notes before publishing the draft as a
   stable release. Attach the files **before** clicking Publish release.
5. The `release: published` event starts **Publish release to PyPI**. Confirm all
   three jobs (`prepare`, `publish`, `verify`) succeed before announcing PyPI
   availability. Drafts and prereleases are excluded.

This keeps tag/build/release-note decisions with maintainers while making PyPI
publication and verification automatic. A GitHub Release created by another
workflow using `GITHUB_TOKEN` does **not** trigger a new release workflow. Such
automation must explicitly dispatch `publish-pypi.yml` after attaching assets,
or use an appropriately authorized GitHub App/PAT to publish the release.

## Retry or backfill a release

Use Actions → **Publish release to PyPI** → **Run workflow**, choose `main` and
enter the existing stable release tag. The CLI equivalent is:

```sh
gh workflow run publish-pypi.yml \
  --repo PhyAgentOS/PhyAgentOS-core --ref main -f tag=v1.1.1
```

The workflow requires all three GitHub Release attachments, checks SHA256 and
the package name/version embedded in both distributions, then checks PyPI:

- Identical existing files are skipped; registry and clean-install checks still run.
- A partial upload resumes with only the missing distribution.
- Conflicting bytes, yanked files, unexpected files or invalid metadata fail the
  run. Fix the release inputs or publish a new version; never overwrite a PyPI version.
- Missing credentials or a PyPI failure fail the run visibly. Rerun after fixing
  the cause; no package version bump is needed for an unchanged retry.

An idempotent run of an already published version verifies the workflow and
installation but does not exercise a new authenticated upload.

## Optional migration to Trusted Publishing

The initial integration uses the existing API token. To remove the long-lived
secret later, add a GitHub Trusted Publisher in the PyPI project's **Publishing**
settings with these exact values:

| Field | Value |
| --- | --- |
| Owner | `PhyAgentOS` |
| Repository | `PhyAgentOS-core` |
| Workflow filename | `publish-pypi.yml` |
| Environment | `pypi` |

Then update only the `publish` job: add `permissions: {id-token: write, contents: read}`,
remove the credential-check step and the action's `password` input, and remove
`attestations: false` to enable attestations. After a successful authenticated
publication, remove the GitHub secret and revoke the old token in PyPI. Do not
remove the secret before switching the workflow and configuring the publisher.

References: [PyPA publishing action](https://github.com/pypa/gh-action-pypi-publish),
[PyPI Trusted Publisher configuration](https://docs.pypi.org/trusted-publishers/adding-a-publisher/),
[GitHub workflow trigger behavior](https://docs.github.com/en/actions/how-tos/writing-workflows/choosing-when-your-workflow-runs/triggering-a-workflow#triggering-a-workflow-from-a-workflow).
