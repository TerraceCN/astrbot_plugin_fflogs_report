"""FF Logs Report package."""

from .client import (
    APIRequestError,
    AuthenticationError,
    CharacterHiddenError,
    CharacterNotFoundError,
    FFLogsClient,
    FFLogsError,
)
from .formatter import format_html
from .models import (
    CharacterAllRankings,
    EncounterInfo,
    RankingEntry,
    ZoneInfo,
    ZoneRanking,
)

__all__ = [
    "FFLogsClient",
    "FFLogsError",
    "AuthenticationError",
    "CharacterNotFoundError",
    "CharacterHiddenError",
    "APIRequestError",
    "ZoneInfo",
    "EncounterInfo",
    "RankingEntry",
    "ZoneRanking",
    "CharacterAllRankings",
    "format_html",
]
