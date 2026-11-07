"""Data models for FF Logs rankings."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class EncounterInfo:
    id: int
    name: str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> EncounterInfo:
        return cls(id=data["id"], name=data.get("name", ""))


@dataclass
class ZoneInfo:
    id: int
    name: str
    frozen: bool = False
    encounters: List[EncounterInfo] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any], custom_name: Optional[str] = None) -> ZoneInfo:
        encounters = [
            EncounterInfo.from_dict(e) for e in data.get("encounters", [])
        ]
        return cls(
            id=data["id"],
            name=custom_name if custom_name else data.get("name", ""),
            frozen=data.get("frozen", False),
            encounters=encounters,
        )


@dataclass
class RankingEntry:
    encounter_id: int
    encounter_name: str
    class_name: str
    spec_name: str
    rank: int
    out_of: int
    percentile: float
    total: float
    duration_ms: int
    start_time_ms: int
    report_id: str
    fight_id: int
    difficulty: Optional[int] = None
    size: Optional[int] = None
    character_name: str = ""
    server_name: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> RankingEntry:
        return cls(
            encounter_id=data.get("encounterID", 0),
            encounter_name=data.get("encounterName", "Unknown"),
            class_name=data.get("class", ""),
            spec_name=data.get("spec", ""),
            rank=data.get("rank", 0),
            out_of=data.get("outOf", 0),
            percentile=float(data.get("percentile", 0.0)),
            total=float(data.get("total", 0.0)),
            duration_ms=data.get("duration", 0),
            start_time_ms=data.get("startTime", 0),
            report_id=data.get("reportID", ""),
            fight_id=data.get("fightID", 0),
            difficulty=data.get("difficulty"),
            size=data.get("size"),
            character_name=data.get("characterName", ""),
            server_name=data.get("server", ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a dict compatible with :meth:`from_dict` (used by the query cache)."""
        return {
            "encounterID": self.encounter_id,
            "encounterName": self.encounter_name,
            "class": self.class_name,
            "spec": self.spec_name,
            "rank": self.rank,
            "outOf": self.out_of,
            "percentile": self.percentile,
            "total": self.total,
            "duration": self.duration_ms,
            "startTime": self.start_time_ms,
            "reportID": self.report_id,
            "fightID": self.fight_id,
            "difficulty": self.difficulty,
            "size": self.size,
            "characterName": self.character_name,
            "server": self.server_name,
        }

    @property
    def duration_str(self) -> str:
        seconds = self.duration_ms // 1000
        mins = seconds // 60
        secs = seconds % 60
        return f"{mins:02d}:{secs:02d}"

    @property
    def start_time_str(self) -> str:
        if not self.start_time_ms:
            return "N/A"
        try:
            dt = datetime.datetime.fromtimestamp(
                self.start_time_ms / 1000.0, tz=datetime.timezone.utc
            ).astimezone()
            return dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return "N/A"

    @property
    def start_date_str(self) -> str:
        """Start time formatted as a date only (YYYY-MM-DD), used by the simple HTML report."""
        if not self.start_time_ms:
            return "N/A"
        try:
            dt = datetime.datetime.fromtimestamp(
                self.start_time_ms / 1000.0, tz=datetime.timezone.utc
            ).astimezone()
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return "N/A"

    def report_url(self, host: str = "https://cn.fflogs.com") -> str:
        clean_host = host.rstrip("/")
        return f"{clean_host}/reports/{self.report_id}#fight={self.fight_id}"

    @property
    def tier_name(self) -> str:
        p = round(self.percentile)
        if p == 100:
            return "金 (Rank 1)"
        elif p >= 99:
            return "粉 (99)"
        elif p >= 95:
            return "橙 (95-98)"
        elif p >= 75:
            return "紫 (75-94)"
        elif p >= 50:
            return "蓝 (50-74)"
        elif p >= 25:
            return "绿 (25-49)"
        else:
            return "灰 (0-24)"


