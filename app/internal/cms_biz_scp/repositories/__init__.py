"""
CP/SP管理模块 - 数据访问层
"""
from .provider_repository import (
    get_provider_by_id,
    get_provider_by_name,
    list_providers_query,
    get_providers_by_ids,
    get_all_providers_simple,
    count_active_providers,
    count_provider_contracts,
    has_active_contracts,
    get_provider_contracts,
    add_provider,
    get_user_display_name,
)
from .contract_repository import (
    get_contract_by_id,
    get_contract_by_name,
    list_contracts_query,
    get_contracts_by_ids,
    get_all_contracts_simple,
    count_contracts_without_license,
    add_contract,
    get_contract_platforms,
    replace_contract_platforms,
    has_active_licenses,
    get_contract_attachments,
    get_attachment_by_id,
    add_attachment,
    delete_attachment,
)
from .license_repository import (
    get_license_by_id,
    get_license_by_name,
    list_licenses_query,
    get_licenses_by_ids,
    get_contract_licenses,
    add_license,
    get_license_platforms,
    replace_license_platforms,
    count_license_contents,
    get_license_content_ids,
    get_license_contents,
    add_license_content,
    get_license_content_by_ids,
    delete_license_content,
    get_content_by_id,
    list_available_contents_query,
    count_contents_without_license,
    get_genre_name,
    get_license_names_by_content_id,
)

__all__ = [
    # provider
    'get_provider_by_id', 'get_provider_by_name', 'list_providers_query',
    'get_providers_by_ids', 'get_all_providers_simple', 'count_active_providers',
    'count_provider_contracts', 'has_active_contracts', 'get_provider_contracts',
    'add_provider', 'get_user_display_name',
    # contract
    'get_contract_by_id', 'get_contract_by_name', 'list_contracts_query',
    'get_contracts_by_ids', 'get_all_contracts_simple', 'count_contracts_without_license',
    'add_contract', 'get_contract_platforms', 'replace_contract_platforms',
    'has_active_licenses', 'get_contract_attachments', 'get_attachment_by_id',
    'add_attachment', 'delete_attachment',
    # license
    'get_license_by_id', 'get_license_by_name', 'list_licenses_query',
    'get_licenses_by_ids', 'get_contract_licenses', 'add_license',
    'get_license_platforms', 'replace_license_platforms',
    'count_license_contents', 'get_license_content_ids', 'get_license_contents',
    'add_license_content', 'get_license_content_by_ids', 'delete_license_content',
    'get_content_by_id', 'list_available_contents_query', 'count_contents_without_license',
    'get_genre_name', 'get_license_names_by_content_id',
]
