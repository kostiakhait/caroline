##################################################################################
#### Camerlengo Universal Swiss Toolkit and Web Server                        ####
#### Copyright (C) Konstantin Khait @ Partners Solutions, 2015-2025           ####
#### All rights reserved                                                      ####
#### Must be licensed before use                                              ####
##################################################################################

import Config
import os
import json
import time
import hashlib
import threading
import traceback
import copy
import uuid

from JSONStorage import JSONStorage

# Per-file write locks to prevent concurrent writers on the same file
_write_locks: dict = {}
_write_locks_mutex = threading.Lock()

def _get_write_lock(path: str) -> threading.Lock:
    key = os.path.abspath(path)
    with _write_locks_mutex:
        if key not in _write_locks:
            _write_locks[key] = threading.Lock()
        return _write_locks[key]


# === internal helpers ===

def _safe_read_json(path: str):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except json.JSONDecodeError:
        # Corrupted file — try to recover the first valid JSON object
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
            decoder = json.JSONDecoder()
            obj, _ = decoder.raw_decode(raw)
            if isinstance(obj, dict):
                print(f"[Cache] Recovered partial JSON from {path}")
                return obj
        except Exception:
            pass
        traceback.print_exc()
    except Exception:
        traceback.print_exc()
    return {}

def _safe_write_json(path: str, obj: dict, attempts: int = 5, initialDelay: float = 0.01):
    """
    Best-effort JSON writer with atomic replace (temp file → os.replace).
    Uses a per-file lock + unique tmp filename to prevent concurrent-write collisions on Windows.
    Does not raise on failure, only logs errors.
    """
    file_lock = _get_write_lock(path)
    with file_lock:
        lastExc = None
        delay = initialDelay

        for attempt in range(attempts):
            tmp_path = path + f".{uuid.uuid4().hex}.tmp"
            try:
                cloned = copy.deepcopy(obj) if obj is not None else {}
                directory = os.path.dirname(path) or "."
                os.makedirs(directory, exist_ok=True)

                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(cloned or {}, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, path)

                return
            except Exception as e:
                lastExc = e
                traceback.print_exc()
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass
                if attempt < attempts - 1:
                    time.sleep(delay)
                    delay = min(delay * 1.5, 0.2)

        if lastExc is not None:
            print(f"[ERROR] Failed to write JSON to {path} after {attempts} attempts: {lastExc}")


class ObjectCache:
    """
    Persistent JSON object cache.

    Contract:
      - The on-disk JSON is a plain mapping:
            { "<key>": <json-serializable object>, ... }
        No additional envelope / metadata is required or added.
      - The caller is responsible for key design and for embedding any metadata
        (e.g., timestamps) inside the stored objects if needed.

    Storage is backed by JSONStorage for atomic writes + backups.
    """

    def __init__(self, filePath: str, maxBackups: int = 5):
        self.filePath = filePath
        self.maxBackups = maxBackups
        self._lock = threading.RLock()

    def _ensureParentDir(self) -> None:
        try:
            directory = os.path.dirname(self.filePath) or "."
            os.makedirs(directory, exist_ok=True)
        except Exception:
            traceback.print_exc()

    def _load(self) -> dict:
        try:
            self._ensureParentDir()
            storage = JSONStorage(self.filePath, maxBackups=self.maxBackups)
            data = storage.load()
            return data if isinstance(data, dict) else {}
        except Exception:
            traceback.print_exc()
            return {}

    def _save(self, data: dict) -> None:
        try:
            self._ensureParentDir()
            storage = JSONStorage(self.filePath, maxBackups=self.maxBackups)
            storage.save(copy.deepcopy(data) if isinstance(data, dict) else {})
        except Exception:
            traceback.print_exc()

    def get(self, key: str, default=None):
        with self._lock:
            try:
                data = self._load()
                return data.get(key, default)
            except Exception:
                traceback.print_exc()
                return default

    def has(self, key: str) -> bool:
        with self._lock:
            try:
                data = self._load()
                return key in data
            except Exception:
                traceback.print_exc()
                return False

    def put(self, key: str, obj) -> None:
        with self._lock:
            try:
                data = self._load()
                data[key] = obj
                self._save(data)
            except Exception:
                traceback.print_exc()

    def delete(self, key: str) -> bool:
        with self._lock:
            try:
                data = self._load()
                if key in data:
                    del data[key]
                    self._save(data)
                    return True
                return False
            except Exception:
                traceback.print_exc()
                return False

    def keys(self):
        with self._lock:
            try:
                data = self._load()
                return list(data.keys())
            except Exception:
                traceback.print_exc()
                return []

    def items(self):
        with self._lock:
            try:
                data = self._load()
                return list(data.items())
            except Exception:
                traceback.print_exc()
                return []

    def loadAll(self) -> dict:
        """Returns a COPY of the full mapping."""
        with self._lock:
            try:
                return copy.deepcopy(self._load())
            except Exception:
                traceback.print_exc()
                return {}

    def saveAll(self, mapping: dict) -> None:
        """Overwrites cache with the provided mapping."""
        with self._lock:
            try:
                self._save(mapping if isinstance(mapping, dict) else {})
            except Exception:
                traceback.print_exc()

    def updateAll(self, updateFn):
        """
        Atomically load -> call updateFn(data) -> save.

        updateFn must return the updated mapping OR mutate and return None.
        """
        with self._lock:
            try:
                data = self._load()
                newData = updateFn(data)
                if isinstance(newData, dict):
                    data = newData
                self._save(data)
                return True
            except Exception:
                traceback.print_exc()
                return False


