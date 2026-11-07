"""Persistent query cache for FF Logs rankings.

缓存按 ``region + server + character`` 分文件保存，文件内再按
``metric + timeframe`` 区分不同查询。已封存 (frozen) 副本的数据一旦成功
缓存，后续查询直接复用，只重新获取进行中的副本，从而大幅减少 API 请求。

目录结构::

    .cache/rankings/{region}/{server}/{character}.json

默认目录可用环境变量 ``FFLOGS_CACHE_DIR`` 覆盖；当本项目作为库安装到
site-packages / dist-packages 时，默认回退到
``$XDG_CACHE_HOME/fflogs-report``（或 ``~/.cache/fflogs-report``），
避免把缓存写进解释器目录。
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Union

from .models import RankingEntry, ZoneRanking

logger = logging.getLogger("fflogs_report")

# 包目录与源码项目根目录
PACKAGE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = PACKAGE_DIR.parent
_INSTALLED_CACHE_NAME = "fflogs-report"


def _default_cache_dir(package_dir: Optional[Path] = None) -> Path:
    """Resolve the default cache directory.

    Priority:
    1. ``FFLOGS_CACHE_DIR`` environment variable (``~`` is expanded).
    2. ``<项目根目录>/.cache`` for a source checkout or editable install,
       keeping the existing per-project cache behaviour.
    3. ``$XDG_CACHE_HOME/fflogs-report`` (fallback ``~/.cache/fflogs-report``)
       when the package is installed into ``site-packages`` / ``dist-packages``,
       so a library consumer never writes caches into the interpreter directory.
    """
    override = os.getenv("FFLOGS_CACHE_DIR")
    if override and override.strip():
        return Path(override).expanduser()

    pkg = Path(package_dir or PACKAGE_DIR).resolve()
    project_root = pkg.parent
    if project_root.name in {"site-packages", "dist-packages"}:
        cache_home = os.getenv("XDG_CACHE_HOME")
        base = Path(cache_home).expanduser() if cache_home else Path.home() / ".cache"
        return base / _INSTALLED_CACHE_NAME
    return project_root / ".cache"


# 默认缓存目录：源码运行/项目依赖时为项目根目录下的 .cache；
# 作为库安装到 site-packages 时回退到用户缓存目录（可用 FFLOGS_CACHE_DIR 覆盖）
DEFAULT_CACHE_DIR = _default_cache_dir()

_UNSAFE_PATH_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _safe_component(value: str) -> str:
    """Make a value safe to use as a single path component."""
    cleaned = _UNSAFE_PATH_CHARS.sub("_", str(value or "").strip())
    cleaned = cleaned.strip(" .")
    return cleaned or "_"


class RankingCache:
    """JSON file based cache for character rankings."""

    VERSION = 1

    def __init__(
        self,
        cache_dir: Optional[Union[os.PathLike, str]] = None,
        enabled: bool = True,
    ):
        self.root = Path(cache_dir).expanduser() if cache_dir else DEFAULT_CACHE_DIR
        self.enabled = enabled

    def path_for(self, server_region: str, server_name: str, character_name: str) -> Path:
        """Return the cache file path for a character (region + server + name)."""
        return (
            self.root
            / "rankings"
            / _safe_component(str(server_region).upper())
            / _safe_component(server_name)
            / f"{_safe_component(character_name)}.json"
        )

    @staticmethod
    def query_key(metric: str, timeframe: str) -> str:
        """Cache key inside a character file for one metric/timeframe combination."""
        return f"{str(metric).strip().lower()}|{str(timeframe).strip().lower()}"

    def _load_file(self, server_region: str, server_name: str, character_name: str) -> Dict[str, Any]:
        path = self.path_for(server_region, server_name, character_name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {"version": self.VERSION, "queries": {}}
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"缓存文件读取失败，将忽略缓存 {path}: {e}")
            return {"version": self.VERSION, "queries": {}}

        if not isinstance(data, dict):
            return {"version": self.VERSION, "queries": {}}
        if not isinstance(data.get("queries"), dict):
            data["queries"] = {}
        return data

    def load_query(
        self,
        server_region: str,
        server_name: str,
        character_name: str,
        metric: str,
        timeframe: str,
    ) -> Optional[Dict[int, ZoneRanking]]:
        """Load cached zone rankings for one query.

        Returns ``None`` when this metric/timeframe has never been cached for the
        character, otherwise a ``{zone_id: ZoneRanking}`` mapping.
        """
        if not self.enabled:
            return None

        data = self._load_file(server_region, server_name, character_name)
        entry = data["queries"].get(self.query_key(metric, timeframe))
        if not isinstance(entry, dict):
            return None
        raw_zones = entry.get("zones")
        if not isinstance(raw_zones, dict):
            return None

        zones: Dict[int, ZoneRanking] = {}
        for raw in raw_zones.values():
            if not isinstance(raw, dict):
                continue
            try:
                zone_id = int(raw["zone_id"])
            except (KeyError, TypeError, ValueError):
                continue
            rankings = [
                RankingEntry.from_dict(r)
                for r in raw.get("rankings", [])
                if isinstance(r, dict)
            ]
            zones[zone_id] = ZoneRanking(
                zone_id=zone_id,
                zone_name=str(raw.get("zone_name", "")),
                frozen=bool(raw.get("frozen", False)),
                rankings=rankings,
            )
        return zones

    @staticmethod
    def _serialize_zone(zone: ZoneRanking) -> Dict[str, Any]:
        return {
            "zone_id": zone.zone_id,
            "zone_name": zone.zone_name,
            "frozen": zone.frozen,
            "rankings": [r.to_dict() for r in zone.rankings],
        }

    def save_query(
        self,
        server_region: str,
        server_name: str,
        character_name: str,
        metric: str,
        timeframe: str,
        zones: Dict[int, ZoneRanking],
        replace: bool = False,
    ) -> Optional[Path]:
        """Persist zone rankings for one query.

        Args:
            zones: Zone rankings to store. Zone IDs in this mapping are written,
                other previously cached zones are kept unless ``replace`` is True.
            replace: Drop zone entries that are not in ``zones`` (used for full queries).

        Returns:
            The cache file path on success, ``None`` when caching is disabled or
            writing failed.
        """
        if not self.enabled or not zones:
            return None

        path = self.path_for(server_region, server_name, character_name)
        data = self._load_file(server_region, server_name, character_name)
        queries = data.setdefault("queries", {})

        key = self.query_key(metric, timeframe)
        existing = queries.get(key)
        existing_zones: Dict[str, Any] = {}
        if isinstance(existing, dict) and isinstance(existing.get("zones"), dict):
            existing_zones = dict(existing["zones"])

        if replace:
            merged_zones: Dict[str, Any] = {}
        else:
            merged_zones = existing_zones
        for zone_id, zone in zones.items():
            merged_zones[str(zone_id)] = self._serialize_zone(zone)

        queries[key] = {
            "metric": str(metric).strip().lower(),
            "timeframe": str(timeframe).strip().lower(),
            "updated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "zones": merged_zones,
        }
        data["version"] = self.VERSION
        data["character_name"] = character_name
        data["server_name"] = server_name
        data["server_region"] = str(server_region).upper()

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = path.with_name(path.name + ".tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, path)
        except OSError as e:
            logger.warning(f"缓存写入失败 {path}: {e}")
            return None
        return path
