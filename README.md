# HomeLab

[![CI](https://github.com/Razbuzz/HomeLab/actions/workflows/ci.yml/badge.svg)](https://github.com/Razbuzz/HomeLab/actions/workflows/ci.yml)

This repository contains infrastructure-as-code (Docker Compose) for a robust, automated home media server and lab environment. The stack is designed to run on a Linux environment (Ubuntu recommended) and features a fully automated media acquisition pipeline, hardware-accelerated streaming, and network storage management.

## Prerequisites
- Install Ubuntu Server on your machine [see here](https://ubuntu.com/download/server).
- Install Docker Engine and Docker Compose [see here](https://docs.docker.com/desktop/setup/install/linux/).
- For optimal performance, use at least 1 TB of storage for media.
- A VPN or domain tunnel to make your HomeLab remotely accessible (optional, but strongly recommended). Consider using [Tailscale](https://tailscale.com/).

## Installation

1. Clone the repository:
    ```bash
    git clone https://github.com/Razbuzz/HomeLab.git /mnt/HomeLab
    ```
2. Edit the [`.env`](https://github.com/Razbuzz/HomeLab/blob/main/.env) file to match your preferences. **This must be done before proceeding to step 3.**
    ```yml
    # General Configuration
    DEFAULT_STORAGE_MOUNT=CHANGE_TO_STORAGE_PATH
    DEFAULT_APPS_MOUNT=./apps
    DEFAULT_PUID=1000
    DEFAULT_GUID=1000
    DEFAULT_TIMEZONE=CHANGE_TO_YOUR_TIMEZONE
    DEFAULT_LAN_IP=CHANGE_TO_YOUR_LAN_IP

    # DashBoard Configuration
    DASHBOARD_PORT=80

    # plex Configuration
    PLEX_FOLDER=plex

    # qbittorrent Configuration
    QBITTORRENT_FOLDER=qbittorrent
    QBITTORRENT_UI_PORT=8080
    QBITTORRENT_TORRENTING_PORT=6881
    QBITTORRENT_DOWNLOADS_FOLDER=downloads
    QBITTORRENT_TV_FOLDER=tv
    QBITTORRENT_MOVIES_FOLDER=movies

    # filebrowser Configuration
    FILEBROWSER_FOLDER=filebrowser
    FILEBROWSER_UI_PORT=10000

    # samba Configuration
    SAMBA_PORT=445
    SAMBA_SHARE_NAME=Data
    SAMBA_USERNAME=CHANGE_TO_SAMBA_USERNAME
    SAMBA_PASSWORD=CHANGE_TO_SAMBA_PASSWORD

    # sonarr Configuration
    SONARR_FOLDER=sonarr
    SONARR_TV_FOLDER=tv
    SONARR_MOVIES_FOLDER=movies
    SONARR_PORT=8989

    # radarr Configuration
    RADARR_FOLDER=radarr
    RADARR_TV_FOLDER=tv
    RADARR_MOVIES_FOLDER=movies
    RADARR_PORT=7878

    # jacketts Configuration
    JACKETT_FOLDER=jackett
    JACKETT_TV_FOLDER=tv
    JACKETT_MOVIES_FOLDER=movies
    JACKETT_PORT=9117

    # flaresolverr Configuration
    FLARESOLVERR_PORT=8191

    # seerr Configuration
    SEERR_FOLDER=seerr
    SEERR_PORT=5055
    SEERR_LOG_LEVEL=info

    # maintainerr Configuration
    MAINTAINERR_FOLDER=maintainerr
    MAINTAINERR_PORT=6246

    # couchdb Configuration
    COUCHDB_USER=CHANGE_TO_COUCHDB_USER
    COUCHDB_PASSWORD=CHANGE_TO_COUCHDB_PASSWORD
    COUCHDB_FOLDER=couch-db
    COUCHDB_PORT=5984

    # pi-hole Configuration
    PI_HOLE_FOLDER=pi-hole
    PI_HOLE_UI_PORT=12120
    PI_HOLE_WEB_PASSWORD=CHANGE_TO_PI_HOLE_WEB_PASSWORD
    PI_HOLE_DEFAULT_DNS=1.1.1.1
    PI_HOLE_DNSMASQ_FOLDER=pi-hole-dnsmasq

    # ollama Configuration
    OLLAMA_FOLDER=ollama
    OLLAMA_PORT=11434
    OLLAMA_CONTEXT_LENGTH=65536
    OLLAMA_KEEP_ALIVE=24h
    OLLAMA_FLASH_ATTENTION=1
    OLLAMA_KV_CACHE_TYPE=q8_0

    # qdirstat Configuration
    QDirstat_FOLDER=qdirstat
    QDirstat_PORT=5800

    # hermes-agent Configuration (beta profile)
    HERMES_FOLDER=hermes
    HERMES_API_TIMEOUT=1800
    HERMES_DASHBOARD=1
    HERMES_DASHBOARD_PORT=9119
    HERMES_DASHBOARD_BASIC_AUTH_USERNAME=admin
    HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=CHANGE_TO_HERMES_DASHBOARD_PASSWORD
    HERMES_DASHBOARD_BASIC_AUTH_SECRET=CHANGE_TO_HERMES_DASHBOARD_SESSION_SECRET
    HERMES_OPENROUTER_API_KEY=CHANGE_TO_OPENROUTER_API_KEY
    ```
3. Run the setup script:
    ```bash
    make setup
    ```

## Basic Usage

```bash
make help
Usage: make [command]

Available commands:
  up                   Start the stack (pulls latest images & builds dashboard)
  down                 Stop the stack and remove containers
  up-beta              Start the stack including beta services (e.g. Hermes Agent)
  down-beta            Stop the stack including beta services
  restart              Restart the stack (runs down then up)
  logs                 View logs for all services (Ctrl+C to exit)
  pull                 Just pull images (useful for pre-loading updates)
  clean                Clean up dangling images to free space
  permissions          Fix ownership of app volumes (CouchDB, Maintainerr, Seerr)
  setup                Run initial setup scripts (directories & system service)
  backup               Create verified backup of .env and app data (BACKUP=<dir>)
  upgrade              Back up, merge .env, upgrade to TARGET=<ref> (default: latest tag)
  rollback             Restore HomeLab from backup BACKUP=<dir> (default: latest)
  lint                 Run yamllint and shellcheck (and hadolint if installed)
  validate             Validate docker-compose.yml with temporary placeholders when needed
  test                 Run pytest validation suite
  system-test          Run full-stack system test (make up + healthchecks + make down)
  check                Run all local quality checks (lint, validate, test)
  changelog            Generate CHANGELOG.md using git-cliff
  release-notes        Preview unreleased release notes via git-cliff
  help                 Show this help message
```

> **Tip:** All services define Docker `healthcheck` probes. Run `docker compose ps` (or `docker compose --profile '*' ps`) to inspect each container's live health status.

## Upgrading

HomeLab provides automated backup, non-destructive `.env` merging, live health verification, and automatic/manual rollback commands so upgrades never overwrite your secrets or persistent service data:

```bash
# Preview target tag, disk headroom, and .env merge without making changes:
bash scripts/upgrade.sh --dry-run

# Create a verified backup, merge .env, upgrade to the latest release, and verify healthchecks:
make upgrade

# Upgrade to a specific release tag or branch:
make upgrade TARGET=v1.5.0

# Create a standalone backup anytime:
make backup

# Roll back manually to the latest (or a specific) verified backup:
make rollback
make rollback BACKUP=/path/to/backup
```

- **Upgrading from an older commit (pre-`v1.5.0`, e.g. `ec3a6250`)?** Follow the step-by-step bootstrap instructions in [docs/upgrading.md](docs/upgrading.md) to extract the upgrade scripts into `/tmp/hl-up/` and upgrade cleanly without `git pull` conflicts on `.env`.
- See [docs/upgrading.md](docs/upgrading.md) for backup contents/exclusions, `.env` merge rules, and automatic rollback details.

### From v1.3.0 or earlier

CouchDB now runs as `${DEFAULT_PUID}:${DEFAULT_GUID}` instead of `5984:5984`. Existing `couchdb-data` and `couchdb-etc` bind-mount directories created by earlier versions (owned by `5984:5984` or `root`) will prevent the container from starting or writing data until ownership is updated. (`make permissions` also ensures expected non-root ownership for Seerr `1000:1000`, since Seerr runs rootless as `node` and ignores `PUID`/`PGID`.)

Run once before (or as part of) starting the updated stack:

```bash
make permissions
```

`make up` also invokes `make permissions` automatically on every start (including via the `homelab` systemd service).

Verify CouchDB is healthy after starting:

```bash
docker logs couchdb-for-ols
curl -fsS http://<your-server-ip>:5984/_up
```

A healthy instance responds with `{"status":"ok","seeds":{}}`.

## Configuration 
### Requirements

1. A fully configured `.env` file.
2. Patience for initial setup and configuration.
3. Run the stack with `make up`.

### Tailscale

1. Create a Tailscale account at [tailscale.com](https://tailscale.com/).
2. Register your Linux server to your network by following the [installation guide](https://tailscale.com/kb/1038/linux-server/).
3. Enable subnet routing to bypass Plex LAN restrictions and access your services remotely:
    - Follow [Tailscale's subnet routing documentation](https://tailscale.com/kb/1019/subnet-routing/).
    - Accept the advertised routes on any client device you want to use remotely.
4. (Optional) Enable [MagicDNS](https://tailscale.com/kb/1081/magicdns/) for easier access using device names instead of IP addresses.

**Resources:**
- [Tailscale Getting Started Guide](https://tailscale.com/kb/1017/install/)

### qBittorrent

qBittorrent is a free, open-source torrent client that allows you to download and manage files over the BitTorrent network. It's lightweight, cross-platform, and designed to be a reliable alternative to other torrent clients.

> To configure qBittorrent, navigate to http://your-server-ip:8080 in your browser. The default username is `admin` and password is `adminadmin`. If you plan to expose this to the internet, change these credentials immediately. Beyond that, minimal configuration is required.

[Source](https://gist.github.com/rickklaasboer/b5c159833ff2971fccd32296d8ba2260#configuring-qbittorrent)

### Flaresolverr

Flaresolverr is a proxy service that solves Cloudflare CAPTCHA challenges. It's essential for accessing indexers protected by Cloudflare that would otherwise fail in Jackett.

> Flaresolverr runs as a background service without a web UI. It listens on port `8191` and integrates directly with Jackett.
>
> **To enable Flaresolverr for Jackett indexers:**
> 1. Navigate to http://<your-server-ip>:9117 (Jackett)
> 2. Click on an indexer that requires Cloudflare bypass
> 3. Look for the **Flaresolverr** option and enable it
> 4. Set the URL to: `http://flaresolverr:8191/`
> 5. Save the configuration and test the connection
>
> Flaresolverr will automatically handle Cloudflare challenges. It's particularly useful for indexers like 1337x and other sites protected by Cloudflare.
>
> **Tip:** Only enable Flaresolverr for indexers that actually need it, as it uses additional system resources.

### Jackett

Jackett is an API aggregator that allows applications like Sonarr, Radarr, and Lidarr to use a wide variety of torrent and Usenet indexers. It acts as a translator between your apps and indexers, allowing you to search for content across multiple sources from a single interface.

> To configure Jackett, navigate to http://<your-server-ip>:9117 in your browser. Do not expose Jackett to the internet, as it's unnecessary and exposes you to legal risks. The provided Docker image includes a default Jackett installation, so minimal configuration is needed.
>
> Add your preferred indexers by clicking the **+ Add indexer** icon in the top right corner. For indexer selection, refer to the [source documentation](https://gist.github.com/rickklaasboer/b5c159833ff2971fccd32296d8ba2260#configuring-jackett) for recommendations.
>
> **To add indexers to Sonarr and Radarr**, go to Settings > Indexers and click the **+** icon. Select **Torznab** and click **Next**. Enter the following information:
>
> - **Name**: The name of the indexer
> - **Enable RSS**: Yes
> - **Enable Automatic Search**: Yes
> - **Enable Interactive Search**: Yes
> - **URL**: `http://jackett:9117/api/v2.0/indexers/<indexer_name>/results/torznab/`
> - **API Key**: Found in the top-right corner of the Jackett web interface
> - **Categories**: Select relevant categories
> - **Anime Categories**: Select relevant categories (optional)
> - **Anime standard format search**: Optional, can be left disabled
> - **Tags**: Optional, can be left empty
>
> Click **Test** to verify the configuration (you should see a green checkmark). If successful, click **Save**. Repeat for each indexer you wish to add.
>
> **For indexers requiring Cloudflare bypass:** Enable the Flaresolverr option in the indexer settings and set the URL to `http://flaresolverr:8191/`.

[Source](https://gist.github.com/rickklaasboer/b5c159833ff2971fccd32296d8ba2260#configuring-jackett)

### Sonarr

Sonarr is a PVR (Personal Video Recorder) for TV shows that automates the process of finding, downloading, and organizing episodes. It works with torrent or Usenet clients like qBittorrent to keep your TV library up to date automatically.

> To configure Sonarr, navigate to http://<your-server-ip>:8989 in your browser. The Docker image does not enable authentication by default. For local-only access, this is acceptable; for remote access, enable authentication by going to Settings > General and selecting either Forms (Login Page) or Basic (Browser Popup). A restart is required for authentication changes to take effect.
>
> **To add a download client**, go to Settings > Download Clients and click the **+** icon. Select **qBittorrent** and click **Next**. Enter the following information:
>
> - **Name**: qBittorrent
> - **Host**: qbittorrent
> - **Port**: 8080
> - **Username**: admin
> - **Password**: adminadmin
> - **Category**: tv
> - **SSL**: Enable if using SSL
>
> Click **Test** to verify the connection (you should see a green checkmark). If successful, click **Save**.

[Source](https://gist.github.com/rickklaasboer/b5c159833ff2971fccd32296d8ba2260#configuring-sonarr)

### Radarr

Radarr is a movie collection manager for Usenet and BitTorrent users. It automates the process of finding, downloading, and organizing movies, ensuring your library is always up to date. Radarr works seamlessly with various download clients and allows you to set up custom quality profiles and notifications for new releases.

> To configure Radarr, navigate to http://your-server-ip:7878 in your browser. Similar to Sonarr, you can enable authentication for security if you plan remote access. To add a download client, go to Settings > Download Clients and click the **+** icon. Select **qBittorrent** and enter the following information:
>
> - **Name**: qBittorrent
> - **Host**: qbittorrent
> - **Port**: 8080
> - **Username**: admin
> - **Password**: adminadmin
> - **Category**: movies
>
> Click **Test** to verify the connection is successful, then click **Save**. You can also set up custom quality profiles to manage your movie library effectively.

[Source](https://gist.github.com/rickklaasboer/b5c159833ff2971fccd32296d8ba2260#configuring-radarr)

### Seerr

Seerr is a web application designed to manage and automate requests for media content in your home media server setup. It integrates seamlessly with Sonarr and Radarr, allowing users to request movies and TV shows easily. With a user-friendly interface, Seerr provides features such as user authentication, notifications, and a comprehensive dashboard to track request statuses.

> **Access:** Navigate to `http://<your-server-ip>:5055` (or `${SEERR_PORT}` if customized in `.env`) in your browser and follow the setup wizard.
>
> **Config directory permissions & logging:**
> - The official Seerr image (`ghcr.io/seerr-team/seerr`) runs rootless as the `node` user (`1000:1000`) and does **not** honor `PUID`/`PGID` environment variables (see [Why Seerr Doesn't Support PUID/PGID](https://docs.seerr.dev/blog/why-seerr-doesnt-support-puid-pgid/) and [Seerr Docker docs](https://docs.seerr.dev/getting-started/docker/)).
> - Ensure `${DEFAULT_APPS_MOUNT}/${SEERR_FOLDER}/config` is owned by `1000:1000`:
>   ```bash
>   mkdir -p "${DEFAULT_APPS_MOUNT}/${SEERR_FOLDER}/config"
>   docker run --rm -v "${DEFAULT_APPS_MOUNT}/${SEERR_FOLDER}/config:/data" alpine chown -R 1000:1000 /data
>   ```
> - Logging defaults to `info` via `SEERR_LOG_LEVEL` in `.env` (set to `debug` when troubleshooting).
> - The container always listens internally on port `5055`; `SEERR_PORT` in `.env` controls only the published host port.
>
> **Migrating from Jellyseerr:**
>
> Follow the official [Seerr Migration Guide](https://docs.seerr.dev/migration-guide/) when upgrading an existing Jellyseerr setup:
> 1. Stop the stack: `make down`
> 2. Update `.env`: rename `JELLYSEERR_FOLDER` → `SEERR_FOLDER` (default `seerr`) and `JELLYSEERR_PORT` → `SEERR_PORT` (default `5055`), and add `SEERR_LOG_LEVEL=info`.
> 3. Copy your existing Jellyseerr config directory to Seerr's config path (preserving data as a backup):
>    ```bash
>    mkdir -p "${DEFAULT_APPS_MOUNT}/seerr"
>    cp -a "${DEFAULT_APPS_MOUNT}/jellyseerr/config" "${DEFAULT_APPS_MOUNT}/seerr/config"
>    ```
> 4. Ensure ownership is `1000:1000` so the rootless `node` user can read and write `/app/config`:
>    ```bash
>    docker run --rm -v "${DEFAULT_APPS_MOUNT}/seerr/config:/data" alpine chown -R 1000:1000 /data
>    ```
> 5. Start the stack: `make up` (Seerr automatically migrates your Jellyseerr database and settings on first startup).
>
> Here is a reference guide for the initial setup steps:
>
> **Plex Setup:**
> 1. Sign in with your Plex account (the same one used to set up Plex)
> 2. Enter a server name (any name you prefer)
> 3. Click **Scan** next to the server section to find Plex servers on your network; select the local option
> 4. Enter the Plex port (default is 32400)
> 5. Enable SSL if using it
> 6. Click **Sync Libraries** to scan your Plex libraries (you should see two: one for TV and one for movies)
> 7. Select both libraries
> 8. Click **Start scan** to scan your libraries for media (this runs in the background)
> 9. Click **Continue**
>
> **Radarr Setup:**
> 1. Click **Add Server** and select Radarr
> 2. Check **Default server options** (if this is your only server)
> 3. Enter a server name
> 4. Enter the URL: `http://radarr`
> 5. Enter the port: `7878`
> 6. Enter the API key (found in Radarr under Settings > General > Security)
> 7. For Quality profile, choose your preferred profile
> 8. For Root folder, choose `/movies`
> 9. For Minimum Availability, choose **Released**
> 10. For External URL, enter `http://your-server-ip:7878` (or your reverse proxy URL)
> 11. Check **Enable automatic search**
> 12. Click **Test** to verify the connection (should show a green checkmark)
> 13. Click **Save**
>
> **Sonarr Setup:**
> 1. Click **Add Server** and select Sonarr
> 2. Check **Default server options** (if this is your only server)
> 3. Enter a server name
> 4. Enter the URL: `http://sonarr`
> 5. Enter the port: `8989`
> 6. Enter the API key (found in Sonarr under Settings > General > Security)
> 7. For Quality profile, choose your preferred profile
> 8. For Root folder, choose `/tv`
> 9. For Minimum Availability, choose **Released**
> 10. For External URL, enter `http://your-server-ip:8989` (or your reverse proxy URL)
> 11. Check **Enable automatic search**
> 12. Click **Test** to verify the connection
> 13. Click **Save**
>
> After setup, you'll see the Seerr homepage. If needed, correct your Plex URL by going to Settings > Plex and updating the Web App URL to:
> - `http://your-server-ip:32400/web` (if not using a reverse proxy)
> - `https://your-domain-name/web` (if using a reverse proxy)
>
> Seerr is now ready to use!

### Connecting Jackett to Sonarr & Radarr

To connect Jackett indexers to Sonarr and Radarr, follow these steps for each application:

#### Sonarr

1. Navigate to http://your-server-ip:8989 and go to **Settings > Indexers**
2. Click the **+** icon to add a new indexer
3. Select **Torznab** and click **Next**
4. Fill in the following details:
    - **Name**: The name of your indexer
    - **Enable RSS**: Yes
    - **Enable Automatic Search**: Yes
    - **Enable Interactive Search**: Yes
    - **URL**: `http://jackett:9117/api/v2.0/indexers/<indexer_name>/results/torznab/`
    - **API Key**: Found in the top-right corner of the Jackett web interface
    - **Categories**: Select TV show categories
5. Click **Test** to verify the connection (should show a green checkmark)
6. Click **Save**

#### Radarr

1. Navigate to http://your-server-ip:7878 and go to **Settings > Indexers**
2. Click the **+** icon to add a new indexer
3. Select **Torznab** and click **Next**
4. Fill in the following details:
    - **Name**: The name of your indexer
    - **Enable RSS**: Yes
    - **Enable Automatic Search**: Yes
    - **Enable Interactive Search**: Yes
    - **URL**: `http://jackett:9117/api/v2.0/indexers/<indexer_name>/results/torznab/`
    - **API Key**: Found in the top-right corner of the Jackett web interface
    - **Categories**: Select movie categories
5. Click **Test** to verify the connection
6. Click **Save**

Repeat these steps for each indexer you've added to Jackett.

### Plex

Plex is a media server that allows you to organize, manage, and stream your personal media collection (movies, TV shows, music, photos) to any device, anywhere. It provides a Netflix-like experience for your own content with hardware-accelerated streaming support.

#### Configuration
1. Navigate to http://your-server-ip:32400/web in your browser
2. Sign in or create a Plex account
3. Add your libraries:
    - Click **Add Library**
    - Select **Movies** or **TV Shows**
    - Point to `/data` (downloads folder) or your media storage location
4. Enable hardware transcoding (already configured in docker-compose):
    - Go to **Settings > Remote Access** and enable remote access
    - Go to **Settings > Transcoder** and verify GPU acceleration is enabled
5. (Optional) Configure remote access with Tailscale or a reverse proxy for secure external access

### FileBrowser

FileBrowser is a simple web-based file manager that allows you to browse, upload, and manage files on your server through a user-friendly interface.

#### Configuration
1. Navigate to http://your-server-ip:10000 in your browser
2. Default credentials are admin/admin (change these immediately for security)
3. Go to **Settings** to configure:
    - Change admin password
    - Set file permissions and upload limits
    - Configure allowed file types if needed
4. The interface allows you to browse and manage files in `/srv` (your storage mount)

### Samba

Samba is a network file sharing protocol that allows you to access your storage from Windows, macOS, and Linux machines on your local network, similar to a network drive.

#### Configuration
1. On your client machine, connect to the network share:
    - **Windows**: `\\your-server-ip\Data`
    - **macOS/Linux**: `smb://your-server-ip/Data`
2. Enter credentials:
    - Username: Value from `SAMBA_USERNAME` in `.env`
    - Password: Value from `SAMBA_PASSWORD` in `.env`
3. Your storage mount will now appear as a network drive

### Maintainerr

Maintainerr is a tool for managing and cleaning up your media library, removing duplicate files, and maintaining library health by identifying and removing unwanted content.

#### Configuration
1. Navigate to http://your-server-ip:6246 in your browser
2. Follow the setup wizard to connect to your Plex server
3. Configure your preferences:
    - Set rules for automatic library cleanup
    - Configure notification preferences
    - Set up scheduled maintenance tasks

### CouchDB

CouchDB is a NoSQL database that stores data in JSON format, useful for applications that need document-based storage and replication capabilities. It is used for Obsidian live-sync ([see here](https://github.com/vrtmrz/obsidian-livesync)).

#### Configuration
1. Navigate to http://your-server-ip:5984/_utils in your browser
2. Log in with credentials from `.env`:
    - Username: `COUCHDB_USER`
    - Password: `COUCHDB_PASSWORD`
3. Create databases as needed for your applications
4. Most applications will handle CouchDB connection configuration internally

> **Upgrade note (from v1.3.0 or earlier):** CouchDB now runs as `${DEFAULT_PUID}:${DEFAULT_GUID}` instead of `5984:5984`. Run `make permissions` (or `make up`) once to repair directory ownership on `couchdb-data` and `couchdb-etc`, then verify with `docker logs couchdb-for-ols` and `curl -fsS http://<your-server-ip>:5984/_up`.

### Pi-hole

Pi-hole is a network-wide ad blocker and DNS server that protects all devices on your network from ads and malicious websites by filtering DNS requests.

#### Configuration
1. Navigate to http://your-server-ip:12120/admin in your browser
2. Log in with password from `PI_HOLE_WEB_PASSWORD` in `.env`
3. Configure your settings:
    - **Settings > DNS**: Add upstream DNS servers (default is 1.1.1.1)
    - **Settings > DHCP**: Enable DHCP server if desired (optional)
    - **Adlists**: Add blocklists for ad filtering
4. On your devices or router:
    - Set DNS to your-server-ip (typically 10.0.0.32 based on your `.env`)
    - Alternatively, configure DHCP to distribute Pi-hole as DNS server
5. Monitor blocked requests in the dashboard

### Ollama

Ollama is an open-source tool for running large language models (LLMs) locally on your machine. It allows you to run and interact with various AI models like Llama, Mistral, and others without relying on external APIs.

#### Configuration
1. Ollama runs as a background service on port `11434`
2. To interact with Ollama, you can use the Ollama CLI or API:
   - Pull a model: `docker exec -it ollama ollama pull llama2`
   - Run a model: `docker exec -it ollama ollama run llama2`
   - List models: `docker exec -it ollama ollama list`
3. For web-based interfaces, you can integrate with tools like Open WebUI or use the API directly
4. Models are stored in the `./apps/ollama` directory for persistence
5. GPU acceleration is enabled if an NVIDIA GPU is available
6. Context length (`65536`), keep-alive (`24h`), flash attention (`1`), and KV cache type (`q8_0`) are configurable in `.env` for long-context agents like Hermes

### Hermes Agent (Beta)

Hermes Agent is an opt-in autonomous AI agent (`nousresearch/hermes-agent:v0.23.3`) under the Docker Compose `beta` profile. Normal `make up` skips it; use `make up-beta` and `make down-beta` to run it alongside Ollama.

- **Access URL (Web Dashboard):** `http://<DEFAULT_LAN_IP>:9119` (LAN-only, fail-closed Basic Auth via `HERMES_DASHBOARD_BASIC_AUTH_*`; set `HERMES_DASHBOARD=0` to run headless).
- **CLI Usage:** Interact directly inside the running container with `docker exec -it hermes-agent hermes`.
- **LLM Setup:** Uses local Ollama (`http://ollama:11434/v1` at 64K context) by default, with optional per-turn OpenRouter cloud fallback (`HERMES_OPENROUTER_API_KEY` + `fallback_providers` in `config.yaml`).
- **Optional External Messaging Gateways:** Telegram, Discord, Slack, and other supported messaging platforms can be connected via outbound gateway tokens/config (see the [Hermes Messaging Docs](https://hermes-agent.nousresearch.com/docs/user-guide/messaging)).
- **Setup & Security Guide:** Copy [`configs/hermes/config.yaml.example`](configs/hermes/config.yaml.example) to `${DEFAULT_APPS_MOUNT}/${HERMES_FOLDER}/config.yaml`, populate `.env`, pull an Ollama model, and run `make up-beta`. See [docs/hermes-agent.md](docs/hermes-agent.md) for VRAM-tier model picks, optional external messaging gateways, and hardening details.

### QDirStat

QDirStat is a graphical disk usage analyzer that helps you visualize and manage disk space usage on your storage mount.

#### Configuration
1. Navigate to http://your-server-ip:5800 in your browser
2. The interface will display disk usage statistics for your storage directory (`DEFAULT_STORAGE_MOUNT`)
3. Use the GUI to explore directories, identify large files/folders, and clean up space as needed

### Dashboard

The custom dashboard serves as the home page for your HomeLab, providing quick access to all services.

#### Configuration
1. Navigate to http://your-server-ip:80 in your browser
2. The dashboard is built from the `home-server-dashboard` project
3. Modify the dashboard configuration by editing files in `./apps/home-server-dashboard`
4. Update service URLs and links to match your setup and network configuration


## Releases

Changelogs are generated from [Conventional Commits](https://www.conventionalcommits.org/) using [`git-cliff`](https://git-cliff.org/) (`cliff.toml`). See [CHANGELOG.md](CHANGELOG.md) for the full release history.

- Preview unreleased changes locally: `make release-notes`
- Regenerate `CHANGELOG.md`: `make changelog`
- Pushing a version tag (`v*.*.*`) triggers the [Release](.github/workflows/release.yml) workflow to publish a GitHub Release automatically.

## Sources

- [rickklaasboer/how-to-setup-plex-with-sonarr-radarr-jackett-overseerr-and-qbittorrent-using-docker](https://gist.github.com/rickklaasboer/b5c159833ff2971fccd32296d8ba2260)

## Project Agents

AI coding assistants and maintainers working on this repository should start at [`AGENTS.md`](AGENTS.md) and review the role playbooks in [`.agents/`](.agents/README.md).

## License

This project is provided as-is for personal use. Please ensure you comply with all applicable laws and regulations regarding media content and streaming in your jurisdiction.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for the branching model, Conventional Commits guidelines, environment/documentation sync rules, and how to run local quality checks (`make check`) as well as the full-stack end-to-end verification (`make system-test`, which runs automatically in the CI `System Test (make up)` stage). See [docs/BACKLOG.md](docs/BACKLOG.md) for planned features and roadmap items.

## Contributing

Contributions are welcome! Feel free to submit issues or pull requests to improve this project. See [CONTRIBUTING.md](CONTRIBUTING.md) for details.

## Acknowledgments

Special thanks to [rickklaasboer](https://gist.github.com/rickklaasboer) for the foundational setup guides that inspired this project.

