"""Async FF Logs v1 API Client."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx
from dotenv import load_dotenv
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .cache import RankingCache
from .models import CharacterAllRankings, RankingEntry, ZoneInfo, ZoneRanking

logger = logging.getLogger("fflogs_report")


class FFLogsError(Exception):
    """Base exception for FF Logs API errors."""
    pass


class AuthenticationError(FFLogsError):
    """Raised when the API key is missing, invalid, or unauthorized."""
    pass


class CharacterNotFoundError(FFLogsError):
    """Raised when the specified character, server, or region cannot be found."""
    pass


class CharacterHiddenError(FFLogsError):
    """Raised when the specified character has hidden their profile/rankings."""
    pass


class APIRequestError(FFLogsError):
    """Raised for unexpected API responses or server errors."""
    def __init__(self, message: str, status_code: Optional[int] = None, response_body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


def _is_retryable_exception(exc: BaseException) -> bool:
    """Determine whether an exception should trigger a retry."""
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        # Retry on HTTP 429 (Rate Limit) and 5xx Server Errors
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return False


class FFLogsClient:
    """Asynchronous client for FF Logs v1 API.
    
    Features:
    - Loads API key from FFLOGS_V1_APIKEY via python-dotenv.
    - Uses httpx.AsyncClient with connection pooling and timeouts.
    - Uses tenacity for exponential backoff retries on transient errors and 429 rate limits.
    - Limits concurrency via asyncio.Semaphore.
    - Enforces delays between requests to prevent API throttling.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://cn.fflogs.com/v1",
        concurrency_limit: int = 5,
        delay: float = 0.15,
        timeout: float = 25.0,
        env_file: Optional[str] = None,
        zones_file: Optional[str] = None,
        cache_dir: Optional[str] = None,
        use_cache: bool = True,
    ):
        """Initialize the FF Logs client.
        
        Args:
            api_key: FF Logs v1 API key. If omitted, read from FFLOGS_V1_APIKEY.
            base_url: FF Logs v1 API base URL (default: https://cn.fflogs.com/v1).
            concurrency_limit: Maximum concurrent asynchronous requests.
            delay: Delay in seconds before each request to avoid throttling.
            timeout: HTTP request timeout in seconds.
            env_file: Path to custom .env file if any.
            zones_file: Path to custom zones.json localized mapping file if any.
            cache_dir: Custom cache directory (default: <project root>/.cache).
            use_cache: Whether to read/write the on-disk query cache.
        """
        # Load environment variables
        load_dotenv(dotenv_path=env_file)
        
        resolved_key = api_key or os.getenv("FFLOGS_V1_APIKEY")
        if not resolved_key or not resolved_key.strip():
            raise AuthenticationError(
                "FF Logs API key not found. Please set FFLOGS_V1_APIKEY in your .env file "
                "or pass api_key to the client."
            )
        
        self.api_key = resolved_key.strip()
        self.base_url = base_url.rstrip("/")
        self.concurrency_limit = max(1, concurrency_limit)
        self.delay = max(0.0, delay)
        self.timeout = timeout
        self.zones_file = zones_file
        
        self._semaphore = asyncio.Semaphore(self.concurrency_limit)
        self._client: Optional[httpx.AsyncClient] = None
        self._owns_client = False
        
        # On-disk query cache (frozen zones are reused across runs)
        self._cache = RankingCache(cache_dir, enabled=use_cache)
        self.last_cache_stats: Dict[str, Any] = {}
        
        # Local in-memory caches
        self._cached_zones: Optional[List[ZoneInfo]] = None
        self._cached_classes: Optional[Dict[int, str]] = None

    async def __aenter__(self) -> FFLogsClient:
        await self._ensure_client()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            limits = httpx.Limits(
                max_connections=self.concurrency_limit * 3,
                max_keepalive_connections=self.concurrency_limit * 2,
            )
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                limits=limits,
            )
            self._owns_client = True
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client and not self._client.is_closed and self._owns_client:
            await self._client.aclose()
            self._client = None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=5.0),
        retry=retry_if_exception(_is_retryable_exception),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def _request_with_retry(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        """Execute HTTP GET request with tenacity retry and error handling."""
        client = await self._ensure_client()
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        
        merged_params = dict(params or {})
        merged_params["api_key"] = self.api_key
        
        response = await client.get(url, params=merged_params)
        
        # Raise HTTPStatusError on retryable 429 or 5xx codes so tenacity will retry
        if response.status_code in (429, 500, 502, 503, 504):
            response.raise_for_status()
            
        return response

    async def _safe_get(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Execute request under concurrency semaphore and rate-limiting delay."""
        async with self._semaphore:
            if self.delay > 0:
                await asyncio.sleep(self.delay)
            
            response = await self._request_with_retry(endpoint, params)
            
            # Check for API-level errors
            if response.status_code == 401:
                raise AuthenticationError(
                    f"Authentication failed: {response.text.strip()}"
                )
            
            try:
                data = response.json()
            except Exception:
                if response.status_code != 200:
                    raise APIRequestError(
                        f"HTTP {response.status_code}: {response.text}",
                        status_code=response.status_code,
                    )
                return None

            if isinstance(data, dict):
                if data.get("hidden") is True:
                    raise CharacterHiddenError("该角色在 FF Logs 设置了隐藏个人数据 (Profile Hidden)。")
                err_msg = data.get("error", "")
                if "error" in data:
                    if "Invalid character name/server/region" in err_msg:
                        raise CharacterNotFoundError(f"Character not found: {err_msg}")
                elif "Invalid raid zone" in err_msg:
                    # Some zones return 400 if deprecated or invalid
                    logger.debug(f"Invalid zone requested: {err_msg}")
                    return []
                elif response.status_code != 200:
                    raise APIRequestError(
                        f"API Error ({data.get('status', response.status_code)}): {err_msg}",
                        status_code=data.get("status", response.status_code),
                        response_body=data,
                    )

            return data

    @staticmethod
    def load_zones_mapping(custom_path: Optional[str] = None) -> Dict[int, str]:
        """Load localized zone names from zones.json.
        
        Searches in:
        1. custom_path (if provided)
        2. ./zones.json (current working directory)
        3. fflogs_report/zones.json (package directory)
        4. Parent folder of fflogs_report/ (project root directory)
        """
        candidate_paths: List[Path] = []
        if custom_path:
            candidate_paths.append(Path(custom_path))
        candidate_paths.extend([
            Path.cwd() / "zones.json",
            Path(__file__).resolve().parent / "zones.json",
            Path(__file__).resolve().parent.parent / "zones.json",
        ])
        
        for p in candidate_paths:
            if p.is_file():
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    if isinstance(raw, dict):
                        mapping: Dict[int, str] = {}
                        for k, v in raw.items():
                            try:
                                mapping[int(k)] = str(v)
                            except ValueError:
                                pass
                        return mapping
                except Exception as e:
                    logger.warning(f"Failed to read zones file {p}: {e}")
        return {}

    async def get_zones(self, force_refresh: bool = False) -> List[ZoneInfo]:
        """Fetch all raid, trial, and dungeon zones.
        
        Uses localized names from zones.json if present; otherwise falls back to
        the original name returned by the /zones API.
        
        Args:
            force_refresh: Whether to ignore cached zones.
            
        Returns:
            List of ZoneInfo objects.
        """
        if self._cached_zones is not None and not force_refresh:
            return self._cached_zones

        data = await self._safe_get("zones")
        if not isinstance(data, list):
            raise APIRequestError(f"Unexpected response format for zones: {data}")

        # Load localized zone mappings from zones.json if available
        zones_map = self.load_zones_mapping(self.zones_file)

        zones = []
        for z in data:
            z_id = z.get("id")
            # If zones.json has a localized name, use it; otherwise fall back to API name
            custom_name = zones_map.get(z_id)
            zones.append(ZoneInfo.from_dict(z, custom_name=custom_name))

        self._cached_zones = zones
        return zones

    async def get_classes(self, force_refresh: bool = False) -> Dict[int, str]:
        """Fetch all classes and specs (jobs) mapping.
        
        Returns:
            Dictionary mapping spec ID to job name (in Chinese if cn.fflogs.com).
        """
        if self._cached_classes is not None and not force_refresh:
            return self._cached_classes

        data = await self._safe_get("classes")
        specs_map: Dict[int, str] = {}
        if isinstance(data, list):
            for cls_info in data:
                for spec in cls_info.get("specs", []):
                    specs_map[spec["id"]] = spec["name"]

        self._cached_classes = specs_map
        return specs_map

    TIMEFRAME_MAP = {
        "current": "today",
        "history": "historical",
        "today": "today",
        "historical": "historical",
    }

    async def get_character_rankings(
        self,
        character_name: str,
        server_name: str,
        server_region: str = "CN",
        zone_id: Optional[int] = None,
        metric: str = "dps",
        timeframe: str = "current",
        bracket: Optional[int] = None,
        partition: Optional[int] = None,
    ) -> List[RankingEntry]:
        """Fetch rankings for a character in a specific zone (or latest zone if zone_id is None).
        
        Args:
            character_name: In-game character name.
            server_name: In-game server/world name.
            server_region: Region code ('CN', 'US', 'EU', 'KR', 'TW').
            zone_id: Zone ID. If None, queries the default/latest open raid zone.
            metric: 'dps', 'hps', 'bossdps', 'tankhps', 'playerspeed'.
            timeframe: 'current' (current rankings) or 'history' (historical rankings).
            bracket: Optional ranking bracket ID.
            partition: Optional partition ID.
            
        Returns:
            List of RankingEntry objects.
        """
        enc_name = urllib.parse.quote(character_name.strip())
        enc_server = urllib.parse.quote(server_name.strip())
        reg = server_region.strip().upper()
        
        endpoint = f"rankings/character/{enc_name}/{enc_server}/{reg}"
        api_timeframe = self.TIMEFRAME_MAP.get(str(timeframe).lower(), "today")
        
        params: Dict[str, Any] = {
            "metric": metric,
            "timeframe": api_timeframe,
        }
        if zone_id is not None:
            params["zone"] = zone_id
        if bracket is not None:
            params["bracket"] = bracket
        if partition is not None:
            params["partition"] = partition

        data = await self._safe_get(endpoint, params=params)
        if not isinstance(data, list):
            return []

        return [RankingEntry.from_dict(r) for r in data]

    async def get_all_character_rankings(
        self,
        character_name: str,
        server_name: str,
        server_region: str = "CN",
        metric: str = "dps",
        timeframe: str = "current",
        zone_ids: Optional[List[int]] = None,
        progress_callback: Optional[Callable[[int, int, ZoneInfo], None]] = None,
    ) -> CharacterAllRankings:
        """Fetch rankings across ALL raid/trial/dungeon zones for a character.

        Workflow:
        1. Load the on-disk cache for (region, server, character, metric, timeframe).
        2. On a cache miss only, probe character existence to fail fast.
        3. Reuse cached rankings for frozen zones; only query zones that are still
           active (plus any frozen zones missing from the cache).
        4. Assemble results and persist the updated cache.

        Args:
            character_name: Character name.
            server_name: Server/world name.
            server_region: Region code (CN, US, EU, KR, TW).
            metric: 'dps', 'hps', 'bossdps', 'tankhps', 'playerspeed'.
            timeframe: 'today' or 'historical'.
            zone_ids: Optional subset of zone IDs to query. If None, queries all available zones.
            progress_callback: Optional callback func(completed_count, total_count, zone_info).

        Returns:
            CharacterAllRankings summary object.
        """
        region = str(server_region).strip().upper()
        metric_key = str(metric).strip().lower()
        api_timeframe = self.TIMEFRAME_MAP.get(str(timeframe).strip().lower(), "today")

        # Step 1: Load cache for this character/query
        cached_zones: Optional[Dict[int, ZoneRanking]] = None
        if self._cache.enabled:
            cached_zones = self._cache.load_query(
                region, server_name, character_name, metric_key, api_timeframe
            )

        # Step 2: Probe character existence on a cache miss (fail fast, detect hidden profiles)
        if cached_zones is None:
            await self.get_character_rankings(
                character_name=character_name,
                server_name=server_name,
                server_region=region,
                zone_id=None,
                metric=metric_key,
                timeframe=api_timeframe,
            )

        # Step 3: Retrieve zones and split into "reuse cached" vs "must fetch"
        all_zones = await self.get_zones()
        if zone_ids is not None:
            zone_set = set(zone_ids)
            target_zones = [z for z in all_zones if z.id in zone_set]
        else:
            target_zones = list(all_zones)

        # Sort target zones descending by zone ID so recent zones are queried first
        target_zones = sorted(target_zones, key=lambda z: z.id, reverse=True)

        cached_zone_map = cached_zones or {}
        reused_frozen: Dict[int, ZoneRanking] = {}
        fetch_zones: List[ZoneInfo] = []
        for zone in target_zones:
            cached = cached_zone_map.get(zone.id)
            if zone.frozen and cached is not None:
                # 已封存副本的数据不会再变化，直接复用缓存，不再请求 API
                reused_frozen[zone.id] = cached
            else:
                fetch_zones.append(zone)

        total_zones = len(fetch_zones)
        completed_count = 0
        progress_lock = asyncio.Lock()

        # Step 4: Concurrently query the zones that need refreshing
        async def _query_single_zone(zone: ZoneInfo) -> Optional[ZoneRanking]:
            nonlocal completed_count
            success = True
            rankings: List[RankingEntry] = []
            try:
                rankings = await self.get_character_rankings(
                    character_name=character_name,
                    server_name=server_name,
                    server_region=region,
                    zone_id=zone.id,
                    metric=metric_key,
                    timeframe=api_timeframe,
                )
            except (AuthenticationError, CharacterNotFoundError, CharacterHiddenError):
                # 角色/权限类错误需要向上传递，不能当作空结果缓存
                raise
            except Exception as e:
                logger.warning(f"Failed to fetch zone {zone.id} ({zone.name}): {e}")
                success = False

            async with progress_lock:
                completed_count += 1
                if progress_callback:
                    try:
                        progress_callback(completed_count, total_zones, zone)
                    except Exception:
                        pass

            if not success:
                return None
            return ZoneRanking(
                zone_id=zone.id,
                zone_name=zone.name,
                frozen=zone.frozen,
                rankings=rankings,
            )

        if fetch_zones:
            zone_results = await asyncio.gather(*(_query_single_zone(z) for z in fetch_zones))
            fetched: Dict[int, ZoneRanking] = {
                zr.zone_id: zr for zr in zone_results if zr is not None
            }
        else:
            zone_results = []
            fetched = {}

        # Step 5: Assemble results (sorted by zone ID in descending order)
        result_map: Dict[int, ZoneRanking] = {}
        for zone in target_zones:
            if zone.id in reused_frozen:
                result_map[zone.id] = ZoneRanking(
                    zone_id=zone.id,
                    zone_name=zone.name,
                    frozen=zone.frozen,
                    rankings=reused_frozen[zone.id].rankings,
                )
            elif zone.id in fetched:
                result_map[zone.id] = fetched[zone.id]
            else:
                result_map[zone.id] = ZoneRanking(
                    zone_id=zone.id,
                    zone_name=zone.name,
                    frozen=zone.frozen,
                    rankings=[],
                )

        sorted_zone_results = sorted(result_map.values(), key=lambda zr: zr.zone_id, reverse=True)

        # Step 6: Persist cache (failed zones are not cached so frozen ones get retried)
        cache_path: Optional[Path] = None
        if self._cache.enabled:
            updated_zones = {
                zone_id: zone_ranking
                for zone_id, zone_ranking in result_map.items()
                if zone_id in fetched or zone_id in reused_frozen
            }
            if updated_zones:
                cache_path = self._cache.save_query(
                    region,
                    server_name,
                    character_name,
                    metric_key,
                    api_timeframe,
                    updated_zones,
                    replace=zone_ids is None,
                )

        self.last_cache_stats = {
            "cache_enabled": self._cache.enabled,
            "cache_hit": cached_zones is not None,
            "reused_frozen": len(reused_frozen),
            "fetched_zones": len(fetched),
            "cache_saved": cache_path is not None,
            "cache_path": str(cache_path) if cache_path else None,
        }

        return CharacterAllRankings(
            character_name=character_name,
            server_name=server_name,
            server_region=region,
            metric=metric_key,
            timeframe=timeframe,
            zone_rankings=sorted_zone_results,
        )
