"""
内容采编模块 - 数据模型
"""
from .cast_role_map import CastRoleMap
from .content_metadata import (
    ContentMetadata,
    SeriesMetadata,
    ChannelMetadata,
    ScheduleMetadata,
)
from .content_status_log import ContentStatusLog
from .content_process import ContentProcess
from .movie import Movie
from .movie_history import MovieHistory
from .episode_history import EpisodeHistory

__all__ = [
    "CastRoleMap",
    "ContentMetadata",
    "SeriesMetadata",
    "ChannelMetadata",
    "ScheduleMetadata",
    "ContentStatusLog",
    "ContentProcess",
    "Movie",
    "MovieHistory",
    "EpisodeHistory",
]