@dataclass
class ZoneRanking:
    zone_id: int
    zone_name: str
    frozen: bool = False
    rankings: List[RankingEntry] = field(default_factory=list)

    @property
    def has_rankings(self) -> bool:
        return len(self.rankings) > 0

    @property
    def best_percentile(self) -> Optional[float]:
        if not self.rankings:
            return None
        return max(r.percentile for r in self.rankings)

    @property
    def avg_percentile(self) -> Optional[float]:
        if not self.rankings:
            return None
        return sum(r.percentile for r in self.rankings) / len(self.rankings)

    def get_best_per_encounter(self) -> List[RankingEntry]:
        """Keep only the ranking entry with the highest percentile for each encounter in this zone."""
        best_map: Dict[int, RankingEntry] = {}
        encounter_order: List[int] = []

        for r in self.rankings:
            enc_id = r.encounter_id
            if enc_id not in best_map:
                best_map[enc_id] = r
                encounter_order.append(enc_id)
            else:
                existing = best_map[enc_id]
                # Compare by percentile; tie-break by total (DPS/HPS) and start_time_ms
                if (r.percentile, r.total, r.start_time_ms) > (
                    existing.percentile,
                    existing.total,
                    existing.start_time_ms,
                ):
                    best_map[enc_id] = r

        return [best_map[enc_id] for enc_id in encounter_order]


@dataclass
class CharacterAllRankings:
    character_name: str
    server_name: str
    server_region: str
    metric: str
    timeframe: str
    zone_rankings: List[ZoneRanking] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Ensure zone rankings are always sorted by zone_id in descending order."""
        self.zone_rankings.sort(key=lambda zr: zr.zone_id, reverse=True)

    @property
    def total_zones_queried(self) -> int:
        return len(self.zone_rankings)

    @property
    def active_zones(self) -> List[ZoneRanking]:
        return [zr for zr in self.zone_rankings if zr.has_rankings]

    @property
    def total_active_zones(self) -> int:
        return len(self.active_zones)

    @property
    def all_rankings(self) -> List[RankingEntry]:
        result: List[RankingEntry] = []
        for zr in self.zone_rankings:
            result.extend(zr.rankings)
        return result

    @property
    def total_rankings(self) -> int:
        return len(self.all_rankings)

    @property
    def overall_best_percentile(self) -> Optional[float]:
        all_r = self.all_rankings
        if not all_r:
            return None
        return max(r.percentile for r in all_r)

    @property
    def overall_avg_percentile(self) -> Optional[float]:
        all_r = self.all_rankings
        if not all_r:
            return None
        return sum(r.percentile for r in all_r) / len(all_r)

    @property
    def jobs_played(self) -> List[str]:
        """Job names ordered by how many ranking records they appear in (descending).

        Records with an empty job name are ignored. Jobs with the same count are
        ordered by name for a stable, deterministic result.
        """
        counts: Dict[str, int] = {}
        for r in self.all_rankings:
            if r.spec_name:
                counts[r.spec_name] = counts.get(r.spec_name, 0) + 1
        return [
            job
            for job, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ]

    def with_filtering(self, show_all: bool = False) -> CharacterAllRankings:
        """Return a CharacterAllRankings instance with rankings filtered.

        If show_all is False, keeps only the highest percentile record per encounter.
        If show_all is True, preserves all ranking records.
        """
        if show_all:
            return self

        filtered_zones = [
            ZoneRanking(
                zone_id=zr.zone_id,
                zone_name=zr.zone_name,
                frozen=zr.frozen,
                rankings=zr.get_best_per_encounter(),
            )
            for zr in self.zone_rankings
        ]
        return CharacterAllRankings(
            character_name=self.character_name,
            server_name=self.server_name,
            server_region=self.server_region,
            metric=self.metric,
            timeframe=self.timeframe,
            zone_rankings=filtered_zones,
        )
