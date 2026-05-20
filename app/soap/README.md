# SOAP 内容分发模块

## 概述

本模块实现了 CSP（内容服务提供商）与 LSP（本地服务提供商）之间的 SOAP + XML 内容分发协议。

## 目录结构

```
backend/app/soap/
├── __init__.py           # 模块导出
├── config.py             # 配置管理（环境变量）
├── client.py             # SOAP 客户端和指令服务
├── xml_generator.py      # XML 指令文件生成器
├── router.py             # 结果通知接收接口
├── file_server.py        # XML 文件下载服务
└── usage_example.py      # 使用示例
```

## 快速开始

### 1. 安装依赖

```bash
pip install zeep
```

或更新 requirements：

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

在 `backend/.env` 文件中添加以下配置：

```env
# 启用 SOAP 功能
SOAP_ENABLED=true

# CSP 标识（本系统）
SOAP_CSP_ID=SAAT-CMS-001

# LSP 标识（对接方）
SOAP_LSP_ID=ZTE-MW-001

# LSP SOAP WSDL 地址（由 LSP 提供）
SOAP_LSP_SOAP_URL=http://lsp-server.com/soap/wsdl

# XML 文件本地存储路径
SOAP_XML_STORAGE_PATH=./soap_commands

# XML 文件访问 URL（LSP 需能访问）
SOAP_XML_BASE_URL=http://cms-server.com/commands

# 超时和重试配置
SOAP_TIMEOUT=30
SOAP_MAX_RETRIES=3
SOAP_RETRY_DELAY=5
```

### 3. 使用示例

#### 发布内容

```python
from app.soap import SOAPCommandService

# 创建服务实例
service = SOAPCommandService()

# 发布内容到 LSP
result = await service.publish_content(
    content_id="MOVIE_12345",
    content_type="movie",
    title="示例电影",
    file_url="http://cdn.example.com/movies/movie123.mp4",
    duration=7200,
    metadata={
        "Director": "张三",
        "Actor": "李四",
        "Genre": "动作"
    }
)

if result["success"]:
    print(f"发布成功，CorrelateID: {result['correlate_id']}")
else:
    print(f"发布失败: {result['error_description']}")
```

#### 下线内容

```python
result = await service.unpublish_content(
    content_id="MOVIE_12345",
    content_type="movie",
    reason="版权到期"
)
```

### 4. API 接口

模块自动注册以下路由：

#### 结果通知接收

```
POST /soap/result-notify
```

接收 LSP 的命令执行结果通知。

#### XML 文件下载

```
GET /commands/{filename}
```

提供 XML 指令文件下载服务（LSP 调用）。

## 配置说明

所有配置项都支持环境变量覆盖，前缀为 `SOAP_`：

| 配置项 | 说明 | 默认值 | 必填 |
|--------|------|--------|------|
| SOAP_ENABLED | 是否启用 SOAP 功能 | false | 否 |
| SOAP_CSP_ID | CSP 标识 | SAAT-CMS-001 | 是 |
| SOAP_LSP_ID | LSP 标识 | ZTE-MW-001 | 是 |
| SOAP_LSP_SOAP_URL | LSP WSDL 地址 | - | 是 |
| SOAP_XML_STORAGE_PATH | XML 存储路径 | ./soap_commands | 否 |
| SOAP_XML_BASE_URL | XML 访问 URL | - | 是 |
| SOAP_TIMEOUT | 请求超时（秒） | 30 | 否 |
| SOAP_MAX_RETRIES | 重试次数 | 3 | 否 |
| SOAP_RETRY_DELAY | 重试延迟（秒） | 5 | 否 |

## 工作流程

```
CMS (CSP)                              LSP
  |                                     |
  |-- 生成 XML 指令文件                  |
  |-- POST /soap/result-notify -------->|
  |   (CorrelateID, CmdFileURL)         |
  |                                     |-- 下载 XML
  |                                     |-- 执行指令
  |                                     |
  |<-- ResultNotifyReq -----------------|
  |   (CorrelateID, CmdResult)          |
  |-- ResultNotifyRes ----------------->|
  |                                     |
  |-- 更新内容状态                       |
```

## 扩展自定义指令

如需添加新的指令类型，在 `xml_generator.py` 中添加对应方法：

```python
def generate_custom_xml(self, content_id: str, **kwargs) -> str:
    """生成自定义指令 XML"""
    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<Command>
    <Header>
        <CommandType>CustomCommand</CommandType>
    </Header>
    <Content>
        <ID>{content_id}</ID>
    </Content>
</Command>"""
    # 保存到文件并返回路径
    ...
```

## 注意事项

1. **网络配置**：确保 LSP 能访问 `SOAP_XML_BASE_URL`
2. **安全配置**：生产环境建议使用 HTTPS
3. **状态跟踪**：保存 CorrelateID 到数据库，用于关联结果通知
4. **错误处理**：所有方法都返回统一的结果字典，包含 success 字段

## 对接清单

与 LSP 对接时需要确认：

- [ ] CSPID 和 LSPID 的值
- [ ] LSP SOAP WSDL 地址
- [ ] XML 文件访问地址（公网 IP 或域名）
- [ ] 支持的指令类型和 XML 格式
- [ ] 结果通知的超时时间
- [ ] 是否需要认证（Token、Basic Auth 等）


┌─────────────────────────────────────────────────────────────────────────────┐
│                          内容发布流程 (Publish)                              │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  业务层触发                                                                  │
│      │                                                                      │
│      ▼                                                                      │
│  ┌────────────────────────────────────────────────────────────────────┐     │
│  │ SOAPCommandService.publish_content()                              │     │
│  │  1. 参数校验: content_id, content_type, title, file_url          │     │
│  │                    │                                              │     │
│  │                    ▼                                              │     │
│  │  2. XMLCommandGenerator.generate_content_publish_xml()           │     │
│  │     - 生成 CommandID (UUID)                                       │     │
│  │     - 组装 XML 内容                                               │     │
│  │     - 保存到本地存储目录                                          │     │
│  │                    │                                              │     │
│  │                    ▼                                              │     │
│  │  3. 构造 XML 文件 URL                                             │     │
│  │     {SOAP_XML_BASE_URL}/{publish_contentId_timestamp.xml}        │     │
│  │                    │                                              │     │
│  │                    ▼                                              │     │
│  │  4. 生成 CorrelateID (UUID)                                       │     │
│  │                    │                                              │     │
│  │                    ▼                                              │     │
│  │  5. SOAPClient.send_exec_cmd_req()                                │     │
│  │     POST SOAP 请求到 LSP_WSDL_URL                                 │     │
│  │     Body: {CSPID, LSPID, CorrelateID, CmdFileURL}                │     │
│  │                    │                                              │     │
│  │                    ▼                                              │     │
│  │  6. 保存 CorrelateID 到 PublishTask/IngestHistory                 │     │
│  └────────────────────────────────────────────────────────────────────┘     │
│                                                                             │
│                          ↓ LSP 端处理 ↓                                      │
│                                                                             │
│  ┌────────────────────────────────────────────────────────────────────┐     │
│  │ LSP 接收到 ExecCmdReq                                             │     │
│  │  1. GET {CmdFileURL} 下载 XML 文件                                 │     │
│  │  2. 解析 XML，执行发布指令                                         │     │
│  │  3. 生成执行结果                                                   │     │
│  └────────────────────────────────────────────────────────────────────┘     │
│                                                                             │
│                          ↓ LSP 回调 ↓                                        │
│                                                                             │
│  ┌────────────────────────────────────────────────────────────────────┐     │
│  │ POST /soap/result-notify                                          │     │
│  │  Request: {CSPID, LSPID, CorrelateID, CmdResult, ResultFileURL}   │     │
│  │                    │                                              │     │
│  │                    ▼                                              │     │
│  │  1. 根据 CorrelateID 查找 PublishTask                             │     │
│  │  2. 下载 Result XML（如果有）                                     │     │
│  │  3. 更新 IngestHistory.status                                     │     │
│  │  4. 更新 PublishTask.status / publish_status                      │     │
│  │  5. 返回 ResultNotifyRes {Result: 0/-1, ErrorDescription}         │     │
│  └────────────────────────────────────────────────────────────────────┘     │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