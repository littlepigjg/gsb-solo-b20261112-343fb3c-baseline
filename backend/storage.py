"""JSON repository with serialized read/modify/write and atomic replacement.

The local server owns one process. All HTTP and worker threads share this
repository. A backup is retained before each successful replacement. Invalid
JSON is never replaced silently: a valid backup is recovered or startup fails.

Commit semantics
----------------
Every mutation (insert/update/delete/prune) is staged on a deep copy and only
swapped into the in-memory cache after the on-disk atomic replace succeeds.
If writing the temporary file or ``os.replace`` fails, the caller receives a
:class:`~backend.errors.StorageFailure`, the cache keeps serving the last
committed state, no later successful write can carry the rejected change, and
a fresh process reads exactly that committed state. The backup always points
at the last successfully committed file, so a corrupt main file is recovered
from it on the next read; without a valid backup the original failure is
raised instead of fabricating empty data.
"""
import copy
import json
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from .errors import MissingRecord, Conflict, StorageFailure


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id():
    return uuid.uuid4().hex


class JsonStore:
    COLLECTIONS = {"assets", "jobs", "presets", "events", "exports"}

    def __init__(self, directory):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        for collection in self.COLLECTIONS:
            with self.lock:
                if not self._path(collection).exists():
                    self._commit(collection, {"schema_version": 1, "records": {}})
                else:
                    self._load(collection)

    def _path(self, collection):
        if collection not in self.COLLECTIONS:
            raise ValueError("Unknown collection")
        return self.directory / f"{collection}.json"

    def _backup_path(self, collection):
        return self._path(collection).with_suffix(".json.bak")

    def _decode(self, path):
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        if data.get("schema_version") != 1 or not isinstance(data.get("records"), dict):
            raise ValueError("Unsupported JSON schema")
        return data

    def _recover_from_backup(self, collection, path):
        """Replace a corrupt main file with its validated backup.

        Returns the decoded backup contents. Raises the original corruption
        error when no valid backup exists, so callers never see fabricated
        empty data.
        """
        backup = self._backup_path(collection)
        if not backup.exists():
            raise RuntimeError(f"JSON 数据损坏且没有可用备份：{path}")
        data = self._decode(backup)
        recovery = path.with_suffix(f".{new_id()}.recovery")
        try:
            shutil.copy2(backup, recovery)
            os.replace(recovery, path)
        except OSError as error:
            recovery.unlink(missing_ok=True)
            raise StorageFailure(f"备份恢复失败：{path}") from error
        return data

    def _load(self, collection):
        """Decode the main file, recovering a valid backup when it is corrupt."""
        path = self._path(collection)
        try:
            return self._decode(path)
        except (ValueError, OSError):
            return self._recover_from_backup(collection, path)

    def _read(self, collection):
        cache = self.__dict__.setdefault("_decoded", {})
        if collection in cache:
            return cache[collection]
        data = self._load(collection)
        cache[collection] = data
        return data

    def _commit(self, collection, data):
        """Durably persist ``data`` and publish it to the cache.

        The cache is only updated after ``os.replace`` succeeds. Any failure
        leaves both disk (main file and backup) and cache at the last
        committed state and removes every temporary file. The previous main
        file is rotated into the backup via a temporary backup file, so a
        failed commit can never clobber the last known-good backup.
        """
        cache = self.__dict__.setdefault("_decoded", {})
        path = self._path(collection)
        backup = self._backup_path(collection)
        temporary = path.with_suffix(f".{new_id()}.tmp")
        backup_temporary = backup.with_suffix(f".{new_id()}.bak.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if path.exists():
                shutil.copy2(path, backup_temporary)
                os.replace(backup_temporary, backup)
            os.replace(temporary, path)
        except (OSError, ValueError) as error:
            temporary.unlink(missing_ok=True)
            backup_temporary.unlink(missing_ok=True)
            raise StorageFailure(f"JSON 写入失败，修改未保存：{path}") from error
        else:
            temporary.unlink(missing_ok=True)
            backup_temporary.unlink(missing_ok=True)
            cache[collection] = data
            return data

    def all(self, collection):
        with self.lock:
            data = self._read(collection)
            return copy.deepcopy(list(data["records"].values()))

    def get(self, collection, record_id):
        with self.lock:
            data = self._read(collection)
            record = data["records"].get(record_id)
            if record is None:
                raise MissingRecord(collection, record_id)
            return copy.deepcopy(record)

    def insert(self, collection, record):
        with self.lock:
            committed = self._read(collection)
            record = copy.deepcopy(record)
            record.setdefault("id", new_id())
            record.setdefault("created_at", utc_now())
            record["updated_at"] = utc_now()
            if record["id"] in committed["records"]:
                raise Conflict("记录已存在")
            staged = copy.deepcopy(committed)
            staged["records"][record["id"]] = record
            self._commit(collection, staged)
            return copy.deepcopy(record)

    def update(self, collection, record_id, changes):
        with self.lock:
            committed = self._read(collection)
            if record_id not in committed["records"]:
                raise MissingRecord(collection, record_id)
            changes = copy.deepcopy(changes)
            changes.pop("id", None)
            changes.pop("created_at", None)
            staged = copy.deepcopy(committed)
            record = staged["records"][record_id]
            record.update(changes)
            record["updated_at"] = utc_now()
            self._commit(collection, staged)
            return copy.deepcopy(record)

    def delete(self, collection, record_id):
        with self.lock:
            committed = self._read(collection)
            if record_id not in committed["records"]:
                raise MissingRecord(collection, record_id)
            staged = copy.deepcopy(committed)
            removed = staged["records"].pop(record_id)
            self._commit(collection, staged)
            return copy.deepcopy(removed)

    def prune(self, collection, maximum):
        with self.lock:
            committed = self._read(collection)
            records = sorted(
                committed["records"].values(),
                key=lambda item: item["created_at"],
                reverse=True,
            )
            if len(records) > maximum:
                staged = copy.deepcopy(committed)
                staged["records"] = {item["id"]: item for item in records[:maximum]}
                self._commit(collection, staged)

    def event(self, action, message, details=None):
        self.insert("events", {
            "action": action,
            "message": message,
            "details": details or {},
        })
        self.prune("events", 500)

    def snapshot(self):
        with self.lock:
            return {
                "schema_version": 1,
                "created_at": utc_now(),
                "collections": {
                    name: self._read(name)
                    for name in sorted(self.COLLECTIONS)
                },
            }
