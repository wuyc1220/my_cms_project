"""
SOAP 内容分发模块使用示例

本文件演示如何在业务代码中使用 SOAP 模块进行内容分发
"""

from app.soap import SOAPCommandService, soap_settings


# ==========================================
# 示例 1：发布内容到 LSP
# ==========================================
async def example_publish_content():
    """发布内容示例"""
    
    # 检查是否启用 SOAP
    if not soap_settings.enabled:
        print("SOAP 内容分发未启用")
        return
    
    # 创建服务实例
    service = SOAPCommandService()
    
    # 发布电影内容
    result = await service.publish_content(
        content_id="MOVIE_12345",
        content_type="movie",
        title="示例电影",
        file_url="http://cdn.example.com/movies/movie123.mp4",
        duration=7200,  # 2小时
        metadata={
            "Director": "张三",
            "Actor": "李四,王五",
            "Genre": "动作",
            "Year": "2024"
        }
    )
    
    if result["success"]:
        print(f"✅ 内容发布请求成功")
        print(f"   ContentID: {result['content_id']}")
        print(f"   CorrelateID: {result['correlate_id']}")
        print(f"   XML URL: {result['xml_url']}")
    else:
        print(f"❌ 内容发布请求失败")
        print(f"   错误: {result['error_description']}")


# ==========================================
# 示例 2：从 LSP 下线内容
# ==========================================
async def example_unpublish_content():
    """下线内容示例"""
    
    if not soap_settings.enabled:
        print("SOAP 内容分发未启用")
        return
    
    service = SOAPCommandService()
    
    result = await service.unpublish_content(
        content_id="MOVIE_12345",
        content_type="movie",
        reason="版权到期"
    )
    
    if result["success"]:
        print(f"✅ 内容下线请求成功")
        print(f"   CorrelateID: {result['correlate_id']}")
    else:
        print(f"❌ 内容下线请求失败")
        print(f"   错误: {result['error_description']}")


# ==========================================
# 示例 3：在路由中使用 SOAP 服务
# ==========================================
"""
在路由文件中使用示例：

from fastapi import APIRouter
from app.soap import SOAPCommandService, soap_settings

router = APIRouter()

@router.post("/content/{content_id}/publish")
async def publish_content_to_lsp(content_id: str):
    '''发布内容到 LSP'''
    if not soap_settings.enabled:
        return {"success": False, "message": "SOAP 功能未启用"}
    
    # 从数据库获取内容信息
    content = await get_content_from_db(content_id)
    
    # 调用 SOAP 服务
    service = SOAPCommandService()
    result = await service.publish_content(
        content_id=content.id,
        content_type=content.type,
        title=content.title,
        file_url=content.file_url,
        duration=content.duration
    )
    
    # 保存 CorrelateID 到数据库，用于后续状态跟踪
    await save_correlate_id(content_id, result["correlate_id"])
    
    return result
"""


# ==========================================
# 示例 4：处理 LSP 结果通知
# ==========================================
"""
结果通知处理示例（已在 router.py 中实现）：

当 LSP 执行完指令后，会调用你的接口：
POST /soap/result-notify

请求体：
{
    "CSPID": "SAAT-CMS-001",
    "LSPID": "ZTE-MW-001",
    "CorrelateID": "uuid-xxxxx",
    "CmdResult": 0,
    "ResultFileURL": "http://lsp.com/results/xxx.xml"  // 可选
}

你需要在 router.py 中补充业务逻辑：
- 根据 CorrelateID 查找对应的内容
- 更新内容分发状态（published/failed）
- 如果有 ResultFileURL，下载并解析结果文件
"""


# ==========================================
# 示例 5：直接使用底层 API
# ==========================================
async def example_low_level_api():
    """使用底层 SOAP 客户端 API"""
    from app.soap import SOAPClient, XMLCommandGenerator
    
    if not soap_settings.enabled:
        return
    
    # 创建 XML 生成器
    xml_gen = XMLCommandGenerator()
    
    # 生成自定义 XML
    xml_path = xml_gen.generate_content_publish_xml(
        content_id="CUSTOM_001",
        content_type="series",
        title="自定义内容",
        file_url="http://example.com/video.mp4"
    )
    
    # 获取 URL
    xml_url = xml_gen.get_xml_url(xml_path)
    
    # 创建 SOAP 客户端
    soap_client = SOAPClient()
    
    # 发送请求
    result = soap_client.send_exec_cmd_req(
        cmd_file_url=xml_url,
        correlate_id="custom-correlate-id-001"
    )
    
    print(f"结果: {result}")
