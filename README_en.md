# NekroAgent derivative edition

[简体中文](./README.md)

> **This is an unofficial derivative edition.** It is modified from [KroMiose/nekro-agent](https://github.com/KroMiose/nekro-agent) (NekroAgent) and has no affiliation with, or endorsement from, the NekroAgent maintainers.
> **Built on the NekroAgent project** — upstream project: <https://github.com/KroMiose/nekro-agent>.
>
> - Version: **0.3.0** (this edition's own version number; the upstream version it is based on is `2.3.3`)
> - License: the upstream [Nekro Agent Open Source License V1.1](./LICENSE) (modified from Apache License 2.0); see "License" below and [`NOTICE`](./NOTICE)
> - Changes: [`CHANGELOG.md`](./CHANGELOG.md)

## What this is

NekroAgent is an AI chat and agent-execution bot: it prompts a large language model to write code, runs that code in a container sandbox, and talks to the real environment over RPC.
It supports OneBot v11 (QQ), Discord, Telegram, Minecraft, Bilibili Live, WeChat, email, SSE+SDK and more, and comes with a plugin system and a WebUI.
The full introduction, documentation and community of the upstream project are at <https://github.com/KroMiose/nekro-agent>.

This derivative edition adds capabilities for running several bots in one process and handing them to several customers.

## Main differences from upstream

- **Multi-instance**: one process runs several logical bot instances (one instance = one bot account) that share the heavy resources; chat keys, configuration, sandboxes and memory carry the instance dimension.
- **Console and multi-tenancy**: three roles (platform operator / tenant admin / read-only member); instances, containers, announcements, offline alerts, quotas and concurrency limits are managed in one place.
- **Isolation and identity**: affection and memory are isolated by "memory domain + person + persona" and can be shared across instances by configuration; identity aliases, merges and domain joins have an approval flow.
- **Usage metering**: one ledger row per upstream LLM request, aggregated by instance / tenant / model group, viewable at `/console/usage`.
- **Hardening**: model-group API keys encrypted at rest; startup audit of JWT keys and the admin password, with old tokens invalidated when the password changes; log and upstream-error redaction; sandbox and CC-workspace network isolation; optional `docker.sock` hardening ([`docs/docker-guard.md`](./docs/docker-guard.md)).
- **Video reading and analysis** (built-in plugin, off by default): lets the AI read videos from the chat, sandbox files or links; see "Video Analysis plugin" below.
- **Also**: a favourability soft reply gate, Chinese tokenization for memory and the knowledge base, NapCat container ownership and orphan reclaim, regression tests against a real PostgreSQL, and CI.

## Quick deployment (Docker)

Requirements: a Linux host, Docker (with Compose v2), `bash`, `curl`.

```bash
git clone https://github.com/Akiyo-dayo/Nekro-Agent-ByAkiyo.git nekro-agent
cd nekro-agent/docker
sudo -E bash install.sh --with-napcat
```

**Run the script from this repository's `docker/` directory.** When it finds this repository's compose files next to it, it uses them;
otherwise it downloads the compose file and `.env.example` from **this repository's** `main` branch (`https://raw.githubusercontent.com/Akiyo-dayo/Nekro-Agent-ByAkiyo/main/docker`).
You can also use the one-click command (also served from this repository):

```bash
sudo -E bash -c "$(curl -fsSL https://raw.githubusercontent.com/Akiyo-dayo/Nekro-Agent-ByAkiyo/main/docker/install.sh)" - --with-napcat
```

**Do not use the one-click `curl` command from the upstream README**: it fetches the upstream compose file and images, which are not this edition's.

The installer generates random access tokens, an admin password and JWT signing keys and writes them to the `.env` in the data directory (`~/srv/nekro_agent` by default).
**Read [`docs/operations.md`](./docs/operations.md) before going to production**: it covers the requirements for each credential, the model-group key encryption key and backup caveats, and the logging and diagnostics switches (the page is in Chinese).

### Images

Images are published to the GitHub Container Registry (GHCR), built and pushed by the repository's own CI with the built-in `GITHUB_TOKEN`:

| Image | Purpose |
|---|---|
| `ghcr.io/akiyo-dayo/nekro-agent-byakiyo` | the main application |
| `ghcr.io/akiyo-dayo/nekro-agent-byakiyo-sandbox` | the sandbox (pulled on the host by the installer) |

Tags:

- **version number** (e.g. `0.3.0`) and `latest`: built when a `v*.*.*` tag is pushed, suitable for production;
- `preview` / `preview-<short commit SHA>`: built on every `main` update, includes the latest changes, suitable for testing.

```bash
docker pull ghcr.io/akiyo-dayo/nekro-agent-byakiyo:latest
docker pull ghcr.io/akiyo-dayo/nekro-agent-byakiyo-sandbox:latest
```

Sandbox containers are created by the application through the Docker API, and its automatic pull **carries no credentials**, so pull the sandbox image on the host first and again whenever you update it.

### Other ways to deploy

- Several deployments on one host: [`docs/multi-deploy-same-host.md`](./docs/multi-deploy-same-host.md) (Chinese)
- A stricter `docker.sock` boundary: [`docs/docker-guard.md`](./docs/docker-guard.md) (Chinese)
- Bare-metal / development setup, plugin and adapter development: see [`CONTRIBUTING.md`](./CONTRIBUTING.md), [`docs/`](./docs/) and the upstream [documentation center](https://doc.nekro.ai) (for image names and script addresses, this page wins over upstream documents)

## Video Analysis plugin (off by default)

The built-in plugin "Video Analysis" lets the AI watch videos: it samples frames with `ffmpeg`, sends them to a vision model group in batches and returns the answer to the question; optionally the audio track goes to an OpenAI-compatible transcription endpoint. In short:

- **Off by default.** Enable it on the plugin page and pick a model group with vision (`VIDEO_MODEL_GROUP`; when empty, the channel's primary and then fallback group are tried).
- **Needs `ffmpeg` / `ffprobe`.** The Docker image includes them; without Docker, install them yourself. Without them a call to the plugin fails; nothing else is affected.
- **Audio transcription is off by default**: the audio leaves your host when it is on, and a tenant instance cannot turn it on while it is off globally.
- **Conservative limits with hard ceilings**: 64 MB, 10 minutes, 16 frames, 768 px long edge, 8 frames per request, 100 seconds in total; 2 concurrent jobs process-wide and 1 per instance. An instance can tighten the limits but not loosen them (one exception: frames per request, because smaller batches mean more requests, so an instance can only raise it). The total time is capped at the sandbox timeout minus 10 seconds.
- **Video content is untrusted**: link downloads go through the existing outbound protection (every redirect is checked, and the connected peer is checked again), `ffmpeg` reads only local plain video containers in an isolated working directory with memory limits, and text from the video or the models is sanitized and handed to the main model marked as untrusted, never as a system message.
- **Video downloads do not use a proxy**: the HTTP client that downloads video links ignores `HTTP_PROXY` / `HTTPS_PROXY` and similar variables and always connects directly (through a proxy the connected peer is the proxy, which the outbound protection cannot check). If the host can only reach the internet through a proxy, video links in the chat will fail to download; put the video in the sandbox shared directory and analyze that instead.
- With the plugin on, OneBot video messages are no longer dropped: the model sees `[Video: message:<id>]` and the link is fetched only when the video is analyzed; a recalled video cannot be analyzed. With the plugin off nothing changes: a lone video in a group is dropped, and a lone video in a private chat is still recorded but no longer triggers an empty reply.

## Telemetry and cloud services (on by default)

Inherited from upstream and **not changed by this edition**: the setting `ENABLE_NEKRO_CLOUD` (shown as "Enable NekroAI cloud service") **defaults to `true`**. While it is on:

- the application sends a usage report **every hour** to the NekroAI cloud service at `https://cloud.nekro.ai`. That service is operated by the upstream project, not by this derivative edition; the admin UI's "check for updates" also contacts the same domain;
- the report (see `nekro_agent/systems/cloud/` and `nekro_agent/tools/telemetry_util.py`) contains: an instance ID (a SHA-256 of the hostname, platform, machine ID, CPU count, total memory and the list of environment-variable names, among others), the upstream version number, whether it runs in Docker, system information (including the **hostname**, platform, Python version and CPU architecture), and **database-wide totals** (with several instances / tenants, the sum over all of them) plus the previous hour's numbers: users, channels, messages (group / private), sandbox executions with their stop-type breakdown, average and maximum execution time, active users / channels. It does **not** contain chat content, code content or credentials;
- with a NekroAI cloud API key you can also use the cloud plugin and persona sharing features.

**How to turn it off:**

1. Set `ENABLE_NEKRO_CLOUD` to `false`: in the web UI under "Settings -> Basic settings -> Enable NekroAI cloud service", in the setup wizard on first deployment, or in `<data directory>/configs/nekro-agent.yaml`.
2. ⚠️ **Known limitation**: in the current implementation, **when no NekroAI cloud API key is configured, the telemetry report does not check that switch** (`nekro_agent/systems/cloud/api/telemetry.py`). To stop the reports for certain, also set the environment variable `NEKRO_NEKRO_CLOUD_API_BASE_URL` to an unreachable address (for example `http://127.0.0.1:9`; note the two `NEKRO_` prefixes: the application prepends `NEKRO_` once more to the variable name), or block `cloud.nekro.ai` at your egress firewall. Doing so also disables the features that depend on the cloud service (cloud plugins, persona sharing).

## License

This project keeps the upstream **Nekro Agent Open Source License V1.1** (modified from Apache License 2.0; the license wins where they conflict). **Read [`LICENSE`](./LICENSE) in full before using, modifying or redistributing.** The obligations most relevant to redistribution (the text of `LICENSE` is authoritative; this is only a pointer):

- redistribution (commercial included) must carry a **complete copy** of `LICENSE` and keep copyright notices, trademark information and attribution marks;
- you must state clearly that the work is **built on the NekroAgent project** and give the original project link; keep the "built on NekroAgent" mark and a hyperlink to the upstream project in the user interface, and do not downplay the original project's contribution;
- when offering commercial content creation or real-time interactive services to the public (live streaming, virtual streamers, public chatbots, etc.), show "Powered by NekroAgent" prominently with the upstream source address;
- **large-scale commercial use** (all instances operated by you and your affiliates exceed 1000 channels in total, or 50000 registered users in total) requires prior written authorization from the upstream maintainers, and must not be split across instances or entities to avoid the limit; contact details are in `LICENSE`;
- when providing support or deployment services, tell the customer that the supported project is NekroAgent with its official source address, and never imply that the project is your own development or that you are associated with the maintainers;
- the project generates and executes code with large language models; assess the risks yourself and inform end users (`LICENSE`, part 1, section 3);
- **changes to the original files** are described in the "Statement of changes" of [`NOTICE`](./NOTICE) and in [`CHANGELOG.md`](./CHANGELOG.md). This repository has no per-commit history (it is exported as a whole from the maintainers' development repository), so to identify exactly which files were modified, compare this tree with the upstream `2.3.3` release.

This is not legal advice and does not replace `LICENSE`.

## Acknowledgements

Thanks to KroMiose, the author of NekroAgent, and all upstream contributors. The upstream contributors are listed at <https://github.com/KroMiose/nekro-agent/graphs/contributors>.
