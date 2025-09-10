from fastapi import FastAPI, HTTPException, Request, Body
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import json
import uvicorn
import datetime
import mimetypes
import yaml
import logging
import os
import shutil

import scanner  # use the scanner module (which sets up logging)
import time
from typing import Dict, Optional

BASE_DIR = Path(".").resolve()
GUI_DIR = BASE_DIR / "gui"
LOGS_DIR = BASE_DIR / "logs"
CONFIG_PATH = BASE_DIR / "config.yml"

app = FastAPI(title="Orphanage")

# Simple cache for tracker information (key: torrent_id, value: {tracker: str, timestamp: float})
TRACKER_CACHE = {}
TRACKER_CACHE_TTL = 300  # 5 minutes cache

# Use the same logger as scanner
logger = logging.getLogger("orphanage")

def get_cached_tracker(torrent_id: str) -> Optional[str]:
    """Get tracker from cache if it's fresh"""
    if torrent_id in TRACKER_CACHE:
        cached_data = TRACKER_CACHE[torrent_id]
        if time.time() - cached_data["timestamp"] < TRACKER_CACHE_TTL:
            return cached_data["tracker"]
    return None

def cache_tracker(torrent_id: str, tracker: str):
    """Cache tracker information"""
    TRACKER_CACHE[torrent_id] = {
        "tracker": tracker,
        "timestamp": time.time()
    }

def _remove_from_orphan_files(deleted_file_path: str):
    """Remove a deleted file from the orphan text files"""
    try:
        
        # Get all orphan files that might contain this path
        orphan_files = []
        for key, file_path in SECTION_FILES.items():
            if 'orphans' in key and file_path and os.path.exists(file_path):
                orphan_files.append(file_path)
        
        files_updated = 0
        for orphan_file in orphan_files:
            try:
                # Read current content
                with open(orphan_file, 'r', encoding='utf-8', errors='ignore') as f:
                    lines = f.readlines()
                
                # Filter out the deleted file path
                original_count = len(lines)
                lines = [line for line in lines if line.strip() != deleted_file_path]
                
                if len(lines) < original_count:
                    # File was found and removed, write back
                    with open(orphan_file, 'w', encoding='utf-8') as f:
                        f.writelines(lines)
                    files_updated += 1
                    logger.info("Removed deleted file from %s", orphan_file)
                    
            except Exception as e:
                logger.warning("Failed to update orphan file %s: %s", orphan_file, e)
        
        logger.info("Updated %d orphan files after deletion", files_updated)
        return files_updated
        
    except Exception as e:
        logger.error("Error updating orphan files after deletion: %s", e)
        return 0

# Serve static files (CSS/JS/etc.)
app.mount("/gui", StaticFiles(directory=str(GUI_DIR), html=False), name="gui")


# ---------- helpers ----------

def read_config() -> dict:
    if not CONFIG_PATH.exists():
        raise HTTPException(status_code=500, detail="config.yml not found")
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read config.yml: {e}")


def apply_log_level():
    """Apply log level from config.yml (default INFO)."""
    try:
        cfg = read_config()
        lvl_name = cfg.get("Log_Level", "INFO").upper()
        lvl = getattr(logging, lvl_name, logging.INFO)
        logger.setLevel(lvl)
        for h in logger.handlers:
            h.setLevel(lvl)
        logger.info("Log level set to %s", lvl_name)
    except Exception as e:
        logger.warning("Failed to set log level from config: %s", e)


def _load_lines(p: Path):
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]


SECTION_FILES = {
    "deluge-orphans":     LOGS_DIR / "deluge-orphans.txt",
    "deluge-runaways":    LOGS_DIR / "deluge-runaways.txt",
    "deluge-reportcards": LOGS_DIR / "deluge-reportcards.txt",
    "qbit-orphans":       LOGS_DIR / "qbit-orphans.txt",
    "qbit-runaways":      LOGS_DIR / "qbit-runaways.txt",
    "qbit-reportcards":   LOGS_DIR / "qbit-reportcards.txt",
}

# Apply log level at startup
apply_log_level()

# ---------- routes ----------

@app.get("/")
def index():
    logger.debug("GET / called")
    html_path = GUI_DIR / "index.html"
    if not html_path.exists():
        logger.error("index.html missing in gui/")
        return PlainTextResponse("gui/index.html not found", status_code=500)
    with html_path.open("r", encoding="utf-8") as f:
        data = f.read()
    logger.debug("Returning index.html (%d bytes)", len(data))
    return Response(data, media_type="text/html")


