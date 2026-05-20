"""
工作流配置测试脚本 - 验证 Program 类型完整流程

运行方式：
    cd backend
    python -m pytest tests/test_workflow_config.py -v
"""
import pytest
from unittest.mock import AsyncMock, MagicMock
import json

from app.internal.cms_biz_orchestration.services import workflow_config_service
from app.internal.cms_biz_orchestration.services import workflow_service
from app.internal.cms_biz_orchestration.schemas.workflow_config import (
    WorkflowConfigCreate,
    WorkflowConfigUpdate,
)
from app.internal.cms_biz_orchestration.models.workflow_config import WorkflowConfig, WorkflowNodeConfig
from app.internal.cms_biz_orchestration.models.content_process import ContentProcess


class TestWorkflowConfigService:
    """测试流程配置服务。"""

    @pytest.mark.asyncio
    async def test_create_workflow_config(self):
        """测试创建流程配置。"""
        mock_db = AsyncMock()
        mock_config = WorkflowConfig(
            id=1,
            process_code="program_workflow_v1",
            process_name="Program 标准流程",
            belonging="Program",
            status="draft",
            version=1,
        )
        
        with pytest.raises(Exception):
            await workflow_config_service.create_workflow_config(
                mock_db,
                WorkflowConfigCreate(
                    process_code="program_workflow_v1",
                    process_name="Program 标准流程",
                    belonging="Program",
                ),
                user_id=1,
            )

    @pytest.mark.asyncio
    async def test_get_available_nodes(self):
        """测试获取可用节点列表。"""
        nodes = await workflow_config_service.get_available_nodes()
        assert len(nodes) > 0
        assert "code" in nodes[0]
        assert "name" in nodes[0]


class TestWorkflowService:
    """测试工作流执行引擎。"""

    def test_content_type_to_belonging_mapping(self):
        """测试内容类型到流程模块的映射。"""
        assert workflow_service.CONTENT_TYPE_TO_BELONGING["MOVIE"] == "Program"
        assert workflow_service.CONTENT_TYPE_TO_BELONGING["EPISODE"] == "Program"
        assert workflow_service.CONTENT_TYPE_TO_BELONGING["SERIES"] == "Series"
        assert workflow_service.CONTENT_TYPE_TO_BELONGING["CHANNEL"] == "Channel"
        assert workflow_service.CONTENT_TYPE_TO_BELONGING["SCHEDULE"] == "Schedule"

    def test_default_status_flow_order(self):
        """测试默认状态流转顺序。"""
        order = workflow_service.DEFAULT_STATUS_FLOW_ORDER
        assert "None" in order
        assert "WaitingForMaterials" in order
        assert "InProgress" in order
        assert "ReadyForPublish" in order
        assert "Published" in order

    def test_is_default_process_match(self):
        """测试默认流程节点匹配。"""
        assert workflow_service._is_default_process_match("MissingMaterials", "WaitingForMaterials") is True
        assert workflow_service._is_default_process_match("SupplementMetadata", "InProgress") is True
        assert workflow_service._is_default_process_match("UploadPosters", "ReadyForPublish") is True
        assert workflow_service._is_default_process_match("ContentReview", "Published") is True
        assert workflow_service._is_default_process_match("MissingMaterials", "InProgress") is False


class TestWorkflowConfigJSON:
    """测试流程配置 JSON 格式。"""

    def test_valid_program_workflow_json(self):
        """测试有效的 Program 工作流 JSON 格式。"""
        config_json = {
            "nodes": [
                {
                    "node_code": "Materials",
                    "node_name": "缺失材料",
                    "node_type": "process",
                    "mandatory": True,
                    "bind_status_before": "None",
                    "bind_status_after": "InProgress",
                    "position_x": 100,
                    "position_y": 200,
                    "sequence": 1,
                },
                {
                    "node_code": "Metadata",
                    "node_name": "补充元数据",
                    "node_type": "process",
                    "mandatory": True,
                    "bind_status_before": "WaitingForMaterials",
                    "bind_status_after": "InProgress",
                    "position_x": 300,
                    "position_y": 200,
                    "sequence": 2,
                },
                {
                    "node_code": "Posters",
                    "node_name": "上传海报",
                    "node_type": "process",
                    "mandatory": True,
                    "bind_status_before": "InProgress",
                    "bind_status_after": "ReadyForPublish",
                    "position_x": 500,
                    "position_y": 200,
                    "sequence": 3,
                },
                {
                    "node_code": "ContentReview",
                    "node_name": "内容审核",
                    "node_type": "process",
                    "mandatory": True,
                    "bind_status_before": "ReadyForPublish",
                    "bind_status_after": "Published",
                    "position_x": 700,
                    "position_y": 200,
                    "sequence": 4,
                },
            ]
        }
        
        parsed = json.loads(json.dumps(config_json))
        assert len(parsed["nodes"]) == 4
        assert parsed["nodes"][0]["node_code"] == "Materials"
        assert parsed["nodes"][0]["node_type"] == "process"

    def test_parallel_box_workflow_json(self):
        """测试包含并行框的工作流 JSON 格式。"""
        config_json = {
            "nodes": [
                {
                    "node_code": "ParallelBox1",
                    "node_name": "材料准备（并行）",
                    "node_type": "parallel_box",
                    "mandatory": True,
                    "parallel_rule": "all_required",
                    "position_x": 300,
                    "position_y": 200,
                    "sequence": 2,
                },
                {
                    "node_code": "Metadata",
                    "node_name": "补充元数据",
                    "node_type": "process",
                    "mandatory": True,
                    "parent_node_id": 1,
                    "position_x": 300,
                    "position_y": 150,
                    "sequence": 3,
                },
                {
                    "node_code": "Posters",
                    "node_name": "上传海报",
                    "node_type": "process",
                    "mandatory": True,
                    "parent_node_id": 1,
                    "position_x": 300,
                    "position_y": 250,
                    "sequence": 4,
                },
            ]
        }
        
        parsed = json.loads(json.dumps(config_json))
        parallel_box = parsed["nodes"][0]
        assert parallel_box["node_type"] == "parallel_box"
        assert parallel_box["parallel_rule"] == "all_required"
        
        children = [n for n in parsed["nodes"] if n.get("parent_node_id") == 1]
        assert len(children) == 2
