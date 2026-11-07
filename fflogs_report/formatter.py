"""HTML formatters for displaying FF Logs rankings.

Only the standalone HTML report is supported: ``format_html(..., simple=False)``
renders the interactive report and ``format_html(..., simple=True)`` renders the
static "html-simple" variant from the same Jinja2 template.
"""

from __future__ import annotations

import base64
import datetime
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import jinja2

from .models import CharacterAllRankings


_TEMPLATES_DIR = Path(__file__).parent / "templates"
_JOBS_ASSETS_DIR = Path(__file__).parent / "assets" / "jobs"
_jinja_env: Optional[jinja2.Environment] = None

# 职业/特职名称（国服中文、国际服英文、日服日文）到图标文件名的映射。
# 图标文件位于 fflogs_report/assets/jobs/{slug}.png，来源:
# https://github.com/xivapi/classjob-icons （FFXIV 素材版权归 SQUARE ENIX 所有）
JOB_ICON_SLUGS: Dict[str, str] = {
    # 防护职业 / Tank
    "骑士": "paladin", "paladin": "paladin", "ナイト": "paladin",
    "战士": "warrior", "warrior": "warrior", "戦士": "warrior",
    "暗黑骑士": "darkknight", "dark knight": "darkknight", "darkknight": "darkknight", "暗黒騎士": "darkknight",
    "绝枪战士": "gunbreaker", "gunbreaker": "gunbreaker", "ガンブレイカー": "gunbreaker",
    # 近战物理 / Melee DPS
    "武僧": "monk", "monk": "monk", "モンク": "monk",
    "龙骑士": "dragoon", "dragoon": "dragoon", "竜騎士": "dragoon",
    "忍者": "ninja", "ninja": "ninja",
    "武士": "samurai", "samurai": "samurai", "侍": "samurai",
    "钐镰客": "reaper", "reaper": "reaper", "リーパー": "reaper",
    "蝰蛇剑士": "viper", "viper": "viper", "ヴァイパー": "viper",
    # 远程物理 / Physical Ranged DPS
    "吟游诗人": "bard", "bard": "bard", "吟遊詩人": "bard",
    "机工士": "machinist", "machinist": "machinist", "機工士": "machinist",
    "舞者": "dancer", "dancer": "dancer", "踊り子": "dancer",
    # 魔法远程 / Caster DPS
    "黑魔法师": "blackmage", "black mage": "blackmage", "blackmage": "blackmage", "黒魔道士": "blackmage",
    "召唤师": "summoner", "summoner": "summoner", "召喚士": "summoner",
    "赤魔法师": "redmage", "red mage": "redmage", "redmage": "redmage", "赤魔道士": "redmage",
    "绘灵法师": "pictomancer", "pictomancer": "pictomancer", "ピクトマンサー": "pictomancer",
    "青魔法师": "bluemage", "blue mage": "bluemage", "bluemage": "bluemage", "青魔道士": "bluemage",
    # 治疗职业 / Healer
    "白魔法师": "whitemage", "white mage": "whitemage", "whitemage": "whitemage", "白魔道士": "whitemage",
    "学者": "scholar", "scholar": "scholar",
    "占星术士": "astrologian", "astrologian": "astrologian", "占星術師": "astrologian",
    "贤者": "sage", "sage": "sage", "賢者": "sage",
    # 基础职业 / Base Classes
    "剑术师": "gladiator", "gladiator": "gladiator", "剣術士": "gladiator",
    "斧术师": "marauder", "marauder": "marauder", "斧術士": "marauder",
    "枪术师": "lancer", "lancer": "lancer", "槍術士": "lancer",
    "弓箭手": "archer", "archer": "archer", "弓術士": "archer",
    "双剑师": "rogue", "rogue": "rogue", "双剣士": "rogue",
    "格斗家": "pugilist", "pugilist": "pugilist", "格闘士": "pugilist",
    "咒术师": "thaumaturge", "thaumaturge": "thaumaturge", "呪術士": "thaumaturge",
    "秘术师": "arcanist", "arcanist": "arcanist", "巴術士": "arcanist",
    "幻术师": "conjurer", "conjurer": "conjurer", "幻術士": "conjurer",
}


def job_icon_slug(spec_name: Optional[str]) -> str:
    """Return the icon slug for a job/class name (CN/EN/JP names accepted)."""
    if not spec_name:
        return ""
    return JOB_ICON_SLUGS.get(spec_name.strip().lower(), "")


# 职责分组：防护职业=坦克、治疗职业=治疗，其余战斗职业（近战/远程/魔法）=输出。
# 配色参考游戏内职责图标（坦克蓝 / 治疗绿 / 输出红）。
TANK_JOB_SLUGS = {"paladin", "warrior", "darkknight", "gunbreaker", "gladiator", "marauder"}
HEALER_JOB_SLUGS = {"whitemage", "scholar", "astrologian", "sage", "conjurer"}