# === translations ===

def loadTranslations(translations: dict):
    """Mutates given dict; does NOT rebind it."""
    pname = Config.STORAGE
    fname = os.path.join(pname, "ai_trans.json")
    try:
        data = _safe_read_json(fname)
        translations.clear()
        translations.update(data)
    except Exception:
        traceback.print_exc()

def saveTranslations(translations: dict):
    pname = Config.STORAGE
    fname = os.path.join(pname, "ai_trans.json")
    try:
        _safe_write_json(fname, translations)
    except Exception:
        traceback.print_exc()


# === instructions ===

def loadInstructions(instructions: dict):
    """Mutates given dict; does NOT rebind it."""
    pname = Config.STORAGE
    fname = os.path.join(pname, "ai_instructions.json")
    try:
        data = _safe_read_json(fname)
        instructions.clear()
        instructions.update(data)
    except Exception:
        traceback.print_exc()

def saveInstructions(instructions: dict):
    pname = Config.STORAGE
    fname = os.path.join(pname, "ai_instructions.json")
    try:
        _safe_write_json(fname, instructions)
    except Exception:
        traceback.print_exc()


# === TTS cache ===

def loadTtsCache(tts_cache: dict):
    """
    Loads TTS cache from disk and prunes expired entries.
    IMPORTANT: mutates the provided dict (clear()/update()), does not rebind it.
    """
    pname = getattr(Config, "TTS_STORAGE", Config.STORAGE)
    fname = os.path.join(pname, "ai_tts_files.json")
    ttl = getattr(Config, "TTS_CACHE_TTL", 24 * 3600)

    try:
        data = _safe_read_json(fname)
        now = time.time()
        pruned = {k: v for k, v in (data or {}).items() if now - v.get("ts", 0) <= ttl}

        tts_cache.clear()
        tts_cache.update(pruned)
    except Exception:
        traceback.print_exc()

def saveTtsCache(tts_cache: dict):
    pname = getattr(Config, "TTS_STORAGE", Config.STORAGE)
    fname = os.path.join(pname, "ai_tts_files.json")
    try:
        _safe_write_json(fname, tts_cache)
    except Exception:
        traceback.print_exc()

def ttsCacheKey(text: str, voice: str) -> str:
    base = f"{text}|{voice}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()

def delete_tts_entry(key: str, entry: dict):
    path = entry.get("path")
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        traceback.print_exc()

