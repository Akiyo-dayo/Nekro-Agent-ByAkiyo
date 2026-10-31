# Changelog

Version numbers here are this derivative edition's own (`FORK_VERSION` in `nekro_agent/tools/common_util.py`). The `version` in `pyproject.toml` is the upstream
NekroAgent version this edition is based on (`2.3.3`) and is intentionally left untouched: it drives plugin-compatibility checks and update checks.

## v0.2.0 (2026-10-31)

First public edition. It is exported as a whole from the maintainers' development repository, so there is no per-commit history here; this entry summarizes what the
edition contains compared with upstream `2.3.3`. The authoritative statement of modifications is `NOTICE` ("Statement of changes") together with this file; to identify exactly which files differ, compare this tree with the upstream `2.3.3` release.

### Added
- Multi-instance operation (one process, several logical bot instances), a platform console, and three-level permissions (platform operator / tenant admin / read-only member).
- Identity and favourability: memory-domain based isolation of favourability and memory, identity alias requests with an approval flow, joining an instance to a memory domain with the existing favourability folded in, and a soft reply gate that makes a bot less eager to reply unprompted to people it has a low favourability with.
- LLM usage ledger: one row per upstream request, aggregated by instance, tenant and model group (`/console/usage`).
- Model-group API keys are encrypted at rest; a startup audit warns about example, placeholder, short or duplicated JWT signing keys, a weak admin password and a factory-default database password.
- Optional `docker.sock` hardening (`docker/docker-compose-x-hardened.yml`, [`docs/docker-guard.md`](docs/docker-guard.md)) and a short operations guide ([`docs/operations.md`](docs/operations.md)).
- Database migrations 15 to 26 add the tenant / model-group, favourability, identity-scope, memory-persona, usage-ledger, identity-alias-request, password-generation and uniqueness tables, columns and indexes. Apply them in order.

### Changed
- **Container images are published to this edition's GHCR namespace** (`ghcr.io/akiyo-dayo/nekro-agent-byakiyo` and `ghcr.io/akiyo-dayo/nekro-agent-byakiyo-sandbox`; the packages are public and can be pulled anonymously) by workflows that use only the built-in `GITHUB_TOKEN`. The two inherited PyPI publish jobs are disabled.
- The installers (`docker/install.sh`, `install_i18n.sh`, `wrtinstall.sh`) generate random JWT signing keys, keep an existing compose file from this edition and otherwise download it (and `.env.example`) from this repository's `main` branch instead of the upstream one, and no longer make the data directory's `.env` or the credential master key readable by every account.
- Rotating `NEKRO_ADMIN_PASSWORD` now invalidates previously issued admin tokens; `NEKRO_SUPER_ACCESS_KEY` now actually takes effect when set.
- Both Dockerfiles point apt at `archive.debian.org` because the Debian 11 (bullseye) base image's packages are gone from the regular mirrors.

### Fixed
- Several security and robustness fixes found during review: credentials leaking through logs and upstream error text, tenant-visible endpoints that should have been platform-only, concurrent first-message user creation producing duplicate rows, and read-only members seeing an empty page where they should see a "no access" message.
- `backend-tests.yml` was an invalid workflow file (duplicate `NO_PROXY` / `no_proxy` keys in `env`); two tests that only passed on Windows by accident now pass on Linux as well.

### Documentation
- `README.md`, `README_en.md` and `NOTICE` state that telemetry and the NekroAI cloud service are **on by default** (inherited from upstream, behaviour unchanged), what is reported, and how to turn it off, including a known limitation of the switch when no cloud API key is configured.
- The user interface now shows "Built on the NekroAgent project" with a clickable link to the upstream project (login page and sidebar footer).

### Compatibility and deployment notes
- Upgrading from an earlier build: the first start after the upgrade signs everybody out once if you let the installer write JWT keys for a Docker deployment; admins sign in again after a password rotation.
- The new default sandbox image name only applies to newly generated configuration; set `SANDBOX_IMAGE_NAME` yourself on an existing deployment.
- Back up the model-group credential key material (the `NEKRO_CREDENTIAL_SECRET_KEY` environment variable or `credential_secret.key`) separately from database backups; without it the stored API keys cannot be decrypted.
