#!/usr/bin/env python3
# scanner.py — YAML config version with robust Deluge + qBit login and TXT outputs
# - Real-time progress updates inside each phase
# - Robust qBit path resolution (content_path/save_path/absolute names)
# - Report Cards skip transient tracker errors ("bad gateway", "overloaded")

import os
import time
import threading
from pathlib import Path
from typing import Dict, List, Tuple, Set, Optional

import requests
import logging
from logging.handlers import RotatingFileHandler
import yaml
import unicodedata

class ScanStopped(Exception):
    """Raised when user stops a scan."""
    pass

# --------------------------
# Globals & logging
# --------------------------

BASE_DIR = Path(".").resolve()
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = LOGS_DIR / "orphanage.log"

logger = logging.getLogger("orphanage")

# Default INFO, will be overridden by config
default_level = logging.INFO
if not logger.handlers:
    fh = RotatingFileHandler(str(LOG_FILE), maxBytes=100 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)
logger.setLevel(default_level)

# --------------------------
# Config loader
# --------------------------

def load_config(path: Path = BASE_DIR / "config.yml") -> Dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

# --------------------------
# Path helpers
# --------------------------

def normalize(p: str) -> str:
    if not p:
        return ""
    p = unicodedata.normalize("NFC", p)
    return os.path.normpath(p)

def robust_path_exists(path: str) -> bool:
    """Hybrid Unicode-aware path existence check"""
    if not path:
        return False
        
    # FIRST PASS: Conservative checks (fast)
    # Try the path as-is first
    if os.path.exists(path):
        return True
        
    # Remove only invisible characters (zero-width spaces) - but try both with and without
    invisible_chars = ['\u200b', '\u200c', '\u200d', '\ufeff']
    clean_path = path
    for char in invisible_chars:
        clean_path = clean_path.replace(char, '')
    
    # Try the cleaned version
    if os.path.exists(clean_path):
        return True
        
    # Try only basic Unicode normalizations on both original and cleaned paths
    for test_path in [path, clean_path]:
        for form in ['NFC', 'NFD']:
            try:
                normalized = unicodedata.normalize(form, test_path)
                if os.path.exists(normalized):
                    return True
            except (ValueError, TypeError):
                continue
    
    # SECOND PASS: Targeted directory scan for edge cases (slower)
    # Only for files that failed conservative checks
    try:
        parent = os.path.dirname(clean_path)
        filename = os.path.basename(clean_path)
        
        # Try to find the parent directory with Unicode variations
        actual_parent = None
        if os.path.exists(parent):
            actual_parent = parent
        else:
            # Parent directory might have Unicode differences
            grandparent = os.path.dirname(parent)
            parent_name = os.path.basename(parent)
            
            if os.path.exists(grandparent):
                try:
                    for item in os.listdir(grandparent):
                        # Try different Unicode normalizations for directory names
                        for form in ['NFC', 'NFD']:
                            try:
                                item_norm = unicodedata.normalize(form, item)
                                parent_norm = unicodedata.normalize(form, parent_name)
                                if item_norm == parent_norm:
                                    actual_parent = os.path.join(grandparent, item)
                                    break
                            except (ValueError, TypeError):
                                continue
                        if actual_parent:
                            break
                except OSError:
                    pass
        
        if not actual_parent or not os.path.exists(actual_parent):
            return False
            
        # Get directory contents once
        try:
            dir_items = os.listdir(actual_parent)
        except OSError:
            return False
            
        # Check each item in directory with Unicode normalization
        for item in dir_items:
            # Try different Unicode normalizations
            for form in ['NFC', 'NFD']:
                try:
                    item_norm = unicodedata.normalize(form, item)
                    filename_norm = unicodedata.normalize(form, filename)
                    if item_norm == filename_norm:
                        return True
                except (ValueError, TypeError):
                    continue
                
    except OSError:
        pass
            
    return False

def realnorm(p: str) -> str:
    """realpath + normpath + NFC unicode normalization"""
    return normalize(os.path.realpath(p))

def safe_realnorm(p: str) -> str:
    """Safe version of realnorm with error handling"""
    try:
        if not p or not os.path.exists(p):
            return normalize(p)  # Fall back to normalize only
        return realnorm(p)
    except (OSError, ValueError) as e:
        logger.warning("Path resolution failed for %s: %s", p, e)
        return normalize(p)

def map_path(path: str, path_in: str, path_out: str) -> str:
    path = normalize(path)
    path_in = normalize(path_in or "")
    path_out = normalize(path_out or "")
    if path_in and path.startswith(path_in):
        mapped = path_out + path[len(path_in):]
        return normalize(mapped)
    return path

def safe_map_path(path: str, path_in: str, path_out: str) -> str:
    """Safe version of map_path with validation"""
    try:
        return map_path(path, path_in, path_out)
    except (OSError, ValueError) as e:
        logger.warning("Path mapping failed for %s: %s", path, e)
        return normalize(path)

# --------------------------
# qBittorrent client
# --------------------------

