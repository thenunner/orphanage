# 🗂️ Orphanage - Torrent File Management

**Find and manage orphaned files, runaway torrents, and problematic torrents across Deluge and qBittorrent.**

## ✨ Features

- 🔍 **Orphan Detection** - Find files not linked to any torrent
- 🏃 **Runaway Detection** - Find missing files from active torrents  
- 📋 **Report Cards** - Monitor torrents with tracker errors
- 🗑️ **Bulk Operations** - Delete files and folders efficiently with intelligent folder detection
- 🌐 **Web Interface** - Clean, responsive dashboard with real-time progress
- 🐳 **Docker Ready** - Easy deployment with docker-compose
- 🔄 **Multi-Client Support** - Works with both Deluge and qBittorrent
- 📊 **Real-time Scanning** - Live progress updates during scans

## 🚀 Quick Start

### Prerequisites
- Docker & Docker Compose
- Deluge and/or qBittorrent with web interface enabled
- Network access to your torrent clients

### Installation
```bash
# 1. Clone the repository
git clone https://github.com/thenunner/orphanage.git
cd orphanage

# 2. Copy and edit configuration
cp config.yml.example config.yml
nano config.yml  # Edit with your settings

# 3. Start the application
docker-compose up -d

# 4. Access the web interface
open http://localhost:3750
```

## 📖 Documentation

- [📋 Installation Guide](INSTALL.md) - Detailed setup instructions
- [⚙️ Configuration Examples](#configuration-examples) - Common setups
- [🔧 Troubleshooting](#troubleshooting) - Common issues and solutions

## 🖼️ What It Does

### Orphan Detection
Scans your file system and identifies files that exist on disk but aren't associated with any active torrent. Perfect for cleaning up after removing torrents without deleting files.

### Runaway Detection  
Finds missing files from active torrents - cases where the torrent is still active but some files have been moved, deleted, or corrupted.

### Report Cards
Monitors torrents with tracker errors, connection issues, or other problems that need attention.

### Smart Deletion
- Bulk delete multiple files at once
- Intelligent folder detection - offers to delete entire folders when all files inside are selected
- Real-time confirmation with file counts and folder previews

## ⚙️ Configuration Examples

### Unraid Setup
```yaml
Enable_Deluge: true
Enable_Qbit: true

Deluge_Url: "http://192.168.1.100:8112"
Deluge_Path_In: "/data"
Deluge_Path_Out: "/mnt/user/data"

Qbit_Url: "http://192.168.1.100:8080" 
Qbit_Path_In: "/data"
Qbit_Path_Out: "/mnt/user/data"
```

### Synology Setup
```yaml
Deluge_Path_In: "/data"
Deluge_Path_Out: "/volume1/downloads"

Qbit_Path_In: "/data" 
Qbit_Path_Out: "/volume1/downloads"
```

### Docker Network Setup
If your torrent clients are also running in Docker:
```yaml
Deluge_Url: "http://deluge:8112"  # Use container names
Qbit_Url: "http://qbittorrent:8080"
```

## 🔧 Troubleshooting

### Can't Connect to Clients
- Verify web interfaces are enabled and accessible
- Check IP addresses and ports in config.yml
- Ensure Docker containers can reach your torrent clients
- Test connectivity: `curl http://your-deluge-ip:8112`

### Files Not Detected
- Verify Path_In and Path_Out mappings are correct
- Path_In should match the path inside the Orphanage container
- Path_Out should match your host system paths
- Check docker volume mounts in docker-compose.yml

### Permission Issues
- Ensure Orphanage container runs with correct user (99:100 for Unraid)
- Verify read/write access to mounted directories
- Check file ownership on your data directories

## ⚡ System Requirements

- **RAM**: 512MB minimum, 1GB recommended for large libraries
- **Storage**: 100MB for application + space for logs
- **Network**: HTTP access to torrent client web interfaces
- **Docker**: Docker Engine 20.10+ and Docker Compose 2.0+

## 🛡️ Security Notes

- Web interface runs on localhost:3750 by default
- No authentication required (designed for local/private networks)
- Config file contains torrent client passwords - secure appropriately
- Only exposes port 3750 - torrent clients remain on their original ports

## 🏗️ Architecture

- **Backend**: Python FastAPI for REST APIs and torrent client communication
- **Frontend**: Vanilla JavaScript with responsive CSS
- **Data Storage**: Simple text files for scan results (reliable and portable)
- **Deployment**: Docker container with volume mounts for data persistence

## 🤝 Contributing

Issues and pull requests welcome! This project is designed to be simple and reliable.

### Development Setup
```bash
git clone https://github.com/thenunner/orphanage.git
cd orphanage
pip install -r requirements.txt
cp config.yml.example config.yml
# Edit config.yml
python app.py
```

## 📄 License

MIT License - see LICENSE file for details

## 🙏 Acknowledgments

Built for the homelab and self-hosted community. Designed to solve real problems with torrent file management in a simple, reliable way.