@app.get("/gui/{name}")
def gui_assets(name: str):
    logger.debug("GET /gui/%s called", name)
    p = GUI_DIR / name
    if not p.exists():
        logger.warning("Asset %s not found", p)
        raise HTTPException(status_code=404)
    media_type = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    with p.open("rb") as fh:
        data = fh.read()
    logger.debug("Serving gui asset %s (%d bytes)", name, len(data))
    return Response(content=data, media_type=media_type, headers={"Cache-Control": "no-store"})


@app.get("/api/config")
def api_config():
    logger.debug("GET /api/config called")
    cfg = read_config()
    ui_cfg = {
        "ENABLE_DELUGE": bool(cfg.get("Enable_Deluge", False)),
        "ENABLE_QBIT":   bool(cfg.get("Enable_Qbit", False)),
    }
    logger.debug("Returning config: %s", ui_cfg)
    return ui_cfg

@app.get("/api/config-full")
def api_config_full():
    """Return full configuration for editing"""
    logger.debug("GET /api/config-full called")
    cfg = read_config()
    logger.debug("Returning full config with %d keys", len(cfg))
    return cfg

@app.post("/api/config")
def api_config_save(config_data: dict):
    """Save configuration to config.yml"""
    logger.debug("POST /api/config called with %d keys", len(config_data))
    
    try:
        # Write the config back to the YAML file
        with CONFIG_PATH.open("w", encoding="utf-8") as f:
            # Add header comment
            f.write("# --------------------------\n")
            f.write("# Orphanage configuration\n") 
            f.write("# --------------------------\n\n")
            
            # Enable/disable backends
            f.write("# Enable/disable backends\n")
            f.write(f"Enable_Deluge: {str(config_data.get('Enable_Deluge', False)).lower()}\n")
            f.write(f"Enable_Qbit: {str(config_data.get('Enable_Qbit', False)).lower()}\n\n")
            
            # Deluge settings
            f.write("# Deluge settings\n")
            f.write(f"Deluge_Url: \"{config_data.get('Deluge_Url', '')}\"\n")
            f.write(f"Deluge_Pass: \"{config_data.get('Deluge_Pass', '')}\"\n") 
            f.write(f"Deluge_Torrent_Folder: \"{config_data.get('Deluge_Torrent_Folder', '')}\"\n")
            f.write(f"Deluge_Path_In: \"{config_data.get('Deluge_Path_In', '')}\"\n")
            f.write(f"Deluge_Path_Out: \"{config_data.get('Deluge_Path_Out', '')}\"\n\n")
            
            # qBittorrent settings  
            f.write("# qBittorrent settings\n")
            f.write(f"Qbit_Url: \"{config_data.get('Qbit_Url', '')}\"\n")
            f.write(f"Qbit_User: \"{config_data.get('Qbit_User', '')}\"\n")
            f.write(f"Qbit_Pass: \"{config_data.get('Qbit_Pass', '')}\"\n")
            f.write(f"Qbit_Torrent_Folder: \"{config_data.get('Qbit_Torrent_Folder', '')}\"\n") 
            f.write(f"Qbit_Path_In: \"{config_data.get('Qbit_Path_In', '')}\"\n")
            f.write(f"Qbit_Path_Out: \"{config_data.get('Qbit_Path_Out', '')}\"\n\n")
            
            # Logging
            f.write("# --------------------------\n")
            f.write("# Logging\n") 
            f.write("# --------------------------\n")
            f.write("# Levels: DEBUG, INFO, WARNING, ERROR, CRITICAL\n")
            f.write(f"Log_Level: {config_data.get('Log_Level', 'INFO')}\n")
            
        logger.info("Configuration saved successfully")
        
        # Apply new log level immediately
        apply_log_level()
        
        return {"success": True, "message": "Configuration saved successfully"}
        
    except Exception as e:
        logger.error("Failed to save configuration: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to save configuration: {e}")


@app.get("/api/status")
def api_status():
    logger.debug("GET /api/status called")
    st = scanner.status()
    
    # If there's no recent scan timestamp but we have cached data, add cached timestamp
    if not st.get("timestamp") and not st.get("running"):
        cached_timestamp = _get_latest_cached_timestamp()
        if cached_timestamp:
            st["cached_timestamp"] = cached_timestamp
    
    logger.debug("Status result: %s", st)
    return st

