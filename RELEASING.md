# Releasing Traser SDK to PyPI

The public Python SDK is built from this repository and released through
PyPI Trusted Publishing using GitHub Actions OIDC. Do not create a long-lived
PyPI API token or upload production traces.

## One-time setup

1. Open https://github.com/Maas-Dorian/traser-sdk/settings/environments.
   Create a GitHub Actions environment named pypi. Configure required
   reviewers and release-tag restrictions where available, so a release
   cannot publish without approval.
2. Open https://pypi.org/manage/account/publishing/.
   If the project has not been published, create a pending trusted publisher
   for the following values:

   - PyPI project name: traser-sdk
   - GitHub owner: Maas-Dorian
   - Repository: traser-sdk
   - Workflow filename: publish.yml
   - Environment: pypi

   These must exactly match the repository, workflow and environment.
   If the project already exists and you own it, configure the new publisher
   under its Manage > Publishing page instead.
3. An earlier pending publisher may still refer to the private
   Maas-Dorian/Traser-Landing repo and publish-python-sdk.yml workflow.
   That publisher does not authorize publishing from this public repo;
   remove the stale entry after confirming the new one is configured.

GitHub OIDC supplies a short-lived credential; no password, token or secret
needs to be stored in GitHub.

## Publish version 0.1.0

1. Check that Python SDK CI is green on the latest main commit.
2. Visit https://github.com/Maas-Dorian/traser-sdk/releases/new.
3. Choose "Create new tag" and enter v0.1.0, targeting main.
4. Release title: Traser SDK v0.1.0.
5. Include a short note saying this is an alpha, opt-in, local-first evidence
   recorder, not a hosted service or automatic postcondition validator.
6. Choose "Publish release" (not "Save draft"). This starts the publish.yml
   workflow and builds the artifacts, but the PyPI deployment must also pass
   any approval rules configured on the pypi environment.
7. Go to https://github.com/Maas-Dorian/traser-sdk/actions/workflows/publish.yml
   and ensure all jobs pass. Approve the pypi job only after you confirm that
   the version, source commit and CI checks are expected.
8. Verify the package at https://pypi.org/project/traser-sdk/ and try it
   in a clean virtual environment:

       python -m venv .venv
       . .venv/bin/activate
       python -m pip install --upgrade pip
       python -m pip install traser-sdk==0.1.0
       python -c "from traser_sdk import Traser; print(Traser().run('smoke').to_run()['schemaVersion'])"

If the Trusted Publisher authentication fails, confirm the owner, repo,
workflow filename and environment on both GitHub and PyPI. The environment
must be called pypi, and the publishing workflow filename is publish.yml.

For a future release, update the project.version in pyproject.toml and
__version__ in src/traser_sdk/__init__.py, merge to main, then publish a
matching vX.Y.Z GitHub release.

## Scope

This repository contains the Apache-2.0 Python SDK and its synthetic tests
and examples. It does not contain the proprietary Traser investigation
engine, source for the site, or the npm MCP package.

The SDK records only explicitly instrumented calls and does not itself
authenticate external receipts, independently verify business outcomes, or
sanitize every possible secret.