def janitor_tts_loop():
    """
    Periodically:
      1) Removes expired records from cache file and deletes corresponding audio files.
      2) Sweeps stray files in audio_dir older than TTL or not referenced by cache.
    """
    pname = getattr(Config, "TTS_STORAGE", Config.STORAGE)
    cache_file = os.path.join(pname, "ai_tts_files.json")
    audio_dir = os.path.join(pname, "tts_audio")
    ttl = getattr(Config, "TTS_CACHE_TTL", 24 * 3600)
    interval = getattr(Config, "TTS_JANITOR_INTERVAL", 3600)

    try:
        os.makedirs(audio_dir, exist_ok=True)
    except Exception:
        pass

    while True:
        try:
            now = time.time()
            changed = False

            remove_tts_cache = _safe_read_json(cache_file)

            for k, v in list(remove_tts_cache.items()):
                ts = v.get("ts", 0)
                if now - ts > ttl:
                    delete_tts_entry(k, v)
                    remove_tts_cache.pop(k, None)
                    changed = True

            if changed:
                _safe_write_json(cache_file, remove_tts_cache)

            if os.path.isdir(audio_dir):
                referenced_paths = {rec.get("path") for rec in remove_tts_cache.values() if isinstance(rec, dict)}
                for root, _, files in os.walk(audio_dir):
                    for f in files:
                        path = os.path.join(root, f)
                        try:
                            st = os.stat(path)
                            too_old = (now - st.st_mtime) > ttl
                            unreferenced = (path not in referenced_paths)
                            if too_old or unreferenced:
                                os.remove(path)
                        except Exception:
                            traceback.print_exc()
        except Exception:
            traceback.print_exc()

        time.sleep(interval)

def startTtsJanitor():
    t = threading.Thread(target=janitor_tts_loop, name="tts-janitor", daemon=True)
    t.start()

TTS_CACHE = {}
try:
    loadTtsCache(TTS_CACHE)
except Exception:
    traceback.print_exc()

def getTtsCache() -> dict:
    """Returns a reference to the module-level singleton TTS cache dict."""
    return TTS_CACHE


## Class responsible for caching file contents.
class FileCache:
    ## Constructor for the FileCache class.
    # Initializes the cache and sets the host for configuration.
    # @param host The host name for retrieving configuration settings.
    def __init__(self, host):
        self.reset()
        self.host = host

    ## Checks if a file is present in the cache.
    # @param fname The name of the file to check.
    # @return True if the file is in the cache, False otherwise.
    def check(self, fname):
        try:
            return fname in self.cache.keys()
        except Exception:
            traceback.print_exc()
            return False

    ## Writes content to the cache for a given file name.
    # If the cache size exceeds the maximum limit, it removes the oldest items.
    # @param fname The name of the file.
    # @param content The content to cache.
    def write(self, fname, content):
        try:
            if len(content) < Config.getSiteConfig("max_cache_size", self.host):
                self.cache[fname] = content
                self.cache_keys.append(fname)
                self.total_cache_size += len(content)
        except Exception:
            traceback.print_exc()
        try:
            while self.total_cache_size > Config.getSiteConfig("max_cache_size", self.host):
                key = self.cache_keys[0]
                self.total_cache_size -= len(self.cache.get(key, b""))
                if key in self.cache:
                    del self.cache[key]
                self.cache_keys = self.cache_keys[1:]
        except Exception:
            traceback.print_exc()

    ## Reads and returns the content of a file from the cache.
    # @param fname The name of the file to read.
    # @return The content of the file if it's in the cache, empty string otherwise.
    def read(self, fname):
        try:
            if fname in self.cache.keys():
                return self.cache[fname]
            else:
                return ""
        except Exception:
            traceback.print_exc()
            return ""

    ## Removes a single file from the cache (call after writing or deleting the file on disk).
    # @param fname The absolute file path (same key used by write/check/read).
    def remove(self, fname):
        try:
            if fname in self.cache:
                self.total_cache_size -= len(self.cache[fname])
                del self.cache[fname]
            if fname in self.cache_keys:
                self.cache_keys.remove(fname)
        except Exception:
            traceback.print_exc()

    ## Resets the cache by clearing all cached content.
    def reset(self):
        self.cache = {}
        self.cache_keys = []
        self.total_cache_size = 0

    ## Checks if caching is disabled for a specific directory path.
    # @param path The directory path to check.
    # @return True if caching is disabled for the path, False otherwise.
    def is_disabled(self, path):
        try:
            for directory in Config.getSiteConfig("disable_cache", self.host):
                if path.find(directory) == 0:
                    return True
        except Exception:
            traceback.print_exc()
        return False