def _get_latest_cached_timestamp():
    """Get the most recent timestamp from cached log files"""
    try:
        
        log_files = [
            LOGS_DIR / "deluge-orphans.txt",
            LOGS_DIR / "deluge-runaways.txt", 
            LOGS_DIR / "deluge-reportcards.txt",
            LOGS_DIR / "qbit-orphans.txt",
            LOGS_DIR / "qbit-runaways.txt",
            LOGS_DIR / "qbit-reportcards.txt"
        ]
        
        latest_time = None
        latest_timestamp = None
        
        for log_file in log_files:
            if log_file.exists() and log_file.stat().st_size > 0:
                mtime = log_file.stat().st_mtime
                if latest_time is None or mtime > latest_time:
                    latest_time = mtime
                    # Format timestamp like the scanner does
                    latest_timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
        
        return latest_timestamp
    except Exception as e:
        logger.debug("Error getting cached timestamp: %s", e)
        return None


@app.post("/api/scan/start")
def api_scan_start():
    logger.debug("POST /api/scan/start called")
    st = scanner.status()
    if st.get("running"):
        logger.debug("Scan already running")
        return JSONResponse({"ok": False, "msg": "Scan already running"}, status_code=409)
    cfg = read_config()
    scanner.start_scan(cfg)
    logger.debug("Scan started with cfg keys: %s", list(cfg.keys()))
    return {"ok": True}


@app.post("/api/scan/stop")
def api_scan_stop():
    logger.debug("POST /api/scan/stop called")
    st = scanner.status()
    if not st.get("running"):
        logger.debug("No scan running")
        return JSONResponse({"ok": False, "msg": "No scan running"}, status_code=409)
    scanner.stop_scan()
    logger.debug("Stop requested")
    return {"ok": True}


@app.get("/api/section")
def api_section(key: str):
    logger.debug("GET /api/section?key=%s called", key)
    p = SECTION_FILES.get(key)
    if not p:
        logger.error("Unknown section key %s", key)
        raise HTTPException(status_code=400, detail="unknown section key")
    rows = _load_lines(p)

    if key.endswith("orphans"):
        files = [r for r in rows if r and not r.startswith("DIR_")]
        out = {"files": files, "counts": {"dirs": 0, "files": len(files)}}
        logger.debug("Section orphans: %d files", len(files))
        return out

    if key.endswith("runaways"):
        flat = rows[:]  # keep original flat list
        grouped = {}
        torrent_info = {}  # Store torrent metadata
        
        for row in rows:
            # Parse the runaway format: client|torrent_id|torrent_name|category_or_label|tags|file_path
            try:
                parts = row.split('|', 5)
                if len(parts) >= 6:
                    client, torrent_id, torrent_name, label, tags, file_path = parts
                    
                    # Group by torrent_name (use as key)
                    group_key = torrent_name
                    grouped.setdefault(group_key, []).append(file_path)
                    
                    # Store torrent info (only once per torrent)
                    if group_key not in torrent_info:
                        # Check cache first
                        tracker = get_cached_tracker(torrent_id)
                        if tracker is None:
                            # Get additional torrent info including tracker
                            try:
                                cfg = read_config()
                                detailed_info = scanner.find_runaway_relationship(cfg, row)
                                tracker = detailed_info.get("tracker", "Unknown") if detailed_info else "Unknown"
                                # Cache the result
                                cache_tracker(torrent_id, tracker)
                            except Exception as e:
                                logger.warning("Failed to get tracker info for %s: %s", torrent_name, e)
                                tracker = "Unknown"
                        else:
                            logger.debug("Using cached tracker for %s: %s", torrent_name, tracker)
                        
                        torrent_info[group_key] = {
                            "client": client,
                            "torrent_id": torrent_id,
                            "torrent_name": torrent_name,
                            "label": label or "None",
                            "tags": tags,
                            "tracker": tracker,
                            "file_count": 0
                        }
                    torrent_info[group_key]["file_count"] += 1
                else:
                    # Fallback for malformed rows
                    grouped.setdefault("Unknown", []).append(row)
                    if "Unknown" not in torrent_info:
                        torrent_info["Unknown"] = {
                            "client": "unknown",
                            "torrent_id": "",
                            "torrent_name": "Unknown",
                            "label": "None",
                            "tags": "",
                            "tracker": "Unknown",
                            "file_count": 0
                        }
                    torrent_info["Unknown"]["file_count"] += 1
            except Exception:
                # Fallback for any parsing errors
                grouped.setdefault("Unknown", []).append(row)
                if "Unknown" not in torrent_info:
                    torrent_info["Unknown"] = {
                        "client": "unknown",
                        "torrent_id": "",
                        "torrent_name": "Unknown",
                        "label": "None",
                        "tags": "",
                        "tracker": "Unknown",
                        "file_count": 0
                    }
                torrent_info["Unknown"]["file_count"] += 1
                    
        out = {
            "missing": flat,
            "missing_by_torrent": grouped,
            "torrent_info": torrent_info,
            "count": len(flat)
        }
        logger.debug("Section runaways: %d total across %d torrents", len(flat), len(grouped))
        return out


    if key.endswith("reportcards"):
        out = {"errors": rows, "count": len(rows)}
        logger.debug("Section reportcards: %d entries", len(rows))
        return out

    logger.error("Unhandled section key %s", key)
    raise HTTPException(status_code=400, detail="unhandled section")