class QbitClient:
    def __init__(self, url: str, user: str, password: str, timeout: int = 25):
        self.base = url.rstrip("/")
        self.user = user
        self.password = password
        self.s = requests.Session()
        self.s.headers.update({"Referer": self.base})
        self.timeout = timeout

    def login(self) -> None:
        r = self.s.post(
            f"{self.base}/api/v2/auth/login",
            data={"username": self.user, "password": self.password},
            timeout=self.timeout,
        )
        r.raise_for_status()

        ok_text = "Ok." in (r.text or "")
        have_cookie = any(c.name.lower() in ("sid", "qbittorrent-sid") for c in self.s.cookies)
        if not (ok_text or have_cookie):
            raise RuntimeError("qBittorrent login failed (no Ok. and no SID cookie)")

        vr = self.s.get(f"{self.base}/api/v2/app/version", timeout=self.timeout)
        if vr.status_code != 200:
            raise RuntimeError(f"qBittorrent session verify failed (HTTP {vr.status_code})")

    def torrents(self, hashes: str = None) -> List[Dict]:
        params = {}
        if hashes:
            params["hashes"] = hashes
        r = self.s.get(f"{self.base}/api/v2/torrents/info", params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def files(self, torrent_hash: str) -> List[Dict]:
        r = self.s.get(f"{self.base}/api/v2/torrents/files", params={"hash": torrent_hash}, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def trackers(self, torrent_hash: str) -> List[Dict]:
        r = self.s.get(f"{self.base}/api/v2/torrents/trackers", params={"hash": torrent_hash}, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

# --------------------------
# Deluge Web client (JSON-RPC)
# --------------------------

class DelugeClient:
    def __init__(self, url: str, password: str, timeout: int = 25):
        self.base = url.rstrip("/")
        self.password = password
        self.s = requests.Session()
        self.timeout = timeout
        self._id = 0

    def _rpc(self, method: str, params: List) -> Dict:
        self._id += 1
        payload = {"method": method, "params": params, "id": self._id}
        r = self.s.post(f"{self.base}/json", json=payload, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        if "error" in data and data["error"]:
            raise RuntimeError(f"Deluge RPC error: {data['error']}")
        return data["result"]

    def login(self) -> None:
        logger.debug("Deluge auth.login attempt")
        ok = self._rpc("auth.login", [self.password])
        if not ok:
            raise RuntimeError("auth.login returned False (check your password)")
        logger.debug("Deluge auth.login OK")

    def update_ui(self, fields: List[str]) -> Dict:
        return self._rpc("web.update_ui", [fields, {}])

# --------------------------
# Text file helpers
# --------------------------

def write_lines(path: Path, lines: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for ln in lines:
            f.write(ln.rstrip() + "\n")
    tmp.replace(path)

def _read_lines(path: Path) -> List[str]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [ln.rstrip("\n") for ln in f]

# --------------------------
# Utility: transient error filter
# --------------------------

def _is_transient_tracker_error(msg: str) -> bool:
    if not msg:
        return False
    m = msg.lower()
    return ("bad gateway" in m) or ("overloaded" in m) or ("maintenance" in m) or ("stream truncated" in m)

# --------------------------
# Scanner runner
# --------------------------

class ScannerRunner:
    def __init__(self, cfg: Dict):
        self.cfg = cfg
        self.stop_evt = threading.Event()
        self.progress = {"phase": 0, "total": 0, "percent": 0.0, "label": "Idle"}
        self.last_error: Optional[str] = None
        self.last_timestamp: Optional[str] = None
        self._start_time = None

        lvl_name = cfg.get("Log_Level", "INFO").upper()
        lvl = getattr(logging, lvl_name, logging.INFO)
        logger.setLevel(lvl)
        for h in logger.handlers:
            h.setLevel(lvl)
        logger.info("Scanner log level set to %s", lvl_name)

    def stop(self):
        self.stop_evt.set()

    def _set_progress(self, phase: int, total: int, label: str, percent: float = None, phase_progress: float = 0.0, log_every: int = 5):
        """Update current progress and throttle log spam."""
        self.progress["phase"] = phase
        self.progress["total"] = total
        self.progress["label"] = label
        
        if percent is not None:
            # Use provided percent (for manual override)
            self.progress["percent"] = max(0.0, min(100.0, float(percent)))
        else:
            # Calculate true overall progress across all phases
            if total > 0:
                # Base progress from completed phases
                phase_base = ((phase - 1) / total) * 100.0
                # Progress within current phase  
                phase_contribution = (phase_progress / 100.0) * (100.0 / total)
                self.progress["percent"] = max(0.0, min(100.0, phase_base + phase_contribution))
            else:
                self.progress["percent"] = 0.0

        # only log occasionally to keep logs clean
        if phase != 0:  # skip "Idle"
            if not hasattr(self, "_log_counter"):
                self._log_counter = 0
            self._log_counter += 1 
            if self._log_counter % log_every == 0:
                    logger.info(
                        "Progress: %s (%s/%s) %.1f%%",
                        label,
                        phase,
                        total,
                        self.progress.get("percent", 0.0),
                    )

    def run(self):
        try:
            self._start_time = time.time()
            self.last_error = None
            enabled = []
            if self.cfg.get("Enable_Deluge", False):
                enabled.append("deluge")
            if self.cfg.get("Enable_Qbit", False):
                enabled.append("qbit")
            if not enabled:
                raise RuntimeError("No clients enabled in config.yml")

            phases = []
            if "deluge" in enabled:
                phases += [("deluge","orphans"), ("deluge","runaways"), ("deluge","reportcards")]
            if "qbit" in enabled:
                phases += [("qbit","orphans"), ("qbit","runaways"), ("qbit","reportcards")]
            total = len(phases)

            out_map = {
                ("deluge","orphans"): LOGS_DIR / "deluge-orphans.txt",
                ("deluge","runaways"): LOGS_DIR / "deluge-runaways.txt",
                ("deluge","reportcards"): LOGS_DIR / "deluge-reportcards.txt",
                ("qbit","orphans"): LOGS_DIR / "qbit-orphans.txt",
                ("qbit","runaways"): LOGS_DIR / "qbit-runaways.txt",
                ("qbit","reportcards"): LOGS_DIR / "qbit-reportcards.txt",
            }
            for p in out_map.values():
                try:
                    p.unlink(missing_ok=True)
                except Exception as e:
                    logger.warning("Could not remove %s: %s", p, e)

            deluge_cli = None
            qbit_cli = None

            for idx, (client, kind) in enumerate(phases, start=1):
                if self.stop_evt.is_set():
                    raise ScanStopped

                label = self._phase_label(client, kind)

                if idx == 1:
                    self._set_progress(idx, total, label, phase_progress=0.0)
                else:
                    self._set_progress(idx, total, label, phase_progress=0.0)

                if client == "deluge":
                    if deluge_cli is None:
                        deluge_cli = self._login_deluge()
                    if kind == "orphans":
                        self._phase_orphans_deluge(idx, total, deluge_cli, out_map[(client, kind)])
                    elif kind == "runaways":
                        self._phase_runaways_deluge(idx, total, deluge_cli, out_map[(client, kind)])
                    elif kind == "reportcards":
                        self._phase_reportcards_deluge(idx, total, deluge_cli, out_map[(client, kind)])
                else:
                    if qbit_cli is None:
                        qbit_cli = self._login_qbit()
                    if kind == "orphans":
                        self._phase_orphans_qbit(idx, total, qbit_cli, out_map[(client, kind)])
                    elif kind == "runaways":
                        self._phase_runaways_qbit(idx, total, qbit_cli, out_map[(client, kind)])
                    elif kind == "reportcards":
                        self._phase_reportcards_qbit(idx, total, qbit_cli, out_map[(client, kind)])

            self.last_timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            logger.info("Scan complete at %s", self.last_timestamp)

        except ScanStopped:
            logger.info("Scan cancelled by user")
        except Exception as e:
            self.last_error = str(e)
            logger.exception("Scan failed: %s", e)
        finally:
            self._set_progress(0, 0, "Idle", phase_progress=0.0)


    def _phase_label(self, client: str, kind: str) -> str:
        names = {
            ("deluge","orphans"): "Phase 1: Checking Orphans in Deluge",
            ("deluge","runaways"): "Phase 2: Checking Runaways in Deluge",
            ("deluge","reportcards"): "Phase 3: Collecting Report Cards in Deluge",
            ("qbit","orphans"): "Phase 4: Checking Orphans in qBittorrent",
            ("qbit","runaways"): "Phase 5: Checking Runaways in qBittorrent",
            ("qbit","reportcards"): "Phase 6: Checking Report Cards in qBittorrent",
        }
        if not self.cfg.get("Enable_Deluge", False):
            names = {
                ("qbit","orphans"): "Phase 1: Checking Orphans in qBittorrent",
                ("qbit","runaways"): "Phase 2: Checking Runaways in qBittorrent",
                ("qbit","reportcards"): "Phase 3: Checking Report Cards in qBittorrent",
            }
        elif not self.cfg.get("Enable_Qbit", False):
            names = {
                ("deluge","orphans"): "Phase 1: Checking Orphans in Deluge",
                ("deluge","runaways"): "Phase 2: Checking Runaways in Deluge",
                ("deluge","reportcards"): "Phase 3: Collecting Report Cards in Deluge",
            }
        return names.get((client, kind), f"{client}:{kind}")

    # -------- Collect client file sets --------

    def _collect_deluge_files_for_orphans(self, deluge_cli: "DelugeClient", idx: int, total: int) -> Tuple[Set[str], Dict[str, Dict]]:
        """Collect just the file paths from Deluge for orphan detection"""
        logger.debug("Collecting Deluge file paths for orphan detection")
        try:
            res = deluge_cli.update_ui(["name","save_path","download_location","files","label"])
            torrent_items = list((res.get("torrents") or {}).items())
            total_torrents = len(torrent_items)
            logger.debug("Got %d torrents from Deluge", total_torrents)
        except Exception as e:
            logger.error("Failed to get torrents from Deluge: %s", e)
            return set(), {}
            
        host_files: Set[str] = set()
        file_metadata: Dict[str, Dict] = {}  # file_path -> torrent metadata
        path_in = self.cfg.get("Deluge_Path_In","")
        path_out = self.cfg.get("Deluge_Path_Out","")
        batch_size = 25
        processed = 0
        
        # Process torrents in batches of 25
        for i in range(0, len(torrent_items), batch_size):
            if self.stop_evt.is_set():
                logger.info("Deluge file collection stopped by user")
                break
                
            batch = torrent_items[i:i + batch_size]
            last_torrent_name = ""
            
            for tid, t in batch:
                if self.stop_evt.is_set():
                    break
                    
                name = t.get("name", "") or str(tid)[:8]
                last_torrent_name = name  # Track the last processed torrent name
                label = t.get("label", "") or ""
                
                # Create torrent metadata
                torrent_info = {
                    "client": "deluge",
                    "torrent_id": str(tid),
                    "torrent_name": name,
                    "label": label,
                    "tags": ""  # Empty for Deluge
                }
                
                save_path = t.get("save_path") or t.get("download_location") or ""
                files = t.get("files") or []
                
                for f in files:
                    rel = f.get("path","") or ""
                    full = normalize(os.path.join(save_path, rel))
                    container_path = safe_realnorm(full)  # Keep as container path (original logic)
                    host_files.add(container_path)
                    file_metadata[container_path] = torrent_info  # Store metadata for this file
                    
                processed += 1
            
            # Update progress with last torrent name from batch
            collection_percent = (processed / total_torrents) * 50.0  # 50% of this phase for collection
            self._set_progress(idx, total, f"Deluge: {last_torrent_name} ({processed}/{total_torrents})", phase_progress=collection_percent)
            logger.debug("Processed batch ending with: %s (%d/%d torrents)", last_torrent_name, processed, total_torrents)
            
        logger.debug("Collected %d file paths from %d torrents", len(host_files), processed)
        return host_files, file_metadata
        
    def _collect_deluge_torrents_for_runaways(self, deluge_cli: "DelugeClient") -> List[Dict]:
        """Collect torrent info for runaway detection"""
        logger.debug("Collecting Deluge torrents for runaway detection")
        try:
            res = deluge_cli.update_ui(["name","save_path","download_location","files","label"])
            torrent_items = list((res.get("torrents") or {}).items())
        except Exception as e:
            logger.error("Failed to get torrents from Deluge: %s", e)
            return []
            
        torrents = []
        for tid, t in torrent_items:
            if self.stop_evt.is_set():
                break
            name = t.get("name","")
            save_path = t.get("save_path") or t.get("download_location") or ""
            files = t.get("files") or []
            label = t.get("label", "")
            torrents.append({"id": tid, "name": name, "save_path": save_path, "files": files, "label": label})
            
        return torrents
        
    def _collect_deluge_torrents_for_reportcards(self, deluge_cli: "DelugeClient") -> List[Dict]:
        """Collect torrent info for report card detection"""
        logger.debug("Collecting Deluge torrents for report cards")
        try:
            res = deluge_cli.update_ui(["name","tracker_status","save_path","download_location","label"])
            torrent_items = list((res.get("torrents") or {}).items())
        except Exception as e:
            logger.error("Failed to get torrents from Deluge: %s", e)
            return []
            
        torrents = []
        for tid, t in torrent_items:
            if self.stop_evt.is_set():
                break
            name = t.get("name","")
            tracker_status = t.get("tracker_status","") or ""
            save_path = t.get("save_path") or t.get("download_location") or ""
            label = t.get("label", "") or ""
            torrents.append({"id": tid, "name": name, "tracker_status": tracker_status, "save_path": save_path, "label": label})
            
        return torrents

    def _collect_qbit_files_for_orphans(self, qbit_cli: "QbitClient", idx: int, total: int) -> Tuple[Set[str], Dict[str, Dict]]:
        """Collect just the file paths from qBit for orphan detection"""
        logger.debug("Collecting qBit file paths for orphan detection")
        try:
            torrents = qbit_cli.torrents()
            total_torrents = len(torrents)
            logger.debug("Got %d torrents from qBit", total_torrents)
        except Exception as e:
            logger.error("Failed to get torrents from qBit: %s", e)
            return set(), {}
            
        host_files: Set[str] = set()
        file_metadata: Dict[str, Dict] = {}  # file_path -> torrent metadata
        path_in = self.cfg.get("Qbit_Path_In","")
        path_out = self.cfg.get("Qbit_Path_Out","")
        batch_size = 25
        processed = 0
        
        # Process torrents in batches of 25
        for i in range(0, len(torrents), batch_size):
            if self.stop_evt.is_set():
                logger.info("qBit file collection stopped by user")
                break
                
            batch = torrents[i:i + batch_size]
            last_torrent_name = ""
            
            for t in batch:
                if self.stop_evt.is_set():
                    break
                    
                thash = t.get("hash")
                name = t.get("name","") or thash[:8] if thash else "Unknown"
                last_torrent_name = name  # Track the last processed torrent name
                category = t.get("category", "") or ""
                tags = t.get("tags", "") or ""  # qBit returns comma-separated string
                
                # Create torrent metadata
                torrent_info = {
                    "client": "qbit",
                    "torrent_id": str(thash),
                    "torrent_name": name,
                    "label": category,  # Use category as label equivalent
                    "tags": tags
                }
                
                save_path = t.get("save_path","") or ""
                content_path = t.get("content_path") or ""
                base_root = normalize(content_path or save_path)
                
                try:
                    files = qbit_cli.files(thash)
                except Exception as e:
                    logger.warning("qBit files() failed for %s: %s", name, e)
                    files = []
                    
                for f in files:
                    file_name = (f.get("name") or "").strip()
                    if not file_name:
                        continue
                    # Use simple path join like test script - works for both single-file and multi-file torrents
                    full = normalize(os.path.join(save_path, file_name))
                    container_path = safe_realnorm(full)
                    host_files.add(container_path)
                    file_metadata[container_path] = torrent_info  # Store metadata for this file
                    
                processed += 1
            
            # Update progress with last torrent name from batch
            collection_percent = (processed / total_torrents) * 50.0  # 50% of this phase for collection
            self._set_progress(idx, total, f"qBit: {last_torrent_name} ({processed}/{total_torrents})", phase_progress=collection_percent)
            logger.debug("Processed batch ending with: %s (%d/%d torrents)", last_torrent_name, processed, total_torrents)
            
        logger.debug("Collected %d file paths from %d torrents", len(host_files), processed)
        return host_files, file_metadata
        
    def _collect_qbit_torrents_for_runaways(self, qbit_cli: "QbitClient") -> List[Dict]:
        """Collect torrent info for runaway detection"""
        logger.debug("Collecting qBit torrents for runaway detection")
        try:
            torrents = qbit_cli.torrents()
        except Exception as e:
            logger.error("Failed to get torrents from qBit: %s", e)
            return []
            
        out = []
        for t in torrents:
            if self.stop_evt.is_set():
                break
            thash = t.get("hash")
            name = t.get("name","") or ""
            save_path = t.get("save_path","") or ""
            content_path = t.get("content_path") or ""
            category = t.get("category", "")
            tags = t.get("tags", "")
            
            # Get files for this torrent (this was missing in original)
            try:
                files = qbit_cli.files(thash)
            except Exception as e:
                logger.warning("qBit files() failed for %s: %s", name, e)
                files = []
                
            out.append({"hash": thash, "name": name, "save_path": save_path, "content_path": content_path, "category": category, "tags": tags, "files": files})
            
        return out
        
    def _collect_qbit_torrents_for_reportcards(self, qbit_cli: "QbitClient") -> List[Dict]:
        """Collect torrent info for report cards"""
        logger.debug("Collecting qBit torrents for report cards")
        try:
            torrents = qbit_cli.torrents()
        except Exception as e:
            logger.error("Failed to get torrents from qBit: %s", e)
            return []
            
        out = []
        for t in torrents:
            if self.stop_evt.is_set():
                break
            thash = t.get("hash")
            name = t.get("name","") or ""
            save_path = t.get("save_path","") or ""
            category = t.get("category", "") or ""
            tags = t.get("tags", "") or ""
            out.append({"hash": thash, "name": name, "save_path": save_path, "category": category, "tags": tags})
            
        return out

    # -------- Phases with live progress --------

    def _phase_orphans_deluge(self, idx: int, total: int, deluge_cli: "DelugeClient", out_file: Path):
        root = normalize(self.cfg.get("Deluge_Torrent_Folder",""))
        if not os.path.exists(root):
            logger.warning("Deluge_Torrent_Folder does not exist inside container: %s", root)
        logger.info("Scanning Deluge orphans in %s", root)

        # Update progress before collecting files
        self._set_progress(idx, total, f"Collecting Deluge torrent files", phase_progress=0.0)
        
        client_files, file_metadata = self._collect_deluge_files_for_orphans(deluge_cli, idx, total)
        if self.stop_evt.is_set():
            raise ScanStopped
            
        logger.info("Collected %d files from Deluge, starting directory scan", len(client_files))
        processed = 0
        orphan_files: List[str] = []
        chunk_size = 1000  # Process 1000 files at a time

        try:
            for r, dirs, files in os.walk(root):
                if self.stop_evt.is_set():  # Add stop check at directory level
                    raise ScanStopped
                
                # Show current directory being scanned
                current_dir = r.replace(root, "").lstrip("/") or "root"
                
                # Process files in smaller batches
                for i in range(0, len(files), chunk_size):
                    if self.stop_evt.is_set():
                        raise ScanStopped
                        
                    file_batch = files[i:i + chunk_size]
                    for name in file_batch:
                        host_path = normalize(os.path.join(r, name))
                        # Convert external filesystem path to container path for comparison
                        path_in = self.cfg.get("Deluge_Path_In","")
                        path_out = self.cfg.get("Deluge_Path_Out","") 
                        container_path = safe_map_path(host_path, path_out, path_in)  # Reverse mapping
                        if container_path not in client_files:
                            # Check if we have metadata for this file (cross-client detection)
                            if container_path in file_metadata:
                                metadata = file_metadata[container_path]
                                enhanced_line = f"{metadata['client']}|{metadata['torrent_id']}|{metadata['torrent_name']}|{metadata['label']}|{metadata['tags']}|{host_path}"
                                orphan_files.append(enhanced_line)
                            else:
                                # True orphan - just the file path
                                orphan_files.append(host_path)
                        processed += 1
                        
                        # Check stop every 100 items to be responsive
                        if processed % 100 == 0 and self.stop_evt.is_set():
                            raise ScanStopped
                    
                    # Update progress every chunk instead of every file
                    # Calculate progress within this phase (50% collection, 50% scanning)
                    scan_progress = min(50.0, (processed / 1000) * 5.0) + 50.0  # Start at 50% after collection
                    self._set_progress(idx, total, f"Scanning Deluge orphans: {current_dir}/ (~{processed} processed)", phase_progress=scan_progress)
                    
        except OSError as e:
            logger.warning("OS error during directory scan: %s", e)
        except ScanStopped:
            logger.info("Deluge orphan scan stopped by user")
            raise

        logger.info("Deluge orphan scan completed: %d files processed, %d orphans found", processed, len(orphan_files))
        self._set_progress(idx, total, f"Finished scanning Deluge orphans ({processed} processed)", phase_progress=100.0)
        existing = _read_lines(out_file)
        write_lines(out_file, existing + orphan_files)
        logger.info("Deluge orphans written: %s", out_file)


    def _phase_orphans_qbit(self, idx: int, total: int, qbit_cli: "QbitClient", out_file: Path):
        # Update progress before collecting files
        self._set_progress(idx, total, f"Collecting qBit torrent files", phase_progress=0.0)
        
        client_files, file_metadata = self._collect_qbit_files_for_orphans(qbit_cli, idx, total)
        if self.stop_evt.is_set():
            raise ScanStopped
            
        root = safe_realnorm(self.cfg.get("Qbit_Torrent_Folder",""))
        logger.info("Collected %d files from qBit, scanning orphans in %s", len(client_files), root)

        processed = 0
        orphan_files: List[str] = []
        chunk_size = 1000  # Process 1000 files at a time

        try:
            for r, dirs, files in os.walk(root):
                if self.stop_evt.is_set():  # Add stop check at directory level
                    raise ScanStopped
                
                # Show current directory being scanned
                current_dir = r.replace(root, "").lstrip("/") or "root"
                
                # Process files in smaller batches
                for i in range(0, len(files), chunk_size):
                    if self.stop_evt.is_set():
                        raise ScanStopped
                        
                    file_batch = files[i:i + chunk_size]
                    for name in file_batch:
                        host_path = safe_realnorm(os.path.join(r, name))
                        # Convert external filesystem path to container path for comparison
                        path_in = self.cfg.get("Qbit_Path_In","")
                        path_out = self.cfg.get("Qbit_Path_Out","") 
                        container_path = safe_map_path(host_path, path_out, path_in)  # Reverse mapping
                        if container_path not in client_files:
                            # Check if we have metadata for this file (cross-client detection)
                            if container_path in file_metadata:
                                metadata = file_metadata[container_path]
                                enhanced_line = f"{metadata['client']}|{metadata['torrent_id']}|{metadata['torrent_name']}|{metadata['label']}|{metadata['tags']}|{host_path}"
                                orphan_files.append(enhanced_line)
                            else:
                                # True orphan - just the file path
                                orphan_files.append(host_path)
                        processed += 1
                        
                        # Check stop every 100 items to be responsive
                        if processed % 100 == 0 and self.stop_evt.is_set():
                            raise ScanStopped
                    
                    # Update progress every chunk instead of every file
                    # Calculate progress within this phase (50% collection, 50% scanning)
                    scan_progress = min(50.0, (processed / 1000) * 5.0) + 50.0  # Start at 50% after collection
                    self._set_progress(idx, total, f"Scanning qBit orphans: {current_dir}/ (~{processed} processed)", phase_progress=scan_progress)
                    
        except OSError as e:
            logger.warning("OS error during directory scan: %s", e)
        except ScanStopped:
            logger.info("qBit orphan scan stopped by user")
            raise

        self._set_progress(idx, total, f"Finished scanning qBit orphans ({processed} processed)", phase_progress=100.0)
        existing = _read_lines(out_file)
        write_lines(out_file, existing + orphan_files)
        logger.info("qBit orphans written: %s", out_file)


    def _phase_runaways_deluge(self, idx: int, total: int, deluge_cli: "DelugeClient", out_file: Path):
        torrents = self._collect_deluge_torrents_for_runaways(deluge_cli)
        total_items = sum(len(t.get("files", [])) for t in torrents) or 1
        processed = 0
        lines: List[str] = []

        for t in torrents:
            name = t.get("name","") or t.get("id","?")
            tid = str(t.get("id", ""))
            label = t.get("label", "")
            for f in t.get("files", []):
                processed += 1
                rel = f.get("path","") or ""
                save_path = t.get("save_path") or t.get("download_location") or ""
                full_path = normalize(os.path.join(save_path, rel))
                # For runaway detection, check file existence using container paths (no mapping needed)
                container_path = safe_realnorm(full_path)
                if not os.path.exists(container_path):
                    enhanced_line = f"deluge|{tid}|{name}|{label}||{container_path}"
                    lines.append(enhanced_line)

                phase_percent = (processed / total_items) * 100.0 if total_items > 0 else 0.0
                self._set_progress(idx, total, f"Checking Runaways in Deluge ({processed}/{total_items})", phase_progress=phase_percent)

        self._set_progress(idx, total, f"Finished checking Runaways in Deluge ({processed}/{total_items})", phase_progress=100.0)
        write_lines(out_file, lines)
        logger.info("Deluge runaways written: %s", out_file)


    def _phase_runaways_qbit(self, idx: int, total: int, qbit_cli: "QbitClient", out_file: Path):
        torrents = self._collect_qbit_torrents_for_runaways(qbit_cli)
        total_items = sum(len(t.get("files", [])) for t in torrents) or 1
        processed = 0
        lines: List[str] = []

        for t in torrents:
            name = t.get("name","") or t.get("hash")[:8]
            thash = str(t.get("hash", ""))
            category = t.get("category", "")
            tags = t.get("tags", "")
            for f in t.get("files", []):
                processed += 1
                rel = f.get("name") or ""
                save_path = t.get("save_path") or ""
                full_path = normalize(os.path.join(save_path, rel))
                # For runaway detection, check file existence using container paths (no mapping needed)
                container_path = safe_realnorm(full_path)
                if not robust_path_exists(container_path):
                    enhanced_line = f"qbit|{thash}|{name}|{category}|{tags}|{container_path}"
                    lines.append(enhanced_line)

                phase_percent = (processed / total_items) * 100.0 if total_items > 0 else 0.0
                self._set_progress(idx, total, f"Checking Runaways in qBit ({processed}/{total_items})", phase_progress=phase_percent)

        self._set_progress(idx, total, f"Finished checking Runaways in qBit ({processed}/{total_items})", phase_progress=100.0)
        write_lines(out_file, lines)
        logger.info("qBit runaways written: %s", out_file)


    def _phase_reportcards_deluge(self, idx: int, total: int, deluge_cli: "DelugeClient", out_file: Path):
        torrents_data = self._collect_deluge_torrents_for_reportcards(deluge_cli)
        torrents = [(t["id"], t) for t in torrents_data]
        total_items = len(torrents) or 1
        processed = 0
        lines: List[str] = []

        for tid, t in torrents:
            processed += 1
            name = t.get("name","")
            label = t.get("label", "")
            save_path = t.get("save_path", "")
            status = (t.get("tracker_status") or "").strip()
            low = status.lower()
            if low.startswith("error") or "error" in low:
                enhanced_line = f"deluge|{str(tid)}|{name}|{label}||{status}|{save_path}"
                lines.append(enhanced_line)

            phase_percent = (processed / total_items) * 100.0 if total_items > 0 else 0.0
            self._set_progress(idx, total, f"Collecting Report Cards in Deluge ({processed}/{total_items})", phase_progress=phase_percent)

        self._set_progress(idx, total, f"Finished collecting Report Cards in Deluge ({processed}/{total_items})", phase_progress=100.0)
        write_lines(out_file, lines)
        logger.info("Deluge report cards written: %s", out_file)


    def _phase_reportcards_qbit(self, idx: int, total: int, qbit_cli: "QbitClient", out_file: Path):
        torrents = self._collect_qbit_torrents_for_reportcards(qbit_cli)
        total_items = len(torrents) or 1
        processed = 0
        lines: List[str] = []

        for t in torrents:
            processed += 1
            thash = t.get("hash")
            name = t.get("name","")
            category = t.get("category", "")
            tags = t.get("tags", "")
            save_path = t.get("save_path", "")
            try:
                trackers = qbit_cli.trackers(thash)
            except Exception as e:
                logger.warning("qBit trackers failed for %s: %s", name, e)
                continue

            bad_msgs = []
            working_trackers = 0
            total_trackers = len(trackers)
            
            for tr in trackers:
                status = tr.get("status")
                msg = (tr.get("msg") or "").strip()
                low = msg.lower()
                
                # Count working trackers (status 2 = Working)
                if status == 2:
                    working_trackers += 1
                    continue
                    
                # Skip transient/temporary errors
                if _is_transient_tracker_error(low):
                    continue
                    
                # Only flag persistent/serious errors
                if status == 4 and not _is_transient_tracker_error(low):
                    bad_msgs.append(msg or "tracker error")
                    
            # Only report torrents that have NO working trackers and have actual errors
            if bad_msgs and working_trackers == 0:
                uniq = sorted({m for m in bad_msgs if m})
                error_msg = ' ; '.join(uniq)
                enhanced_line = f"qbit|{str(thash)}|{name}|{category}|{tags}|{error_msg}|{save_path}"
                lines.append(enhanced_line)

            phase_percent = (processed / total_items) * 100.0 if total_items > 0 else 0.0
            self._set_progress(idx, total, f"Collecting Report Cards in qBit ({processed}/{total_items})", phase_progress=phase_percent)

        self._set_progress(idx, total, f"Finished collecting Report Cards in qBit ({processed}/{total_items})", phase_progress=100.0)
        write_lines(out_file, lines)
        logger.info("qBit report cards written: %s", out_file)


    # --------------------------
    # Client logins
    # --------------------------

    def _login_qbit(self) -> "QbitClient":
        url = self.cfg["Qbit_Url"]
        user = self.cfg["Qbit_User"]
        pw = self.cfg["Qbit_Pass"]
        cli = QbitClient(url, user, pw)
        logger.info("Logging into qBittorrent at %s", url)
        cli.login()
        logger.info("qBittorrent login OK")
        return cli

    def _login_deluge(self) -> "DelugeClient":
        url = self.cfg["Deluge_Url"]
        pw = self.cfg["Deluge_Pass"]
        cli = DelugeClient(url, pw)
        logger.info("Logging into Deluge at %s", url)
        cli.login()
        logger.info("Deluge login OK")
        return cli

# --------------------------
# Module-level control
# --------------------------

_runner: Optional["ScannerRunner"] = None
_thread: Optional[threading.Thread] = None

def _check_scanner_timeout():
    """Check if scanner has been running too long and force stop"""
    global _runner, _thread
    if _thread and _thread.is_alive() and _runner:
        if hasattr(_runner, '_start_time') and _runner._start_time:
            if time.time() - _runner._start_time > 300:  # 5 minutes
                logger.warning("Scanner timeout - forcing stop")
                _runner.stop()

def start_scan(cfg: Dict):
    global _runner, _thread
    if _thread and _thread.is_alive():
        raise RuntimeError("Scan already running")
    _runner = ScannerRunner(cfg)
    _thread = threading.Thread(target=_runner.run, daemon=True)
    _thread.start()
    
    # Add timeout monitoring
    threading.Timer(300.0, _check_scanner_timeout).start()  # 5 min timeout
    logger.info("Scan started")

def stop_scan():
    global _runner, _thread
    if _runner:
        _runner.stop()
        logger.info("Stop requested")

def find_file_relationships(cfg: Dict, filename: str = "", torrent_name: str = "") -> List[Dict]:
    """Find relationships for a file or torrent across all clients using exact filename + file size matching"""
    relationships = []
    
    try:
        search_term = filename or torrent_name
        if not search_term:
            return relationships
            
        logger.debug("Finding file relationships for: %s", search_term)
        
        # Remove file extension for better torrent name matching  
        search_name = search_term
        if '.' in search_term and search_term.count('.') > 2:  # Keep things like "7.Tigers" but remove ".mkv"
            search_name = '.'.join(search_term.split('.')[:-1])
        
        # Get file size of the orphan file for exact matching
        orphan_file_size = _get_file_size(search_term)
        orphan_filename = search_term.split('/')[-1]
        
        if orphan_file_size == 0:
            logger.warning("Could not get file size for: %s", search_term)
            return relationships
            
        logger.debug("Looking for files named '%s' with size %d bytes", orphan_filename, orphan_file_size)
        
        # Search both clients for exact filename + size matches using existing working methods
        if cfg.get("Enable_Deluge", False):
            relationships.extend(_search_deluge_relationships_exact(cfg, orphan_filename, orphan_file_size, search_term))
        
        if cfg.get("Enable_Qbit", False):
            relationships.extend(_search_qbit_relationships_exact(cfg, orphan_filename, orphan_file_size, search_term))
            
    except Exception as e:
        logger.error("Error finding relationships: %s", e)
    
    return relationships

def _find_deluge_fuzzy_matches(cfg: Dict, search_name: str) -> List[Dict]:
    """Find fuzzy matches in Deluge using existing scanner infrastructure"""
    relationships = []
    
    try:
        # Use the existing login method from the scanner
        deluge_cli = _login_deluge_safe(cfg)
        if not deluge_cli:
            return relationships
            
        logger.debug("Scanning Deluge for fuzzy matches of: %s", search_name)
        
        # Get all torrents with metadata
        res = deluge_cli.update_ui(["name", "save_path", "download_location", "files", "label", "trackers"])
        torrent_items = list((res.get("torrents") or {}).items())
        
        for tid, t in torrent_items:
            torrent_name_full = t.get("name", "")
            label = t.get("label", "") or ""
            save_path = t.get("save_path") or t.get("download_location") or ""
            files = t.get("files") or []
            trackers = t.get("trackers") or []
            
            # Calculate similarity
            similarity = _calculate_fuzzy_similarity(search_name.lower(), torrent_name_full.lower())
            
            if similarity >= 0.85:
                logger.debug("Found Deluge match: '%s' (%.1f%% similarity)", torrent_name_full, similarity * 100)
                
                # Get file paths, prioritizing files that match the search term
                all_files = [f.get("path", "") for f in files]
                
                # Try to find files within this torrent that match the original search term
                file_matches = []
                for file_path in all_files:
                    file_name = file_path.split('/')[-1] if file_path else ""
                    file_similarity = _calculate_fuzzy_similarity(search_name.lower(), file_name.lower())
                    if file_similarity >= 0.75:  # Minimum threshold for individual files
                        file_matches.append((file_path, file_similarity))
                
                if file_matches:
                    # Sort by similarity and get the best match
                    file_matches.sort(key=lambda x: x[1], reverse=True)
                    best_match_path, best_similarity = file_matches[0]
                    best_match_name = best_match_path.split('/')[-1].lower()
                    
                    matching_files = [best_match_path]  # Always include the best match
                    
                    # Check if it's a TV show (has S##E## pattern)
                    import re
                    tv_match = re.search(r'S\d{1,2}E\d{1,2}', best_match_name, re.IGNORECASE)
                    if tv_match:
                        # TV show: find other files with same S##E## pattern
                        episode_pattern = tv_match.group(0).lower()
                        for file_path in all_files:
                            file_name = file_path.split('/')[-1].lower()
                            if file_path != best_match_path and episode_pattern in file_name:
                                matching_files.append(file_path)
                    else:
                        # Movie: find other files that start with similar title
                        # Extract first meaningful part before year/quality indicators
                        title_match = re.match(r'^([^(.\d]+)', best_match_name.replace('.', ' '))
                        if title_match:
                            movie_title = title_match.group(1).strip().lower()
                            for file_path in all_files:
                                file_name = file_path.split('/')[-1].lower().replace('.', ' ')
                                if file_path != best_match_path and file_name.startswith(movie_title):
                                    matching_files.append(file_path)
                    
                    file_paths = matching_files
                else:
                    # No specific file matches found, include all files (fallback)
                    file_paths = all_files
                
                # Get primary tracker
                primary_tracker = ""
                if trackers:
                    # Get the first tracker URL and extract domain
                    tracker_url = trackers[0].get("url", "") if isinstance(trackers[0], dict) else str(trackers[0])
                    primary_tracker = _extract_tracker_domain(tracker_url)
                
                relationships.append({
                    "client": "deluge",
                    "torrent_id": str(tid),
                    "torrent_name": torrent_name_full,
                    "label": label,
                    "tracker": primary_tracker,
                    "save_path": save_path,
                    "file_count": len(files),
                    "matching_files": file_paths,
                    "match_type": "fuzzy_torrent_name",
                    "similarity": similarity
                })
        
        # Note: DelugeClient doesn't have a disconnect method - requests session cleans up automatically
        
    except Exception as e:
        logger.error("Error in Deluge fuzzy matching: %s", e)
    
    return relationships

def _find_qbit_fuzzy_matches(cfg: Dict, search_name: str) -> List[Dict]:
    """Find fuzzy matches in qBittorrent using existing scanner infrastructure"""  
    relationships = []
    
    try:
        # Use the existing login method from the scanner
        qbit_cli = _login_qbit_safe(cfg)
        if not qbit_cli:
            return relationships
            
        logger.debug("Scanning qBittorrent for fuzzy matches of: %s", search_name)
        
        # Get all torrents
        torrents = qbit_cli.torrents()
        
        for t in torrents:
            torrent_hash = t.get("hash", "")
            torrent_name_full = t.get("name", "")
            category = t.get("category", "") or ""
            tags = t.get("tags", "") or ""
            save_path = t.get("save_path", "")
            
            # Calculate similarity
            similarity = _calculate_fuzzy_similarity(search_name.lower(), torrent_name_full.lower())
            
            if similarity >= 0.85:
                logger.debug("Found qBit match: '%s' (%.1f%% similarity)", torrent_name_full, similarity * 100)
                
                # Get file paths, prioritizing files that match the search term
                try:
                    files = qbit_cli.files(torrent_hash)
                    all_files = [f.get("name", "") for f in files]
                    
                    # Try to find files within this torrent that match the original search term
                    file_matches = []
                    for file_path in all_files:
                        file_name = file_path.split('/')[-1] if file_path else ""
                        file_similarity = _calculate_fuzzy_similarity(search_name.lower(), file_name.lower())
                        if file_similarity >= 0.75:  # Minimum threshold for individual files
                            file_matches.append((file_path, file_similarity))
                    
                    if file_matches:
                        # Sort by similarity and get the best match
                        file_matches.sort(key=lambda x: x[1], reverse=True)
                        best_match_path, best_similarity = file_matches[0]
                        best_match_name = best_match_path.split('/')[-1].lower()
                        
                        matching_files = [best_match_path]  # Always include the best match
                        
                        # Check if it's a TV show (has S##E## pattern)
                        import re
                        tv_match = re.search(r'S\d{1,2}E\d{1,2}', best_match_name, re.IGNORECASE)
                        if tv_match:
                            # TV show: find other files with same S##E## pattern
                            episode_pattern = tv_match.group(0).lower()
                            for file_path in all_files:
                                file_name = file_path.split('/')[-1].lower()
                                if file_path != best_match_path and episode_pattern in file_name:
                                    matching_files.append(file_path)
                        else:
                            # Movie: find other files that start with similar title
                            # Extract first meaningful part before year/quality indicators
                            title_match = re.match(r'^([^(.\d]+)', best_match_name.replace('.', ' '))
                            if title_match:
                                movie_title = title_match.group(1).strip().lower()
                                for file_path in all_files:
                                    file_name = file_path.split('/')[-1].lower().replace('.', ' ')
                                    if file_path != best_match_path and file_name.startswith(movie_title):
                                        matching_files.append(file_path)
                        
                        file_paths = matching_files
                    else:
                        # No specific file matches found, include all files (fallback)
                        file_paths = all_files
                except Exception as e:
                    logger.warning("Failed to get files for torrent %s: %s", torrent_name_full, e)
                    file_paths = []
                
                # Get tracker information
                primary_tracker = ""
                try:
                    trackers = qbit_cli.trackers(torrent_hash)
                    if trackers:
                        # Get the first non-DHT tracker
                        for tracker in trackers:
                            tracker_url = tracker.get("url", "")
                            if tracker_url and not tracker_url.startswith("**"):  # Skip DHT/PEX entries
                                primary_tracker = _extract_tracker_domain(tracker_url)
                                break
                except Exception as e:
                    logger.warning("Failed to get trackers for torrent %s: %s", torrent_name_full, e)
                
                relationships.append({
                    "client": "qbit",
                    "torrent_id": torrent_hash,
                    "torrent_name": torrent_name_full,
                    "label": category,
                    "tracker": primary_tracker,
                    "tags": tags,
                    "save_path": save_path,
                    "file_count": len(file_paths),
                    "matching_files": file_paths,
                    "match_type": "fuzzy_torrent_name",
                    "similarity": similarity
                })
        
        # Note: QbitClient doesn't have a disconnect method - requests session cleans up automatically
        
    except Exception as e:
        logger.error("Error in qBittorrent fuzzy matching: %s", e)
    
    return relationships

def _login_deluge_safe(cfg: Dict):
    """Safely login to Deluge (returns None if fails)"""
    try:
        deluge_cli = DelugeClient(cfg.get("Deluge_Url", ""), cfg.get("Deluge_Pass", ""))
        deluge_cli.login()
        logger.debug("Successfully connected to Deluge for relationships")
        return deluge_cli
    except Exception as e:
        logger.error("Failed to connect to Deluge for relationships: %s", e)
    return None

def _login_qbit_safe(cfg: Dict):
    """Safely login to qBittorrent (returns None if fails)"""  
    try:
        qbit_cli = QbitClient(cfg.get("Qbit_Url", ""), cfg.get("Qbit_User", ""), cfg.get("Qbit_Pass", ""))
        qbit_cli.login()
        logger.debug("Successfully connected to qBittorrent for relationships")
        return qbit_cli
    except Exception as e:
        logger.error("Failed to connect to qBittorrent for relationships: %s", e)
    return None

def _calculate_fuzzy_similarity(str1: str, str2: str) -> float:
    """Calculate fuzzy similarity between two strings with episode-aware matching"""
    try:
        from difflib import SequenceMatcher
        import re
        
        # Standard similarity check
        base_similarity = SequenceMatcher(None, str1, str2).ratio()
        
        # Episode-aware matching: check if one is an episode file and other is season pack
        # Pattern: S##E## followed by episode title
        episode_pattern = r'S\d{1,2}E\d{1,2}\.[^.]+\.'
        season_pattern = r'S\d{1,2}\.'
        
        has_episode1 = bool(re.search(episode_pattern, str1, re.IGNORECASE))
        has_episode2 = bool(re.search(episode_pattern, str2, re.IGNORECASE))
        has_season1 = bool(re.search(season_pattern, str1, re.IGNORECASE)) and not has_episode1
        has_season2 = bool(re.search(season_pattern, str2, re.IGNORECASE)) and not has_episode2
        
        # If one is episode and other is season pack, do episode-aware comparison
        if (has_episode1 and has_season2) or (has_episode2 and has_season1):
            # Remove episode-specific parts for comparison
            # Replace S##E##.EpisodeTitle. with S##.
            if has_episode1:
                episode_stripped = re.sub(episode_pattern, lambda m: m.group(0)[:4] + '.', str1, flags=re.IGNORECASE)
                episode_similarity = SequenceMatcher(None, episode_stripped, str2).ratio()
            else:
                episode_stripped = re.sub(episode_pattern, lambda m: m.group(0)[:4] + '.', str2, flags=re.IGNORECASE)
                episode_similarity = SequenceMatcher(None, str1, episode_stripped).ratio()
            
            # Use the higher similarity
            return max(base_similarity, episode_similarity)
        
        return base_similarity
        
    except Exception as e:
        # Fallback to simple containment check
        if str1 in str2 or str2 in str1:
            return 0.9
        return 0.0

def _extract_tracker_domain(tracker_url: str) -> str:
    """Extract domain from tracker URL"""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(tracker_url)
        domain = parsed.netloc or parsed.path
        # Remove port numbers and common prefixes
        if ':' in domain:
            domain = domain.split(':')[0]
        if domain.startswith('www.'):
            domain = domain[4:]
        return domain
    except (ValueError, IndexError, AttributeError):
        # Fallback - just return first part after protocol
        if '://' in tracker_url:
            try:
                return tracker_url.split('://')[1].split('/')[0].split(':')[0]
            except IndexError:
                return tracker_url
        return tracker_url

def find_runaway_relationship(cfg: Dict, runaway_line: str) -> Dict:
    """Find torrent information for a runaway file based on the runaway line data"""
    logger.debug("Finding runaway relationship for: %s", runaway_line[:100])
    
    try:
        # Parse the runaway line: client|torrent_id|torrent_name|category_or_label|tags|file_path
        parts = runaway_line.split('|', 5)
        if len(parts) < 6:
            logger.warning("Invalid runaway line format: %s", runaway_line)
            return {}
        
        client, torrent_id, torrent_name, label, tags, file_path = parts
        
        # Get torrent details from the appropriate client
        if client == "deluge" and cfg.get("Enable_Deluge", False):
            return _get_deluge_runaway_info(cfg, torrent_id, torrent_name, label, tags, file_path)
        elif client == "qbit" and cfg.get("Enable_Qbit", False):
            return _get_qbit_runaway_info(cfg, torrent_id, torrent_name, label, tags, file_path)
        else:
            logger.warning("Client %s not enabled or supported", client)
            return {}
            
    except Exception as e:
        logger.error("Error finding runaway relationship: %s", e)
        return {}

def _get_deluge_runaway_info(cfg: Dict, torrent_id: str, torrent_name: str, label: str, tags: str, file_path: str) -> Dict:
    """Get Deluge torrent info for runaway"""
    try:
        deluge_cli = _login_deluge_safe(cfg)
        if not deluge_cli:
            return {}
            
        # Get torrent details including trackers
        res = deluge_cli.update_ui(["name", "save_path", "download_location", "trackers", "label"])
        torrent_data = res.get("torrents", {}).get(torrent_id)
        
        if not torrent_data:
            logger.warning("Torrent %s not found in Deluge", torrent_id)
            return {}
        
        # Get tracker info
        trackers = torrent_data.get("trackers") or []
        primary_tracker = ""
        if trackers:
            tracker_url = trackers[0].get("url", "") if isinstance(trackers[0], dict) else str(trackers[0])
            primary_tracker = _extract_tracker_domain(tracker_url)
        
        # Note: DelugeClient doesn't have a disconnect method - requests session cleans up automatically
        
        return {
            "client": "deluge",
            "torrent_id": torrent_id,
            "torrent_name": torrent_name,
            "label": label or "None",
            "tracker": primary_tracker or "Unknown",
            "missing_file": file_path,
            "save_path": torrent_data.get("save_path") or torrent_data.get("download_location") or ""
        }
        
    except Exception as e:
        logger.error("Error getting Deluge runaway info: %s", e)
        return {}

def _get_qbit_runaway_info(cfg: Dict, torrent_hash: str, torrent_name: str, category: str, tags: str, file_path: str) -> Dict:
    """Get qBittorrent torrent info for runaway"""
    try:
        qbit_cli = _login_qbit_safe(cfg)
        if not qbit_cli:
            return {}
            
        # Get torrent details
        torrents = qbit_cli.torrents(hashes=torrent_hash)
        if not torrents:
            logger.warning("Torrent %s not found in qBittorrent", torrent_hash)
            return {}
        
        torrent_data = torrents[0]
        
        # Get tracker info
        primary_tracker = ""
        try:
            trackers = qbit_cli.trackers(torrent_hash)
            if trackers:
                for tracker in trackers:
                    tracker_url = tracker.get("url", "")
                    if tracker_url and not tracker_url.startswith("**"):  # Skip DHT/PEX entries
                        primary_tracker = _extract_tracker_domain(tracker_url)
                        break
        except Exception as e:
            logger.warning("Failed to get trackers for torrent %s: %s", torrent_name, e)
        
        # Note: QbitClient doesn't have a disconnect method - requests session cleans up automatically
        
        return {
            "client": "qbit", 
            "torrent_id": torrent_hash,
            "torrent_name": torrent_name,
            "label": category or "None",
            "tracker": primary_tracker or "Unknown",
            "missing_file": file_path,
            "save_path": torrent_data.get("save_path", "")
        }
        
    except Exception as e:
        logger.error("Error getting qBittorrent runaway info: %s", e)
        return {}

def _search_deluge_relationships(cfg: Dict, filename: str, torrent_name: str) -> List[Dict]:
    """Search Deluge for file/torrent relationships"""
    relationships = []
    
    try:
        deluge_cli = DelugeClient(cfg.get("Deluge_Url", ""), cfg.get("Deluge_Pass", ""))
        
        # Get all torrents with metadata
        res = deluge_cli.update_ui(["name","save_path","download_location","files","label"])
        torrent_items = list((res.get("torrents") or {}).items())
        
        for tid, t in torrent_items:
            name = t.get("name", "")
            label = t.get("label", "") or ""
            save_path = t.get("save_path") or t.get("download_location") or ""
            files = t.get("files") or []
            
            # Check if this torrent matches our search
            torrent_matches = torrent_name and torrent_name.lower() in name.lower()
            
            # Check if any files match our filename search
            file_matches = []
            if filename:
                filename_lower = filename.lower()
                for f in files:
                    file_path = f.get("path", "")
                    if filename_lower in file_path.lower():
                        file_matches.append(file_path)
            
            if torrent_matches or file_matches:
                relationships.append({
                    "client": "deluge",
                    "torrent_id": str(tid),
                    "torrent_name": name,
                    "label": label,
                    "save_path": save_path,
                    "file_count": len(files),
                    "matching_files": file_matches if filename else [],
                    "match_type": "torrent_name" if torrent_matches else "filename"
                })
                
    except Exception as e:
        logger.error("Error searching Deluge relationships: %s", e)
    
    return relationships

def _search_qbit_relationships(cfg: Dict, filename: str, torrent_name: str) -> List[Dict]:
    """Search qBittorrent for file/torrent relationships"""
    relationships = []
    
    try:
        qbit_cli = QbitClient(cfg.get("Qbit_Url", ""), cfg.get("Qbit_User", ""), cfg.get("Qbit_Pass", ""))
        
        # Get all torrents
        torrents = qbit_cli.torrents()
        
        for t in torrents:
            thash = t.get("hash", "")
            name = t.get("name", "")
            category = t.get("category", "") or ""
            tags = t.get("tags", "") or ""
            save_path = t.get("save_path", "")
            
            # Check if this torrent matches our search
            torrent_matches = torrent_name and torrent_name.lower() in name.lower()
            
            # Check if any files match our filename search
            file_matches = []
            if filename:
                try:
                    files = qbit_cli.files(thash)
                    filename_lower = filename.lower()
                    for f in files:
                        file_name = f.get("name", "")
                        if filename_lower in file_name.lower():
                            file_matches.append(file_name)
                except Exception as e:
                    logger.warning("Failed to get files for torrent %s: %s", name, e)
                    continue
            
            # Get total file count
            try:
                total_files = len(qbit_cli.files(thash)) if not file_matches else len(file_matches)
            except Exception:
                total_files = 0
            
            if torrent_matches or file_matches:
                relationships.append({
                    "client": "qbit", 
                    "torrent_id": str(thash),
                    "torrent_name": name,
                    "label": category,
                    "tags": tags,
                    "save_path": save_path,
                    "file_count": total_files,
                    "matching_files": file_matches if filename else [],
                    "match_type": "torrent_name" if torrent_matches else "filename"
                })
                
    except Exception as e:
        logger.error("Error searching qBittorrent relationships: %s", e)
    
    return relationships

def _get_file_size(file_path: str) -> int:
    """Get file size in bytes"""
    try:
        return os.path.getsize(file_path)
    except OSError:
        return 0

def _search_deluge_relationships_exact(cfg: Dict, filename: str, file_size: int, orphan_path: str) -> List[Dict]:
    """Search Deluge for exact filename + size matches using existing connection method"""
    relationships = []
    
    try:
        # Use existing working login method
        deluge_cli = _login_deluge_safe(cfg)
        if not deluge_cli:
            return relationships
            
        logger.debug("Scanning Deluge for exact matches of: %s (%d bytes)", filename, file_size)
        
        # Get all torrents with metadata including trackers
        res = deluge_cli.update_ui(["name", "save_path", "download_location", "files", "label", "trackers"])
        torrent_items = list((res.get("torrents") or {}).items())
        
        for tid, t in torrent_items:
            name = t.get("name", "")
            label = t.get("label", "") or ""
            save_path = t.get("save_path") or t.get("download_location") or ""
            files = t.get("files") or []
            trackers = t.get("trackers") or []
            
            # Get primary tracker
            primary_tracker = ""
            if trackers:
                # Get the first tracker URL and extract domain
                tracker_url = trackers[0].get("url", "") if isinstance(trackers[0], dict) else str(trackers[0])
                primary_tracker = _extract_tracker_domain(tracker_url)
            
            # Check each file for exact filename + size match
            for f in files:
                file_path = f.get("path", "")
                file_name = file_path.split('/')[-1] if file_path else ""
                torrent_file_size = f.get("size", 0)
                
                # Exact match on filename and size
                if file_name == filename and torrent_file_size == file_size:
                    full_path = os.path.join(save_path, file_path)
                    
                    # Extract title from directory path to ensure matches are from same content
                    orphan_title = _extract_title_from_path(orphan_path)
                    match_title = _extract_title_from_path(full_path)
                    
                    # Only include if titles match (same content)
                    if _titles_match(orphan_title, match_title):
                        relationships.append({
                            "client": "deluge",
                            "torrent_id": str(tid),
                            "torrent_name": name,
                            "label": label,
                            "tracker": primary_tracker or "Unknown",
                            "save_path": save_path,
                            "matching_file": full_path,
                            "match_type": "exact_file_match",
                            "orphan_title": orphan_title,
                            "match_title": match_title
                        })
                        logger.debug("Found Deluge exact match: %s in torrent %s (tracker: %s) - titles match: %s", full_path, name, primary_tracker or "Unknown", orphan_title)
                    else:
                        logger.debug("Skipped Deluge match: different titles - orphan: %s, match: %s", orphan_title, match_title)
                    
    except Exception as e:
        logger.error("Error searching Deluge for exact matches: %s", e)
    
    return relationships

def _search_qbit_relationships_exact(cfg: Dict, filename: str, file_size: int, orphan_path: str) -> List[Dict]:
    """Search qBittorrent for exact filename + size matches using existing connection method"""
    relationships = []
    
    try:
        # Use existing working login method
        qbit_cli = _login_qbit_safe(cfg)
        if not qbit_cli:
            return relationships
            
        logger.debug("Scanning qBittorrent for exact matches of: %s (%d bytes)", filename, file_size)
        
        # Get all torrents using existing method
        torrents = qbit_cli.torrents()
        
        for t in torrents:
            thash = t.get("hash", "")
            name = t.get("name", "")
            category = t.get("category", "") or ""
            tags = t.get("tags", "") or ""
            save_path = t.get("save_path", "")
            
            # Get tracker information
            primary_tracker = ""
            try:
                trackers = qbit_cli.trackers(thash)
                if trackers:
                    # Get the first non-DHT tracker
                    for tracker in trackers:
                        tracker_url = tracker.get("url", "")
                        if tracker_url and not tracker_url.startswith("**"):  # Skip DHT/PEX entries
                            primary_tracker = _extract_tracker_domain(tracker_url)
                            break
            except Exception as e:
                logger.warning("Failed to get trackers for torrent %s: %s", name, e)
            
            try:
                # Get torrent files using existing method
                files = qbit_cli.files(thash)
                for f in files:
                    file_path = f.get("name", "")
                    file_name = file_path.split('/')[-1] if file_path else ""
                    torrent_file_size = f.get("size", 0)
                    
                    # Exact match on filename and size
                    if file_name == filename and torrent_file_size == file_size:
                        full_path = os.path.join(save_path, file_path)
                        
                        # Extract title from directory path to ensure matches are from same content
                        orphan_title = _extract_title_from_path(orphan_path)
                        match_title = _extract_title_from_path(full_path)
                        
                        # Only include if titles match (same content)
                        if _titles_match(orphan_title, match_title):
                            relationships.append({
                                "client": "qbit",
                                "torrent_id": str(thash),
                                "torrent_name": name,
                                "label": category,
                                "tracker": primary_tracker or "Unknown",
                                "tags": tags,
                                "save_path": save_path,
                                "matching_file": full_path,
                                "match_type": "exact_file_match",
                                "orphan_title": orphan_title,
                                "match_title": match_title
                            })
                            logger.debug("Found qBittorrent exact match: %s in torrent %s (tracker: %s) - titles match: %s", full_path, name, primary_tracker or "Unknown", orphan_title)
                        else:
                            logger.debug("Skipped qBittorrent match: different titles - orphan: %s, match: %s", orphan_title, match_title)
                        
            except Exception as e:
                logger.warning("Failed to get files for torrent %s: %s", name, e)
                continue
                
    except Exception as e:
        logger.error("Error searching qBittorrent for exact matches: %s", e)
    
    return relationships

def _extract_title_from_path(file_path: str) -> str:
    """Extract title from directory path (e.g. Cant.Hardly.Wait.1998 from full path)"""
    try:
        import re
        
        # Split path and look for the main content directory
        path_parts = file_path.replace('\\', '/').split('/')
        
        for part in path_parts:
            # Look for directory that looks like a movie/show title
            # Pattern: Title.Year.* or Title.S##.* or just Title.*
            if re.match(r'^[A-Za-z0-9][A-Za-z0-9\.\-\s]+\.(19|20)\d{2}', part):  # Movie with year
                # Extract title part before year
                match = re.match(r'^([A-Za-z0-9\.\-\s]+)\.(19|20)\d{2}', part)
                if match:
                    return match.group(1).replace('.', ' ').strip().lower()
            elif re.match(r'^[A-Za-z0-9][A-Za-z0-9\.\-\s]+\.S\d{1,2}', part):  # TV show with season
                # Extract title part before season
                match = re.match(r'^([A-Za-z0-9\.\-\s]+)\.S\d{1,2}', part)
                if match:
                    return match.group(1).replace('.', ' ').strip().lower()
            elif len(part) > 5 and not part.startswith('.') and '.' in part:  # General title
                # Clean up common separators and return
                title = part.replace('.', ' ').replace('-', ' ').strip().lower()
                # Remove common suffixes
                title = re.sub(r'\s+(complete|uhd|bluray|1080p|2160p|x264|x265|hevc|h264|h265).*$', '', title, flags=re.IGNORECASE)
                if len(title) > 3:
                    return title
        
        # Fallback: use last meaningful directory name
        meaningful_parts = [p for p in path_parts if p and not p.startswith('.') and len(p) > 3]
        if meaningful_parts:
            return meaningful_parts[-1].replace('.', ' ').replace('-', ' ').strip().lower()
            
    except Exception as e:
        logger.warning("Error extracting title from path %s: %s", file_path, e)
    
    return ""

def _is_transient_tracker_error(msg: str) -> bool:
    """Check if a tracker error message is transient/temporary"""
    if not msg:
        return False
    
    msg_lower = msg.lower()
    
    # Common transient errors that should not trigger report cards
    transient_patterns = [
        "bad gateway",
        "gateway timeout", 
        "service unavailable",
        "connection timed out",
        "connection refused",
        "network unreachable",
        "temporary failure",
        "server overloaded",
        "too many requests",
        "rate limit",
        "maintenance",
        "temporarily unavailable",
        "503 service",
        "502 bad gateway",
        "504 gateway timeout",
        "timeout",
        "connection reset",
        "dns resolution failed"
    ]
    
    for pattern in transient_patterns:
        if pattern in msg_lower:
            return True
            
    return False

def _titles_match(title1: str, title2: str) -> bool:
    """Check if two titles represent the same content"""
    if not title1 or not title2:
        return False
    
    # Normalize titles
    t1 = title1.lower().strip()
    t2 = title2.lower().strip()
    
    # Exact match
    if t1 == t2:
        return True
    
    # Check if one title is contained in the other (handles variations)
    if len(t1) >= 5 and len(t2) >= 5:
        if t1 in t2 or t2 in t1:
            return True
    
    # Check for similar titles with small differences (typos, punctuation)
    import difflib
    similarity = difflib.SequenceMatcher(None, t1, t2).ratio()
    return similarity >= 0.85

def status() -> Dict:
    global _runner, _thread
    running = bool(_thread and _thread.is_alive())
    prog = _runner.progress if _runner else {"phase":0,"total":0,"percent":0.0,"label":"Idle"}
    return {
        "running": running,
        "progress": prog,
        "error": _runner.last_error if _runner else None,
        "timestamp": _runner.last_timestamp if _runner else None,
    }
