# Changelog

Version numbers here are this derivative edition's own (`FORK_VERSION` in `nekro_agent/tools/common_util.py`). The `version` in `pyproject.toml` is the upstream
NekroAgent version this edition is based on (`2.3.3`) and is intentionally left untouched: it drives plugin-compatibility checks and update checks.

## v0.3.0 (2026-11-01)

### Added
- **Built-in plugin "Video Analysis"**: the AI can read and analyze videos. The new sandbox method `analyze_video(source, question)` takes a video from the chat, a sandbox path or an http(s) link, samples frames with `ffmpeg`, sends them to a vision model group in batches and returns the answer. Optionally, the audio track is sent to an OpenAI-compatible transcription endpoint (off by default, because the audio leaves your host). The plugin is off by default; enable it in the plugin page and pick a vision model group, or leave `VIDEO_MODEL_GROUP` empty to use the channel's primary or fallback group when it has vision.
- Conservative limits with hard ceilings: 64 MB, 10 minutes, 16 frames, 768 px long edge, 8 frames per request, 100 seconds in total, 2 concurrent jobs process-wide and 1 per instance. An instance can tighten these but not loosen them (the one exception is frames per request: smaller batches mean more requests, so an instance can only raise it); an instance cannot turn on scene detection or audio transcription while they are off globally. The total time is also capped at the sandbox timeout minus 10 seconds, so raise `SANDBOX_RUNNING_TIMEOUT` if you raise it.
- Video content is treated as untrusted: links go through the existing outbound protection on every redirect, and the actual peer is checked again after connecting. `ffmpeg` only reads local plain video containers from an isolated working directory that is always removed afterwards, with memory limits and its whole process tree killed on timeout. Text from the video or the models is sanitized, marked as untrusted, and not stored as a system message. Every upstream request appears in the usage ledger under the `plugin` scene.
- New `UntrustedToolResult` for plugin authors: return it from a sandbox method to hand external content to the model without it becoming a system message or passing through the sandbox.

### Security
- The outbound URL check now refuses IPv6 forms that lead to IPv4 ranges or are deprecated (`::/96`, `64:ff9b::/96`, `64:ff9b:1::/48`, `2002::/16`, `fec0::/10`). This also applies to chat image downloads and tenant endpoints.
- QQ media links are no longer written to logs with their `rkey`, neither in log messages nor in exception text, and log redaction now matches parameter names by whole word (parameters such as `author` or `design` are no longer masked by mistake).
- History lines of messages that are referenced, or shown with message ids, no longer lose the session token: tool results are always wrapped in an untrusted block.
- **`docker-guard` could be bypassed; please update if you use the hardened deployment.** Fixed: bind sources that reach outside the data directory through symbolic links; request bodies whose keys differ only in case from the documented ones; the flat `HostConfig` form of older Docker Engines; request bodies on `start` / `restart`; restart policies on containers with bind mounts; `MaskedPaths` / `ReadonlyPaths`, which unmask `/proc` and `/sys` paths; and several other `HostConfig` fields NekroAgent never sends. Request bodies are now checked against exact-key whitelists and the guard forwards the structure it checked. These fixes have not been exercised against a real Docker daemon yet, only against test doubles. See [`docs/docker-guard.md`](docs/docker-guard.md).

### Changed
- The image now includes `ffmpeg`, which makes it larger. Without Docker, install `ffmpeg` and `ffprobe` yourself if you want the plugin; nothing else depends on them.
- With the plugin on, OneBot video messages are no longer dropped: the model sees `[Video: message:<id>]`, and the link is fetched only when the video is analyzed. With the plugin off, nothing changes, except that a lone video sent in a private chat is still recorded but no longer triggers an empty reply from the AI (before, the synthesized "mention" left in the message did). A recalled video cannot be analyzed.

### Upgrade notes
- **Hardened deployment: add the data directory to the guard sidecar.** In `docker/docker-compose-x-hardened.yml`, `nekro_docker_guard` now needs `- ${NEKRO_DATA_DIR}:${NEKRO_DATA_DIR}:ro` under `volumes`. If you keep your own copy of that file, add this line when you upgrade. Without it the guard refuses every mount under the data directory, so the sandbox, CC workspaces and NapCat will not start.
- **The Video Analysis plugin is off by default and needs `ffmpeg`.** The Docker images include it. Video links are downloaded without any proxy (`HTTP_PROXY` and similar variables are ignored), so a host that can only reach the internet through a proxy cannot download links from the chat.
- Hardened deployment: existing NapCat containers are switched to the restart policy `no` at start-up and kept alive by NekroAgent instead. If that migration fails, the log names the container; delete and re-create it from the console.

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