@app.get("/api/file-relationships")
def api_file_relationships(filename: str = "", torrent_name: str = ""):
    """Search for file relationships across all torrents and clients"""
    logger.debug("GET /api/file-relationships called with filename=%s, torrent_name=%s", filename, torrent_name)
    
    if not filename and not torrent_name:
        raise HTTPException(status_code=400, detail="Either filename or torrent_name parameter is required")
    
    # Start a quick scan to get current torrent data
    cfg = read_config()
    try:
        search_param = filename or torrent_name
        
        # If only filename provided, try to find the full path from orphan files
        if filename and not torrent_name:
            full_path = _find_full_path_for_filename(filename)
            if full_path:
                search_param = full_path
                logger.debug("Found full path for filename '%s': %s", filename, full_path)
            else:
                logger.warning("Could not find full path for filename: %s", filename)
        
        # Get current torrent information from both clients
        relationships = scanner.find_file_relationships(cfg, filename=search_param, torrent_name=torrent_name)
        logger.debug("Found %d relationships", len(relationships))
        return relationships
    except Exception as e:
        logger.error("Failed to find relationships: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to search relationships: {e}")

def _find_full_path_for_filename(target_filename: str) -> str:
    """Find the full path of a file by searching through orphan files"""
    orphan_files = [
        LOGS_DIR / "deluge-orphans.txt",
        LOGS_DIR / "qbit-orphans.txt"
    ]
    
    for orphan_file in orphan_files:
        if orphan_file.exists():
            try:
                with open(orphan_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        
                        # Handle both simple paths and enhanced format
                        if '|' in line:
                            # Enhanced format: client|torrent_id|torrent_name|label|tags|file_path
                            parts = line.split('|')
                            if len(parts) >= 6:
                                file_path = parts[5]
                            else:
                                continue
                        else:
                            # Simple format: just the file path
                            file_path = line
                        
                        # Check if this path ends with our target filename
                        if file_path.endswith('/' + target_filename):
                            return file_path
            except Exception as e:
                logger.warning("Error reading orphan file %s: %s", orphan_file, e)
    
    return ""

@app.get("/api/runaway-relationships")
def api_runaway_relationships(runaway_line: str = ""):
    """Get torrent info for a runaway based on the runaway line data"""
    logger.debug("GET /api/runaway-relationships called with runaway_line=%s", runaway_line[:100] if runaway_line else "")
    
    if not runaway_line:
        raise HTTPException(status_code=400, detail="runaway_line parameter is required")
    
    cfg = read_config()
    try:
        # Parse runaway line and get torrent information
        relationship = scanner.find_runaway_relationship(cfg, runaway_line)
        logger.debug("Found runaway relationship: %s", relationship is not None)
        return relationship if relationship else {}
    except Exception as e:
        logger.error("Failed to find runaway relationship: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to find runaway relationship: {e}")

@app.delete("/api/orphan-file")
def api_delete_orphan_file(file_path: str):
    """Delete an orphaned file from filesystem and update text files"""
    logger.debug("DELETE /api/orphan-file called")
    
    if not file_path:
        raise HTTPException(status_code=400, detail="file_path is required")
    
    try:
        
        # Security check: resolve path and basic validation
        file_path = str(Path(file_path).resolve())
        
        # Check if file exists
        if not os.path.exists(file_path):
            logger.warning("File not found: %s", file_path)
            raise HTTPException(status_code=404, detail="File not found")
        
        # Basic security: ensure it's not trying to delete system files
        if file_path.startswith(('/bin', '/sbin', '/usr', '/etc', '/sys', '/proc', '/dev')):
            logger.error("Attempted to delete system file: %s", file_path)
            raise HTTPException(status_code=403, detail="Cannot delete system files")
        
        # Delete the file
        os.remove(file_path)
        logger.info("Successfully deleted orphan file: %s", file_path)
        
        # Update orphan text files by removing this file path
        _remove_from_orphan_files(file_path)
        
        return {
            "success": True,
            "message": f"File deleted: {Path(file_path).name}",
            "deleted_path": file_path
        }
        
    except OSError as e:
        logger.error("Failed to delete file %s: %s", file_path, e)
        raise HTTPException(status_code=500, detail=f"Failed to delete file: {e}")
    except Exception as e:
        logger.error("Error deleting orphan file %s: %s", file_path, e)
        raise HTTPException(status_code=500, detail=f"Error deleting file: {e}")

@app.delete("/api/bulk-delete-orphan-files")
def api_bulk_delete_orphan_files(file_paths: list = Body(..., embed=True)):
    """Delete multiple orphaned files from filesystem and update text files"""
    logger.debug("DELETE /api/bulk-delete-orphan-files called with %d files", len(file_paths))
    
    if not file_paths:
        raise HTTPException(status_code=400, detail="file_paths is required")
    
    deleted_files = []
    failed_files = []
    
    for file_path in file_paths:
        try:
            # Security check: resolve path and basic validation
            file_path = str(Path(file_path).resolve())
            
            # Check if file exists
            if not os.path.exists(file_path):
                logger.warning("File not found: %s", file_path)
                failed_files.append({"file": file_path, "error": "File not found"})
                continue
            
            # Basic security: ensure it's not trying to delete system files
            if file_path.startswith(('/bin', '/sbin', '/usr', '/etc', '/sys', '/proc', '/dev')):
                logger.error("Attempted to delete system file: %s", file_path)
                failed_files.append({"file": file_path, "error": "Cannot delete system files"})
                continue
            
            # Delete the file
            os.remove(file_path)
            logger.info("Successfully deleted orphan file: %s", file_path)
            deleted_files.append(file_path)
            
        except Exception as e:
            logger.error("Failed to delete file %s: %s", file_path, e)
            failed_files.append({"file": file_path, "error": str(e)})
    
    # Update orphan text files for all successfully deleted files
    deleted_folders = []
    if deleted_files:
        try:
            for deleted_file_path in deleted_files:
                _remove_from_orphan_files(deleted_file_path)
            logger.info("Updated orphan files after bulk deletion of %d files", len(deleted_files))
            
            # Get folder candidates for user confirmation
            folder_candidates = _get_folders_for_confirmation(deleted_files)
                    
        except Exception as e:
            logger.error("Error updating orphan files after bulk deletion: %s", e)
    
    return {
        "success": True,
        "deleted_count": len(deleted_files),
        "failed_count": len(failed_files),
        "deleted_folder_count": len(deleted_folders),
        "deleted_files": deleted_files,
        "failed_files": failed_files,
        "deleted_folders": deleted_folders,
        "message": f"Deleted {len(deleted_files)} files{f' and {len(deleted_folders)} empty folders' if deleted_folders else ''}, {len(failed_files)} failed"
    }

@app.get("/api/export")
def api_export(keys: str):
    logger.debug("GET /api/export?keys=%s called", keys)
    want = [k.strip() for k in (keys or "").split(",") if k.strip()]
    if not want:
        logger.error("No keys provided to export")
        raise HTTPException(status_code=400, detail="no keys provided")

    export = {}
    for k in want:
        p = SECTION_FILES.get(k)
        if not p:
            logger.warning("Skipping unknown section %s", k)
            continue
        rows = _load_lines(p)

        if k.endswith("orphans"):
            files = [r for r in rows if r and not r.startswith("DIR_")]
            export[k] = {"files": files, "counts": {"dirs": 0, "files": len(files)}}
        elif k.endswith("runaways"):
            export[k] = {"missing": rows, "count": len(rows)}
        elif k.endswith("reportcards"):
            export[k] = {"errors": rows, "count": len(rows)}
        else:
            export[k] = rows

    payload = json.dumps(export, indent=2).encode("utf-8")
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"results-{ts}.json"
    headers = {"Content-Disposition": f'attachment; filename=\"{filename}\"'}
    logger.debug("Export returning %d keys", len(export))
    return Response(content=payload, media_type="application/json", headers=headers)


@app.delete("/api/bulk-delete-torrents")
def api_bulk_delete_torrents(torrents: list = Body(..., embed=True)):
    """Delete multiple torrents from torrent clients (with files)"""
    logger.debug("DELETE /api/bulk-delete-torrents called with %d torrents", len(torrents))
    
    if not torrents:
        raise HTTPException(status_code=400, detail="torrents is required")
    
    # Load configuration
    config = read_config()
    
    deleted_torrents = []
    failed_torrents = []
    
    for torrent_data in torrents:
        try:
            # Parse torrent data: client|torrent_id|torrent_name|category|tags|error_message|save_path
            parts = torrent_data.split('|')
            if len(parts) < 6:
                failed_torrents.append({"torrent": torrent_data, "error": "Invalid torrent data format"})
                continue
                
            client, torrent_id, torrent_name, category, tags, error_message = parts[:6]
            
            if client.lower() == "deluge" and config.get("Enable_Deluge", False):
                success = _delete_deluge_torrent(config, torrent_id, torrent_name)
            elif client.lower() == "qbit" and config.get("Enable_Qbit", False):
                success = _delete_qbit_torrent(config, torrent_id, torrent_name) 
            else:
                failed_torrents.append({"torrent": torrent_name, "error": f"Client {client} not enabled or supported"})
                continue
                
            if success:
                deleted_torrents.append(torrent_name)
                logger.info("Successfully removed torrent: %s", torrent_name)
                
                # Remove the deleted torrent from report cards files
                _remove_from_report_cards(torrent_data)
            else:
                failed_torrents.append({"torrent": torrent_name, "error": "Failed to delete torrent"})
                
        except Exception as e:
            logger.error("Failed to delete torrent %s: %s", torrent_data, e)
            failed_torrents.append({"torrent": torrent_data, "error": str(e)})
    
    return {
        "success_count": len(deleted_torrents),
        "failed_count": len(failed_torrents),
        "deleted_torrents": deleted_torrents,
        "failed_torrents": failed_torrents
    }


def _delete_deluge_torrent(config: dict, torrent_id: str, torrent_name: str) -> bool:
    """Delete a torrent from Deluge with files"""
    try:
        deluge_cli = scanner.DelugeClient(config.get("Deluge_Url", ""), config.get("Deluge_Pass", ""))
        deluge_cli.login()
        
        # Delete torrent with files (remove_data=True)
        result = deluge_cli._rpc("core.remove_torrent", [torrent_id, True])
        return bool(result)
        
    except Exception as e:
        logger.error("Failed to delete Deluge torrent %s: %s", torrent_name, e)
        return False


def _delete_qbit_torrent(config: dict, torrent_hash: str, torrent_name: str) -> bool:
    """Delete a torrent from qBittorrent with files"""
    try:
        qbit_cli = scanner.QbitClient(config.get("Qbit_Url", ""), config.get("Qbit_User", ""), config.get("Qbit_Pass", ""))
        qbit_cli.login()
        
        # Delete torrent with files
        response = qbit_cli.s.post(f"{qbit_cli.base}/api/v2/torrents/delete", 
                                 data={"hashes": torrent_hash, "deleteFiles": "true"},
                                 timeout=qbit_cli.timeout)
        response.raise_for_status()
        return True
        
    except Exception as e:
        logger.error("Failed to delete qBittorrent torrent %s: %s", torrent_name, e) 
        return False


def _remove_from_report_cards(torrent_data: str):
    """Remove a deleted torrent from report cards text files"""
    try:
        # Parse torrent data: client|torrent_id|torrent_name|category|tags|error_message|save_path
        parts = torrent_data.split('|')
        if len(parts) < 6:
            logger.warning("Invalid torrent data format for removal: %s", torrent_data)
            return
            
        client = parts[0].lower()
        
        # Determine which report cards file to update using SECTION_FILES mapping
        if client == "deluge":
            report_file = SECTION_FILES.get("deluge-reportcards")
        elif client == "qbit":
            report_file = SECTION_FILES.get("qbit-reportcards")
        else:
            logger.warning("Unknown client for report cards removal: %s", client)
            return
            
        if not report_file:
            logger.warning("Report cards file not configured for client: %s", client)
            return
            
        # Read current report cards
        if not report_file.exists():
            logger.debug("Report cards file does not exist: %s", report_file)
            return
            
        with open(report_file, 'r', encoding='utf-8') as f:
            lines = [line.strip() for line in f.readlines()]
        
        # Filter out the deleted torrent (exact match)
        original_count = len(lines)
        filtered_lines = [line for line in lines if line != torrent_data]
        
        # Write back the filtered lines
        with open(report_file, 'w', encoding='utf-8') as f:
            for line in filtered_lines:
                if line:  # Skip empty lines
                    f.write(line + '\n')
        
        removed_count = original_count - len(filtered_lines)
        if removed_count > 0:
            logger.info("Removed %d entries from %s report cards", removed_count, client)
        else:
            logger.debug("No matching entries found to remove from %s report cards", client)
            
    except Exception as e:
        logger.error("Failed to remove torrent from report cards: %s", e)

def _get_folders_for_confirmation(deleted_files: list) -> list:
    """Get folders where ALL files were selected for deletion"""
    from collections import defaultdict
    
    folder_candidates = []
    
    try:
        # Group deleted files by their parent directories
        dir_files = defaultdict(list)
        for file_path in deleted_files:
            parent_dir = str(Path(file_path).parent)
            dir_files[parent_dir].append(file_path)
        
        # Check each directory
        for parent_dir, deleted_files_in_dir in dir_files.items():
            try:
                if not os.path.exists(parent_dir) or not os.path.isdir(parent_dir):
                    continue
                
                # Get ALL files actually in this directory
                all_files_in_dir = []
                for root, dirs, files in os.walk(parent_dir):
                    for file in files:
                        all_files_in_dir.append(os.path.join(root, file))
                
                # Check if ALL files in directory were selected for deletion
                if len(all_files_in_dir) > 0 and len(deleted_files_in_dir) == len(all_files_in_dir):
                    # All files in this directory were deleted - candidate for folder deletion
                    folder_candidates.append({
                        "path": parent_dir,
                        "file_count": len(all_files_in_dir),
                        "folder_name": Path(parent_dir).name
                    })
                    logger.debug("Folder candidate for deletion: %s (%d files)", parent_dir, len(all_files_in_dir))
                    
            except Exception as e:
                logger.warning("Error checking folder %s: %s", parent_dir, e)
                continue
                
    except Exception as e:
        logger.error("Error finding folder candidates: %s", e)
    
    return folder_candidates

@app.post("/api/check-folder-files")
def api_check_folder_files(folder_path: str = Body(...), selected_files: list = Body(...)):
    """Check if all files in a folder are selected for deletion"""
    logger.debug("POST /api/check-folder-files called for: %s", folder_path)
    
    try:
        
        if not os.path.exists(folder_path) or not os.path.isdir(folder_path):
            return {"can_delete_folder": False, "total_files": 0, "reason": "Folder not found"}
        
        # Get all files in the directory (recursively)
        all_files_in_dir = []
        for root, dirs, files in os.walk(folder_path):
            for file in files:
                all_files_in_dir.append(os.path.join(root, file))
        
        total_files = len(all_files_in_dir)
        selected_count = len(selected_files)
        
        # Check if all files are selected
        can_delete_folder = total_files > 0 and selected_count == total_files
        
        return {
            "can_delete_folder": can_delete_folder,
            "total_files": total_files,
            "selected_files": selected_count,
            "folder_name": Path(folder_path).name
        }
        
    except Exception as e:
        logger.error("Error checking folder files %s: %s", folder_path, e)
        return {"can_delete_folder": False, "total_files": 0, "reason": str(e)}

@app.delete("/api/delete-folder")
def api_delete_folder(folder_path: str = Body(..., embed=True)):
    """Delete a specific folder and update orphan files"""
    logger.debug("DELETE /api/delete-folder called for: %s", folder_path)
    
    try:
        # Security check
        folder_path = str(Path(folder_path).resolve())
        
        if not os.path.exists(folder_path) or not os.path.isdir(folder_path):
            raise HTTPException(status_code=404, detail="Folder not found")
        
        # Security: ensure it's not trying to delete system folders
        if folder_path.startswith(('/bin', '/sbin', '/usr', '/etc', '/sys', '/proc', '/dev')):
            logger.error("Attempted to delete system folder: %s", folder_path)
            raise HTTPException(status_code=403, detail="Cannot delete system folders")
        
        # Get folder info before deletion
        folder_name = Path(folder_path).name
        
        # Delete the entire folder
        shutil.rmtree(folder_path)
        logger.info("Successfully deleted folder: %s", folder_path)
        
        # Remove any remaining references from orphan files
        _remove_folder_from_orphan_files(folder_path)
        
        return {
            "success": True,
            "message": f"Folder deleted: {folder_name}",
            "deleted_folder": folder_path
        }
        
    except Exception as e:
        logger.error("Failed to delete folder %s: %s", folder_path, e)
        raise HTTPException(status_code=500, detail=f"Failed to delete folder: {e}")

def _remove_folder_from_orphan_files(folder_path: str):
    """Remove any files from the deleted folder from orphan text files"""
    try:
        # Get all orphan files
        orphan_files = []
        for key, file_path in SECTION_FILES.items():
            if 'orphans' in key and file_path and os.path.exists(file_path):
                orphan_files.append(file_path)
        
        for orphan_file in orphan_files:
            try:
                # Read current content
                with open(orphan_file, 'r', encoding='utf-8', errors='ignore') as f:
                    lines = f.readlines()
                
                # Filter out any lines that start with the deleted folder path
                original_count = len(lines)
                filtered_lines = []
                for line in lines:
                    line_path = line.strip()
                    if '|' in line_path:
                        # Enhanced format - extract file path
                        parts = line_path.split('|')
                        if len(parts) >= 6:
                            line_path = parts[5]
                    
                    # Keep line if it doesn't start with the deleted folder path
                    if not line_path.startswith(folder_path):
                        filtered_lines.append(line.rstrip() + '\n')
                
                # Write back filtered content if changed
                if len(filtered_lines) != original_count:
                    with open(orphan_file, 'w', encoding='utf-8') as f:
                        f.writelines(filtered_lines)
                    
                    removed_count = original_count - len(filtered_lines)
                    logger.info("Removed %d orphan entries from %s after folder deletion", removed_count, orphan_file)
                    
            except Exception as e:
                logger.warning("Error updating orphan file %s after folder deletion: %s", orphan_file, e)
                
    except Exception as e:
        logger.error("Error removing folder references from orphan files: %s", e)


def _remove_from_orphan_files(file_path: str):
    """Remove a specific file from orphan text files"""
    try:
        # Get all orphan files
        orphan_files = []
        for key, txt_file_path in SECTION_FILES.items():
            if 'orphans' in key and txt_file_path and os.path.exists(txt_file_path):
                orphan_files.append(txt_file_path)
        
        for orphan_file in orphan_files:
            try:
                # Read current content
                with open(orphan_file, 'r', encoding='utf-8', errors='ignore') as f:
                    lines = f.readlines()
                
                # Filter out the specific deleted file
                original_count = len(lines)
                filtered_lines = []
                for line in lines:
                    line_path = line.strip()
                    if '|' in line_path:
                        # Enhanced format - extract file path
                        parts = line_path.split('|')
                        if len(parts) >= 6:
                            line_path = parts[5]
                    
                    # Keep line if it's not the deleted file path
                    if line_path != file_path:
                        filtered_lines.append(line.rstrip() + '\n')
                
                # Write back filtered content if changed
                if len(filtered_lines) != original_count:
                    with open(orphan_file, 'w', encoding='utf-8') as f:
                        f.writelines(filtered_lines)
                    
                    removed_count = original_count - len(filtered_lines)
                    logger.info("Removed %d orphan entry from %s after file deletion", removed_count, orphan_file)
                    
            except Exception as e:
                logger.warning("Error updating orphan file %s after file deletion: %s", orphan_file, e)
                
    except Exception as e:
        logger.error("Error removing file references from orphan files: %s", e)


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=3750, reload=False)
