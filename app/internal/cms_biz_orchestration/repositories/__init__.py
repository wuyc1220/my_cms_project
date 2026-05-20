"""
内容采编模块 - 数据访问层
"""
from .content_repository import (
    # Content
    get_content_by_id, list_contents_query, get_contents_by_ids,
    get_child_contents, count_child_contents, add_content,
    get_content_license_ids, get_content_license_info, count_contents_without_license,
    get_genre_name, get_content_packages,
    # PhysicalChannel
    get_physical_channel_by_id, list_physical_channels_query, get_physical_channels_by_ids, add_physical_channel,
    # LogicalChannel / Schedule / ChannelCategory — 模型尚未创建
    # get_logical_channel_by_id, list_logical_channels_query, get_logical_channels_by_ids, add_logical_channel,
    # get_schedule_by_id, list_schedules_query, add_schedule,
    # get_channel_categories_by_channel_id, add_channel_category,
)
from .metadata_repo import (
    # ContentMetadata
    get_content_metadata_by_content_id, add_content_metadata, delete_content_metadata_by_content_id,
    # SeriesMetadata
    get_series_metadata_by_content_id, add_series_metadata, delete_series_metadata_by_content_id,
    # ChannelMetadata
    get_channel_metadata_by_content_id, add_channel_metadata, delete_channel_metadata_by_content_id,
    # ScheduleMetadata
    get_schedule_metadata_by_content_id, add_schedule_metadata, delete_schedule_metadata_by_content_id,
)
from .movie_repo import (
    get_movie_by_id, list_movies_by_content_id, add_movie, delete_movie,
)
from .movie_history_repo import (
    add_movie_history, list_movie_history_by_content_id,
)
from .episode_history_repo import (
    add_episode_history, list_episode_history_by_parent_id, list_episode_history_by_parent_id_with_filters,
)
from .cast_role_map_repo import (
    get_cast_role_map_by_id, list_cast_role_maps,
    add_cast_role_map, update_cast_role_map, delete_cast_role_map,
)
from .status_log_repo import (
    add_status_log, list_status_logs_by_content_id,
)
from .process_repo import (
    add_process, list_processes_by_content_id,
    get_process_by_content_id_and_node_code, update_process_status,
)
