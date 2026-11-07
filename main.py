"""AstrBot plugin: query FF Logs rankings and render a mobile-friendly image report."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .fflogs_report.client import (
    AuthenticationError,
    CharacterHiddenError,
    CharacterNotFoundError,
    FFLogsClient,
    FFLogsError,
)
from .fflogs_report.formatter import format_html
from .fflogs_report.image_cache import ImageCache

PLUGIN_NAME = "astrbot_plugin_fflogs_report"

DEFAULT_REGION = "CN"
REGIONS = ("CN", "US", "EU", "KR", "TW")
DEFAULT_BASE_URL = "https://cn.fflogs.com/v1"
DEFAULT_METRIC = "dps"
METRICS = ("dps", "hps", "bossdps", "tankhps", "playerspeed")
DEFAULT_TIMEFRAME = "current"
TIMEFRAMES = ("current", "history")

# Mobile-oriented defaults: a 430 CSS px viewport rendered at 1.8x device pixel
# ratio yields a ~774 px wide, full-page image that is comfortable to read on
# phones while staying sharp in chat apps.
DEFAULT_RENDER_WIDTH = 430
DEFAULT_RENDER_SCALE = "ultra"
RENDER_SCALES = ("normal", "high", "ultra")
DEFAULT_IMAGE_FORMAT = "jpeg"
IMAGE_FORMATS = ("jpeg", "png")
DEFAULT_IMAGE_QUALITY = 90

DEFAULT_IMAGE_CACHE_TTL_MINUTES = 60
DEFAULT_IMAGE_CACHE_MAX_MB = 128
DEFAULT_CLEANUP_INTERVAL_MINUTES = 30

USAGE = "用法: /fflogs <服务器名> <角色名>\n例如: /fflogs 拂晓之间 丝瓜卡夫卡"


class FFLogsReportPlugin(Star):
    """查询 FF14 玩家全副本 FF Logs 排名，并生成适合移动端阅读的图片报告。

    指令:
        /fflogs <服务器名> <角色名>

    查询结果会先缓存 FF Logs 数据，再渲染为 html-simple 报告图片；
    同一角色在缓存有效期（默认 1 小时）内重复查询会直接返回缓存图片。
    """

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config: AstrBotConfig | dict[str, Any] = (
            config if config is not None else {}
        )

        plugin_data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self.data_dir = plugin_data_dir
        self.logs_cache_dir = plugin_data_dir / "logs_cache"
        self.image_cache = ImageCache(
            cache_dir=plugin_data_dir / "image_cache",
            ttl_seconds=self._config_int(
                "image_cache_ttl_minutes",
                DEFAULT_IMAGE_CACHE_TTL_MINUTES,
                minimum=1,
            )
            * 60,
            max_bytes=self._config_int(
                "image_cache_max_mb",
                DEFAULT_IMAGE_CACHE_MAX_MB,
                minimum=1,
            )
            * 1024
            * 1024,
        )
        self._cleanup_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ lifecycle

    async def initialize(self) -> None:
        """Start the periodic image-cache cleanup task."""
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("FF Logs report plugin initialized, data dir: %s", self.data_dir)

    async def terminate(self) -> None:
        """Stop the periodic cleanup task."""
        task, self._cleanup_task = self._cleanup_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _cleanup_loop(self) -> None:
        """Periodically expire old images and enforce the cache size limit."""
        interval_seconds = (
            self._config_int(
                "image_cache_cleanup_interval_minutes",
                DEFAULT_CLEANUP_INTERVAL_MINUTES,
                minimum=1,
            )
            * 60
        )
        while True:
            try:
                stats = await asyncio.to_thread(self.image_cache.cleanup)
                if stats["deleted_expired"] or stats["deleted_size"]:
                    logger.info(
                        "FF Logs report image cache cleanup: %s (remaining %s bytes)",
                        stats,
                        stats["remaining_bytes"],
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Failed to clean the FF Logs report image cache")
            await asyncio.sleep(interval_seconds)

    # ------------------------------------------------------------------ config helpers

    def _config_int(
        self, key: str, default: int, minimum: int, maximum: int | None = None
    ) -> int:
        """Read an integer config value, falling back to the default on invalid input."""
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def _config_choice(self, key: str, default: str, choices: tuple[str, ...]) -> str:
        """Read a lower-cased config value and validate it against the allowed choices."""
        value = str(self.config.get(key, default) or default).strip().lower()
        return value if value in choices else default

    def _region(self) -> str:
        value = (
            str(self.config.get("region", DEFAULT_REGION) or DEFAULT_REGION)
            .strip()
            .upper()
        )
        return value if value in REGIONS else DEFAULT_REGION

    def _base_url(self) -> str:
        value = str(
            self.config.get("base_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL
        ).strip()
        value = value.rstrip("/")
        if not value.endswith("/v1"):
            value += "/v1"
        return value

    def _render_options(self) -> dict[str, Any]:
        """Build the html_render options: full page, mobile viewport, crisp scaling."""
        image_format = self._config_choice(
            "image_format", DEFAULT_IMAGE_FORMAT, IMAGE_FORMATS
        )
        options: dict[str, Any] = {
            "full_page": True,
            "type": image_format,
            "viewport_width": self._config_int(
                "render_width", DEFAULT_RENDER_WIDTH, minimum=200, maximum=2000
            ),
            "device_scale_factor_level": self._config_choice(
                "render_scale", DEFAULT_RENDER_SCALE, RENDER_SCALES
            ),
        }
        if image_format == "jpeg":
            options["quality"] = self._config_int(
                "image_quality", DEFAULT_IMAGE_QUALITY, minimum=1, maximum=100
            )
        return options

    def _image_cache_key(
        self, server_name: str, character_name: str, render_options: dict[str, Any]
    ) -> str:
        """Build the image cache key, covering everything that affects the report."""
        return self.image_cache.build_key(
            {
                "region": self._region(),
                "base_url": self._base_url(),
                "server_name": server_name,
                "character_name": character_name,
                "metric": self._config_choice("metric", DEFAULT_METRIC, METRICS),
                "timeframe": self._config_choice(
                    "timeframe", DEFAULT_TIMEFRAME, TIMEFRAMES
                ),
                "render_options": render_options,
            }
        )

    def _build_client(self) -> FFLogsClient:
        """Create an FF Logs client whose query cache lives in the plugin data dir."""
        api_key = str(self.config.get("api_key", "") or "").strip() or None
        return FFLogsClient(
            api_key=api_key,
            base_url=self._base_url(),
            cache_dir=str(self.logs_cache_dir),
            use_cache=True,
        )

    # ------------------------------------------------------------------ command

    @filter.command("fflogs")
    async def fflogs(
        self, event: AstrMessageEvent, server_name: str = "", character_name: str = ""
    ):
        """查询 FF14 玩家全副本 FF Logs 排名，并生成适合移动端阅读的图片报告。

        用法: /fflogs <服务器名> <角色名>
        """
        server_name = str(server_name or "").strip()
        character_name = str(character_name or "").strip()
        if not server_name or not character_name:
            yield event.plain_result(USAGE)
            return

        cache_key = self._image_cache_key(
            server_name, character_name, self._render_options()
        )
        cached_path = await asyncio.to_thread(self.image_cache.get, cache_key)
        if cached_path is not None:
            logger.debug(
                "FF Logs image cache hit for %s @ %s", character_name, server_name
            )
            yield event.image_result(str(cached_path))
            return

        yield event.plain_result(
            f"正在查询 [{server_name}] {character_name} 的 FF Logs 数据，请稍候…"
        )

        region = self._region()
        metric = self._config_choice("metric", DEFAULT_METRIC, METRICS)
        timeframe = self._config_choice("timeframe", DEFAULT_TIMEFRAME, TIMEFRAMES)
        try:
            async with self._build_client() as client:
                results = await client.get_all_character_rankings(
                    character_name=character_name,
                    server_name=server_name,
                    server_region=region,
                    metric=metric,
                    timeframe=timeframe,
                )
        except AuthenticationError:
            yield event.plain_result(
                "未配置 FF Logs API Key。请在插件配置中填写 api_key，"
                "或设置环境变量 FFLOGS_V1_APIKEY。"
            )
            return
        except CharacterNotFoundError:
            yield event.plain_result(
                f"未找到角色 [{server_name}] {character_name}，请检查服务器名与角色名是否准确。"
            )
            return
        except CharacterHiddenError:
            yield event.plain_result(
                f"角色 [{server_name}] {character_name} 在 FF Logs 上隐藏了个人数据，无法查询。"
            )
            return
        except FFLogsError as exc:
            logger.warning(
                "FF Logs API error for %s @ %s: %s", character_name, server_name, exc
            )
            yield event.plain_result(f"查询 FF Logs 失败：{exc}")
            return
        except Exception:
            logger.exception(
                "Unexpected error while querying FF Logs for %s @ %s",
                character_name,
                server_name,
            )
            yield event.plain_result("查询 FF Logs 数据失败，请稍后重试。")
            return

        results = results.with_filtering(show_all=False)
        if results.total_rankings == 0:
            yield event.plain_result(
                f"未查询到 [{server_name}] {character_name} 在 {results.total_zones_queried} 个副本中的排名记录。"
            )
            return

        render_options = self._render_options()
        try:
            rendered_path = await self.html_render(
                format_html(results, simple=True),
                {},
                return_url=False,
                options=render_options,
                umo=event.unified_msg_origin,
            )
        except Exception:
            logger.exception(
                "Failed to render the FF Logs report image for %s @ %s",
                character_name,
                server_name,
            )
            yield event.plain_result(
                "报告图片渲染失败，请稍后重试，或检查 AstrBot 的文转图服务配置。"
            )
            return

        # html_render always downloads the result with a ".jpg" temp name; give the
        # cached file the suffix of the format we actually requested.
        rendered = Path(rendered_path)
        expected_suffix = ".png" if render_options["type"] == "png" else ".jpg"
        if rendered.suffix.lower() != expected_suffix:
            renamed = rendered.with_suffix(expected_suffix)
            try:
                rendered.replace(renamed)
                rendered = renamed
            except OSError:
                pass

        try:
            cached_path = await asyncio.to_thread(
                self.image_cache.put, cache_key, rendered
            )
        except Exception:
            # Sending the freshly rendered image is still better than failing the request.
            logger.exception(
                "Failed to store the rendered image in the FF Logs image cache"
            )
            cached_path = rendered

        yield event.image_result(str(cached_path))