def job_role(spec_name: Optional[str]) -> str:
    """Return "tank" / "healer" / "dps" for a job name, or "" when unknown."""
    slug = job_icon_slug(spec_name)
    if not slug:
        return ""
    if slug in TANK_JOB_SLUGS:
        return "tank"
    if slug in HEALER_JOB_SLUGS:
        return "healer"
    return "dps"


@lru_cache(maxsize=None)
def _job_icon_data_uri(slug: str) -> str:
    """Load a job icon and return it as a base64 data URI (cached)."""
    if not slug:
        return ""
    icon_path = _JOBS_ASSETS_DIR / f"{slug}.png"
    try:
        data = icon_path.read_bytes()
    except OSError:
        return ""
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def build_job_icon_css(spec_names: Iterable[str]) -> str:
    """Build one CSS rule per distinct job used in the report, embedding each icon once.

    Keeping the images in a small set of CSS classes avoids repeating a multi-KB
    base64 data URI on every table row while keeping the report fully offline.
    """
    rules: List[str] = []
    seen: set[str] = set()
    for spec_name in spec_names:
        slug = job_icon_slug(spec_name)
        if not slug or slug in seen:
            continue
        seen.add(slug)
        data_uri = _job_icon_data_uri(slug)
        if data_uri:
            rules.append(f'    .job-icon-{slug} {{ background-image: url("{data_uri}"); }}')
    return "\n".join(rules)


def _create_jinja_env() -> jinja2.Environment:
    """Create and configure the Jinja2 template environment with custom filters."""
    loaders: List[jinja2.BaseLoader] = []
    if _TEMPLATES_DIR.is_dir():
        loaders.append(jinja2.FileSystemLoader(str(_TEMPLATES_DIR)))
    try:
        loaders.append(jinja2.PackageLoader(__package__ or "fflogs_report", "templates"))
    except Exception:
        pass

    if len(loaders) > 1:
        loader: jinja2.BaseLoader = jinja2.ChoiceLoader(loaders)
    elif loaders:
        loader = loaders[0]
    else:
        loader = jinja2.FileSystemLoader(str(_TEMPLATES_DIR))

    env = jinja2.Environment(
        loader=loader,
        autoescape=jinja2.select_autoescape(["html", "xml"]),
    )

    def percentile_tier(percentile: Optional[float]) -> str:
        if percentile is None:
            return "gray"
        p = round(percentile)
        if p >= 100:
            return "gold"
        elif p >= 99:
            return "pink"
        elif p >= 95:
            return "orange"
        elif p >= 75:
            return "purple"
        elif p >= 50:
            return "blue"
        elif p >= 25:
            return "green"
        else:
            return "gray"

    def round_pct(val: Optional[float]) -> str:
        if val is None:
            return "N/A"
        return str(round(val))

    def format_metric(val: Optional[float]) -> str:
        if val is None:
            return "N/A"
        return f"{val:,.1f}"

    def format_int(val: Optional[int]) -> str:
        if val is None:
            return "0"
        return f"{val:,}"

    env.filters["percentile_tier"] = percentile_tier
    env.filters["round_pct"] = round_pct
    env.filters["format_metric"] = format_metric
    env.filters["format_int"] = format_int
    env.filters["job_slug"] = job_icon_slug
    env.filters["job_role"] = job_role

    return env


def get_jinja_env() -> jinja2.Environment:
    """Get or create the singleton Jinja2 environment."""
    global _jinja_env
    if _jinja_env is None:
        _jinja_env = _create_jinja_env()
    return _jinja_env


def format_html(
    results: CharacterAllRankings,
    include_empty: bool = False,
    base_site: str = "https://cn.fflogs.com",
    simple: bool = False,
) -> str:
    """Render an attractive standalone HTML report for character rankings using Jinja2.

    Args:
        simple: When True, render a static report without any interactive elements
            (no search/filter toolbar, no report links, no back-to-top button, no JS)
            and without the "rank / sample" table column.
    """
    env = get_jinja_env()
    template = env.get_template("report.html")

    displayed_zones = results.zone_rankings if include_empty else results.active_zones
    generation_time = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    job_icon_css = build_job_icon_css(
        r.spec_name for zr in displayed_zones for r in zr.rankings
    )

    timeframe_map = {
        "current": "当前榜单 (Current)",
        "today": "当前榜单 (Current)",
        "history": "出分时历史榜单 (History)",
        "historical": "出分时历史榜单 (History)",
    }
    timeframe_display = timeframe_map.get(results.timeframe, results.timeframe)

    return template.render(
        results=results,
        include_empty=include_empty,
        base_site=base_site.rstrip("/"),
        displayed_zones=displayed_zones,
        generation_time=generation_time,
        timeframe_display=timeframe_display,
        job_icon_css=job_icon_css,
        simple=simple,
    )

