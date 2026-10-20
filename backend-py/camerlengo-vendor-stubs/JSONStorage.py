##################################################################################
#### Camerlengo Universal Swiss Toolkit and Web Server                        ####
#### Copyright (C) Konstantin Khait @ Partners Solutions, 2015-2025           ####
#### All rights reserved                                                      ####
#### Must be licensed before use                                              ####
##################################################################################

import json
import os
import threading
import time
from pathlib import Path
import traceback
import shutil
import tempfile
import errno
import stat
import sys

class JSONStorage:
    """
    Потокобезопасное, устойчивое к сбоям хранилище JSON-файла с атомарной записью.
    - один RLock на путь в рамках процесса (serialize load/save между потоками)
    - запись: tmp -> fsync(tmp) -> atomic replace на POSIX / safe copy+retry на Windows -> fsync(dir)
    - перед записью делаются ротационные бэкапы *.bak1..*.bakN
    - при JSONDecodeError делается recovery с предыдущего валидного бэкапа
    """

    _locksRegistry = {}                # abs_path -> RLock
    _registryGuard = threading.Lock()  # guards the registry itself

    @classmethod
    def _lockFor(cls, path: Path) -> threading.RLock:
        key = str(path.resolve())
        with cls._registryGuard:
            lock = cls._locksRegistry.get(key)
            if lock is None:
                lock = threading.RLock()
                cls._locksRegistry[key] = lock
            return lock

    def __init__(self, filename, maxBackups: int = 3):
        self.basePath = Path(filename)
        self.maxBackups = max(0, int(maxBackups))
        self._lock = self._lockFor(self.basePath)

    @staticmethod
    def _fsyncDir(dirpath: str) -> None:
        try:
            dirfd = os.open(dirpath, os.O_RDONLY)
        except Exception:
            return
        try:
            try:
                os.fsync(dirfd)
            except Exception:
                pass
        finally:
            try:
                os.close(dirfd)
            except Exception:
                pass

    @staticmethod
    def _isWindows() -> bool:
        return os.name == "nt" or sys.platform.startswith("win")

    @staticmethod
    def _backupPath(path: Path, index: int) -> Path:
        return Path(str(path) + f".bak{index}")

    @staticmethod
    def _rotateBackups(path: Path, maxBackups: int) -> None:
        if maxBackups <= 0:
            return
        if not path.exists():
            return

        for i in range(maxBackups - 1, 0, -1):
            src = JSONStorage._backupPath(path, i)
            dst = JSONStorage._backupPath(path, i + 1)
            if src.exists():
                try:
                    os.replace(str(src), str(dst))
                except Exception:
                    try:
                        shutil.copy2(str(src), str(dst))
                        os.unlink(str(src))
                    except Exception:
                        pass

        bak1 = JSONStorage._backupPath(path, 1)
        try:
            shutil.copy2(str(path), str(bak1))
        except Exception:
            pass

    @staticmethod
    def _writeJsonAtomic(path: Path, obj, maxBackups: int) -> None:
        directory = str(path.parent) or "."
        os.makedirs(directory, exist_ok=True)

        JSONStorage._rotateBackups(path, maxBackups)

        data = json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")

        fd, tmpPath = tempfile.mkstemp(prefix="._tmp_", dir=directory)
        try:
            with os.fdopen(fd, "wb", closefd=True) as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())

            try:
                if path.exists():
                    os.chmod(str(path), stat.S_IWRITE | stat.S_IREAD)
            except Exception:
                pass

            if JSONStorage._isWindows():
                maxAttempts = 20
                delay = 0.01
                for attempt in range(1, maxAttempts + 1):
                    try:
                        shutil.copy(str(tmpPath), str(path))
                        break
                    except PermissionError:
                        if attempt == maxAttempts:
                            raise
                        time.sleep(delay)
                        delay = min(delay * 1.5, 0.2)
                    except OSError as e:
                        if e.errno in (errno.EACCES, 32):
                            if attempt == maxAttempts:
                                raise
                            time.sleep(delay)
                            delay = min(delay * 1.5, 0.2)
                        else:
                            raise
            else:
                os.replace(str(tmpPath), str(path))

            JSONStorage._fsyncDir(directory)
        finally:
            try:
                os.unlink(tmpPath)
            except FileNotFoundError:
                pass
            except Exception:
                pass

    @staticmethod
    def _readJsonSafe(path: Path):
        with path.open("rb") as f:
            raw = f.read()
        return json.loads(raw.decode("utf-8"))

    def _tryRecoverFromBackups(self):
        if self.maxBackups <= 0:
            return None

        for i in range(1, self.maxBackups + 1):
            bakPath = self._backupPath(self.basePath, i)
            if not bakPath.exists():
                continue
            try:
                data = self._readJsonSafe(bakPath)
                return data, bakPath
            except Exception:
                continue

        return None

    def load(self):
        with self._lock:
            data = {}
            try:
                if self.basePath.exists():
                    data = self._readJsonSafe(self.basePath)
                return data
            except json.JSONDecodeError as e:
                print(f"[WARN] Base JSON is corrupted: {e}. Trying recovery from backups...")
                recovered = self._tryRecoverFromBackups()
                if recovered is None:
                    print("[ERROR] Recovery failed: no valid backups found.")
                    traceback.print_exc()
                    return {}
                data, bakPath = recovered
                print(f"[WARN] Recovered from backup: {bakPath}")

                try:
                    # Restore recovered data into base WITHOUT rotating backups
                    self._writeJsonAtomic(self.basePath, data, 0)
                except Exception as saveErr:
                    print(f"[ERROR] Failed to restore recovered backup to base: {saveErr}")
                    traceback.print_exc()

                return data
            except Exception as e:
                print(f"[ERROR] Failed to load base: {e}")
                traceback.print_exc()
                return {}

    def save(self, newData):
        with self._lock:
            try:
                self._writeJsonAtomic(self.basePath, newData, self.maxBackups)
            except Exception as e:
                print(f"[ERROR] Failed to save base: {e}")
                traceback.print_exc()
