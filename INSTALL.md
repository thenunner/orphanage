# 📋 Installation Guide

## 🐳 Method 1: Docker Compose (Recommended)

### Step 1: Download Orphanage
```bash
git clone https://github.com/thenunner/orphanage.git
cd orphanage
```

### Step 2: Create Configuration
```bash
cp config.yml.example config.yml
nano config.yml  # Or use your preferred editor
```

### Step 3: Configure Your Settings

#### Required Settings
Edit `config.yml` with your specific configuration:

**Deluge Configuration:**
- `Deluge_Url`: Your Deluge web interface URL (default port 8112)
- `Deluge_Pass`: Your Deluge web interface password
- `Deluge_Torrent_Folder`: Path where .torrent files are stored
- `Deluge_Path_In`: Path as seen inside the Orphanage container
- `Deluge_Path_Out`: Actual path on your host system

**qBittorrent Configuration:**
- `Qbit_Url`: Your qBittorrent web interface URL (default port 8080)
- `Qbit_User`: Your qBittorrent username
- `Qbit_Pass`: Your qBittorrent password
- `Qbit_Torrent_Folder`: Path where .torrent files are stored
- `Qbit_Path_In`: Path as seen inside the Orphanage container
- `Qbit_Path_Out`: Actual path on your host system

#### Path Configuration Examples

**Unraid Example:**
```yaml
Deluge_Path_In: "/data"
Deluge_Path_Out: "/mnt/user/data"
```

**Synology Example:**
```yaml
Deluge_Path_In: "/data"
Deluge_Path_Out: "/volume1/downloads"
```

**Generic Linux Example:**
```yaml
Deluge_Path_In: "/data"
Deluge_Path_Out: "/home/user/downloads"
```

**Windows Example:**
```yaml
Deluge_Path_In: "/data"
Deluge_Path_Out: "C:/Downloads"
```

### Step 4: Update Docker Compose (if needed)

The included `docker-compose.yml` works for most setups, but you may need to adjust:

**Volume Mounts:**
```yaml
volumes:
  # App code + config
  - /your/app/path:/appdata/orphanage:rw
  # Your data directory
  - /your/data/path:/data:rw
```

**Network:**
```yaml
# If using custom Docker networks
networks:
  - your_network_name
```

**User/Permissions:**
```yaml
# Match your system's user ID
user: "1000:1000"  # Replace with your UID:GID
```

### Step 5: Deploy
```bash
docker-compose up -d
```

### Step 6: Access the Application
Open your browser and navigate to:
```
http://localhost:3750
```

Or if running on a remote server:
```
http://your-server-ip:3750
```

---

## 🖥️ Method 2: Direct Python Installation

### Prerequisites
- Python 3.11 or higher
- pip package manager

### Step 1: Clone Repository
```bash
git clone https://github.com/thenunner/orphanage.git
cd orphanage
```

### Step 2: Install Dependencies
```bash
pip install -r requirements.txt
```

### Step 3: Configure
```bash
cp config.yml.example config.yml
nano config.yml  # Edit with your settings
```

### Step 4: Run Application
```bash
python app.py
```

The application will start on `http://localhost:3750`

---

## 🔧 Advanced Configuration

### Using Environment Variables

For sensitive data, you can use environment variables instead of storing passwords in config files:

Create a `.env` file:
```bash
DELUGE_PASS=your_deluge_password
QBIT_PASS=your_qbit_password
```

Then reference in docker-compose.yml:
```yaml
environment:
  - DELUGE_PASS=${DELUGE_PASS}
  - QBIT_PASS=${QBIT_PASS}
```

### Custom Port Configuration

To run on a different port, update docker-compose.yml:
```yaml
ports:
  - "8080:3750"  # External:Internal
```

### Reverse Proxy Setup

#### Nginx Example:
```nginx
location /orphanage/ {
    proxy_pass http://localhost:3750/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

#### Traefik Example:
```yaml
labels:
  - "traefik.enable=true"
  - "traefik.http.routers.orphanage.rule=Host(`orphanage.yourdomain.com`)"
```

---

## 🚀 Platform-Specific Instructions

### Unraid Installation

1. **Enable Docker:** Ensure Docker is enabled in Unraid settings
2. **Create Appdata:** Create `/mnt/user/appdata/orphanage/` directory
3. **Download:** Use Community Applications or manual git clone
4. **Configure:** Edit config.yml with your Unraid paths
5. **Deploy:** `docker-compose up -d` or use Unraid Docker templates

### Synology Installation

1. **Enable SSH:** Enable SSH in Control Panel > Terminal & SNMP
2. **Install Docker:** Install Docker package from Package Center
3. **Install Git:** Install Git Server package or use docker
4. **Clone & Configure:** Follow standard installation steps
5. **Permissions:** Ensure proper permissions on volume folders

### QNAP Installation

1. **Enable SSH:** Enable SSH in Control Panel
2. **Install Container Station:** From App Center
3. **Clone Repository:** Use git or download zip
4. **Configure Paths:** Update docker-compose.yml for QNAP paths
5. **Deploy:** Use Container Station or command line

---

## 🔍 Verification & Testing

### Test Torrent Client Connectivity
```bash
# Test Deluge connection
curl -X POST http://your-deluge-ip:8112/json \
  -H "Content-Type: application/json" \
  -d '{"method": "auth.login", "params": ["your-password"], "id": 1}'

# Test qBittorrent connection  
curl -X POST http://your-qbit-ip:8080/api/v2/auth/login \
  -d "username=your-user&password=your-pass"
```

### Test File Access
Verify Orphanage can access your files:
```bash
docker exec orphanage ls -la /data
```

### Check Logs
Monitor application logs:
```bash
docker-compose logs -f orphanage
```

---

## 🛠️ Troubleshooting

### Container Won't Start
- Check docker-compose.yml syntax: `docker-compose config`
- Verify volume paths exist on host system
- Check port conflicts: `netstat -tulpn | grep 3750`

### Can't Connect to Torrent Clients
- Verify client web interfaces are accessible from host
- Check firewall settings
- Test with curl commands above
- Ensure clients allow connections from Docker subnet

### Permission Denied Errors
- Verify user/group settings in docker-compose.yml
- Check ownership of mounted directories
- Ensure Orphanage user has read/write access to data paths

### Empty Results
- Verify path mappings are correct
- Check that Path_In matches Docker volume mount
- Verify Path_Out matches your actual file locations
- Test with a manual scan and check logs

### Port Already in Use
```bash
# Find what's using port 3750
sudo lsof -i :3750

# Change port in docker-compose.yml
ports:
  - "3751:3750"  # Use different external port
```

---

## 📞 Getting Help

1. **Check Logs:** `docker-compose logs orphanage`
2. **Review Configuration:** Verify all paths and URLs
3. **Test Connectivity:** Use curl commands to test client access
4. **GitHub Issues:** Report bugs or ask questions
5. **Documentation:** Re-read this guide and README.md

---

## 🔄 Updating

### Update to Latest Version
```bash
cd orphanage
git pull origin main
docker-compose pull
docker-compose up -d
```

### Backup Configuration
Before updating:
```bash
cp config.yml config.yml.backup
cp -r logs/ logs.backup/
```

---

## 🗑️ Uninstalling

### Stop and Remove
```bash
cd orphanage
docker-compose down
docker rmi orphanage_orphanage  # Remove image
cd ..
rm -rf orphanage  # Remove files
```

### Clean Docker
```bash
docker system prune  # Remove unused containers/images
```