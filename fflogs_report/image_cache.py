"""Disk cache for rendered FF Logs report images.

Rendering a report is the slowest part of the pipeline, so rendered images are
cached on disk and reused until they expire (TTL) or the cache grows past its
size budget. An entry is a plain image file named after a deterministic hash of
the render inputs::

    {cache_dir}/{key}.jpg|.jpeg|.png

The module is dependency free (standard library only) and thread safe: every
mutating operation is serialised by a single lock.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# A cache key is a lower-case hexadecimal SHA-256 prefix.
_KEY_PATTERN = re.compile(r"^[0-9a-f]{16,64}$")
# Cache entries are image files named ``<key>.<suffix>``.
_CACHE_FILE_PATTERN = re.compile(r"^[0-9a-f]{16,64}\.(?:jpg|jpeg|png)$")


class ImageCache:
    """TTL and size bounded on-disk cache for rendered report images.

    Attributes:
        CACHE_VERSION: Bumped when the key layout or the rendered report output
            changes. It participates in every generated key so entries from an
            older layout/report are never reused.
        SUFFIXES: Image file suffixes kept when storing a rendered image.
    """

    CACHE_VERSION = 5
    SUFFIXES = (".jpg", ".jpeg", ".png")

    def __init__(
        self,
        cache_dir: str | Path,
        ttl_seconds: int,
        max_bytes: int,
    ) -> None:
        """Bind a cache to a directory and its limits.

        The directory is not created here; it is created lazily by the first
        write or cleanup.

        Args:
            cache_dir: Directory that holds the cached image files.
            ttl_seconds: Maximum age of an entry in seconds. Values ``<= 0``
                mean entries never expire by age.
            max_bytes: Maximum total size of the cache in bytes. Values
                ``<= 0`` disable size based eviction.
        """
        self.cache_dir = Path(cache_dir)
        self.ttl_seconds = int(ttl_seconds)
        self.max_bytes = int(max_bytes)
        self._lock = threading.Lock()

    def build_key(self, parts: Mapping[str, Any]) -> str:
        """Build a deterministic cache key from the render inputs.

        The key is the 32 character prefix of a SHA-256 hex digest over the
        canonical JSON encoding of ``parts``, so equal mappings always produce
        the same key regardless of their insertion order.

        Args:
            parts: Render inputs that identify the cached image.

        Returns:
            A 32 character lower-case hexadecimal key.
        """
        payload = json.dumps(
            {"v": self.CACHE_VERSION, **dict(parts)},
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
        # sha256 always yields 64 hex characters; pad defensively so the key
        # length is part of the contract even if the digest changes.
        return hashlib.sha256(payload).hexdigest()[:32].ljust(32, "0")

    def get(self, key: str) -> Path | None:
        """Return the cached image for a key when it exists and is fresh.

        Expired entries discovered during the lookup are removed.

        Args:
            key: Cache key produced by :meth:`build_key`.

        Returns:
            The cached image path, or ``None`` when the entry is missing,
            expired, or the cache directory does not exist yet.

        Raises:
            ValueError: If the key is not a valid cache key.
        """
        self._validate_key(key)
        with self._lock:
            if not self.cache_dir.is_dir():
                return None
            now = time.time()
            for suffix in self.SUFFIXES:
                path = self.cache_dir / f"{key}{suffix}"
                try:
                    if not path.is_file():
                        continue
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if self.ttl_seconds <= 0 or now - mtime <= self.ttl_seconds:
                    return path
                try:
                    path.unlink()
                except OSError:
                    pass
                return None
            return None

    def put(self, key: str, source: str | Path) -> Path:
        """Store a rendered image under a cache key.

        The source file is moved into the cache when possible; if the rename
        fails because the source lives on another device, its bytes are copied
        instead and the source is removed on a best-effort basis. The entry is
        written through a ``.tmp`` file plus :func:`os.replace` so a partially
        written image is never visible to readers.

        Args:
            key: Cache key produced by :meth:`build_key`.
            source: Existing image file to store.

        Returns:
            Path of the stored cache entry.

        Raises:
            ValueError: If the key is not a valid cache key.
            FileNotFoundError: If ``source`` is not an existing file.
        """
        self._validate_key(key)
        source_path = Path(source)
        with self._lock:
            if not source_path.is_file():
                raise FileNotFoundError(f"Image source does not exist: {source_path}")
            suffix = source_path.suffix.lower()
            if suffix not in self.SUFFIXES:
                suffix = ".jpg"
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            dest = self.cache_dir / f"{key}{suffix}"
            temp_path = dest.with_name(dest.name + ".tmp")

            moved = True
            try:
                # Same-device rename: fast and keeps the source inode.
                os.replace(source_path, temp_path)
            except OSError:
                # Cross-device move (or another rename failure): copy the bytes.
                moved = False
                try:
                    shutil.copyfile(source_path, temp_path)
                except OSError:
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass
                    raise
            try:
                os.replace(temp_path, dest)
            except OSError:
                # Never leave a half-written entry behind.
                try:
                    temp_path.unlink()
                except OSError:
                    pass
                raise
            if not moved:
                try:
                    source_path.unlink()
                except OSError:
                    pass
            # Protect the entry just written: it is always the newest one.
            self._enforce_size_limit(protected=dest)
            return dest

    def cleanup(self) -> dict[str, int]:
        """Remove expired entries and evict old entries down to the size budget.

        Expired entries are deleted first, then the oldest entries by
        modification time are evicted while the cache exceeds ``max_bytes``. A
        file that cannot be deleted is skipped so a single failure never aborts
        the cleanup.

        Returns:
            Cleanup counters::

                {
                    "deleted_expired": int,  # removed because of the TTL
                    "deleted_size": int,     # removed to satisfy max_bytes
                    "remaining_count": int,  # entries left in the cache
                    "remaining_bytes": int,  # total size of remaining entries
                }
        """
        with self._lock:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            deleted_expired = 0
            if self.ttl_seconds > 0:
                now = time.time()
                for path, mtime, _size in self._scan_entries():
                    if now - mtime <= self.ttl_seconds:
                        continue
                    try:
                        path.unlink()
                    except OSError:
                        continue
                    deleted_expired += 1
            deleted_size = self._enforce_size_limit()
            entries = self._scan_entries()
            return {
                "deleted_expired": deleted_expired,
                "deleted_size": deleted_size,
                "remaining_count": len(entries),
                "remaining_bytes": sum(size for _path, _mtime, size in entries),
            }

    def stats(self) -> dict[str, int]:
        """Summarise the current cache contents.

        Non-image files, including in-flight ``*.tmp`` files, are ignored.

        Returns:
            A mapping with the number of cache files (``"count"``) and their
            total size in bytes (``"bytes"``).
        """
        with self._lock:
            entries = self._scan_entries()
            return {
                "count": len(entries),
                "bytes": sum(size for _path, _mtime, size in entries),
            }

    def _validate_key(self, key: str) -> None:
        """Validate a cache key.

        Args:
            key: Key to validate.

        Raises:
            ValueError: If the key is not a lower-case hexadecimal SHA-256
                prefix of 16 to 64 characters.
        """
        if not isinstance(key, str) or not _KEY_PATTERN.fullmatch(key):
            raise ValueError(f"Invalid image cache key: {key!r}")

    def _scan_entries(self) -> list[tuple[Path, float, int]]:
        """List the cache files ordered from oldest to newest.

        Non-image files, including in-flight ``*.tmp`` files, are ignored.

        Returns:
            ``(path, mtime, size)`` tuples sorted by modification time.
            Callers must already hold ``self._lock``.
        """
        if not self.cache_dir.is_dir():
            return []
        try:
            children = list(self.cache_dir.iterdir())
        except OSError:
            return []
        entries: list[tuple[Path, float, int]] = []
        for path in children:
            if not _CACHE_FILE_PATTERN.fullmatch(path.name):
                continue
            try:
                if not path.is_file():
                    continue
                stat_result = path.stat()
            except OSError:
                continue
            entries.append((path, stat_result.st_mtime, stat_result.st_size))
        entries.sort(key=lambda entry: entry[1])
        return entries

    def _enforce_size_limit(self, protected: Path | None = None) -> int:
        """Evict the oldest cache files until the size budget is respected.

        Args:
            protected: Optional cache entry that is never evicted, typically
                the entry just written by :meth:`put`.

        Returns:
            Number of files evicted. Callers must already hold ``self._lock``.
        """
        if self.max_bytes <= 0:
            return 0
        entries = self._scan_entries()
        total_bytes = sum(size for _path, _mtime, size in entries)
        evicted = 0
        for path, _mtime, size in entries:
            if total_bytes <= self.max_bytes:
                break
            if protected is not None and path == protected:
                continue
            try:
                path.unlink()
            except OSError:
                continue
            evicted += 1
            total_bytes -= size
        return evicted
