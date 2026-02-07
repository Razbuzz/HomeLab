# HomeLab

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

    # Jackett Configuration
    JACKETT_FOLDER=jackett
    JACKETT_TV_FOLDER=tv
    JACKETT_MOVIES_FOLDER=movies
    JACKETT_PORT=9117

    # jellyseerr Configuration
    JELLYSEERR_FOLDER=jellyseerr
    JELLYSEERR_PORT=5055

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
  restart              Restart the stack (runs down then up)
  logs                 View logs for all services (Ctrl+C to exit)
  pull                 Just pull images (useful for pre-loading updates)
  clean                Clean up dangling images to free space
  setup                Run initial setup scripts (directories & system service)
  help                 Show this help message
```

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

### Jellyseerr

Jellyseerr is a web application designed to manage and automate requests for media content in your home media server setup. It integrates seamlessly with Sonarr and Radarr, allowing users to request movies and TV shows easily. With a user-friendly interface, Jellyseerr provides features such as user authentication, notifications, and a comprehensive dashboard to track request statuses.

> To configure Jellyseerr, navigate to http://your-server-ip:5055 in your browser and follow the setup wizard. Here is a reference guide for the setup steps:
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
> After setup, you'll see the Jellyseerr homepage. If needed, correct your Plex URL by going to Settings > Plex and updating the Web App URL to:
> - `http://your-server-ip:32400/web` (if not using a reverse proxy)
> - `https://your-domain-name/web` (if using a reverse proxy)
>
> Jellyseerr is now ready to use!

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

### Dashboard

The custom dashboard serves as the home page for your HomeLab, providing quick access to all services.

#### Configuration
1. Navigate to http://your-server-ip:80 in your browser
2. The dashboard is built from the `home-server-dashboard` project
3. Modify the dashboard configuration by editing files in `./apps/home-server-dashboard`
4. Update service URLs and links to match your setup and network configuration


## Sources

- [rickklaasboer/how-to-setup-plex-with-sonarr-radarr-jackett-overseerr-and-qbittorrent-using-docker](https://gist.github.com/rickklaasboer/b5c159833ff2971fccd32296d8ba2260)

## License

This project is provided as-is for personal use. Please ensure you comply with all applicable laws and regulations regarding media content and streaming in your jurisdiction.

## Contributing

Contributions are welcome! Feel free to submit issues or pull requests to improve this project.

## Acknowledgments

Special thanks to [rickklaasboer](https://gist.github.com/rickklaasboer) for the foundational setup guides that inspired this project.

