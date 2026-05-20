# CMS 模块化单体架构说明

## 架构概览

本项目已从传统的平铺式架构迁移到**模块化单体架构**，实现了模块隔离、Service接口交互和统一异常处理。

## 新目录结构

```
backend/app/
├── cms_main/                  # 入口模块
│   ├── main.py               # FastAPI应用入口（保留原有）
│   └── dependencies.py       # 公共依赖（get_db, 认证等）
├── routers/                  # 路由层 - 按业务模块组织
│   ├── __init__.py          # 总路由注册
│   ├── cms_biz_system.py    # 系统管理模块路由
│   └── ...                  # 其他模块路由
├── internal/                 # 业务模块内部实现
│   ├── cms-biz-system/      # 系统管理模块
│   │   ├── core/            # 核心组件
│   │   │   ├── exceptions.py  # 统一异常处理
│   │   │   ├── transactions.py # 事务装饰器
│   │   │   └── i18n.py       # 多语言支持
│   │   ├── utils/           # 工具函数
│   │   ├── models/          # 数据模型
│   │   └── services/        # 业务服务
│   │       └── __init__.py  # 显式导出公开接口
│   ├── cms-biz-scp/         # CP/SP管理模块
│   ├── cms-biz-metada/      # 内容元数据模块
│   ├── cms-biz-orchestration/ # 内容采编模块
│   ├── cms-biz-package/     # 内容打包模块
│   ├── cms-biz-publish/     # 内容发布模块（待实现）
│   └── cms-biz-stat/        # 运营统计模块（待实现）
├── api/                     # 旧路由层（向后兼容，逐步废弃）
├── models/                  # 旧模型层（向后兼容，逐步迁移）
├── schemas/                 # Pydantic模型（保留）
└── services/                # 旧服务层（向后兼容，逐步迁移）
```

## 核心特性

### 1. 模块化隔离

每个业务模块拥有独立的：
- 路由（routers/）
- 数据模型（models/）
- 业务服务（services/）
- 工具函数（utils/）
- 核心组件（core/）

### 2. Service接口交互

**模块间只能通过Service接口调用**，禁止直接操作其他模块的数据库表。

```python
# ✅ 正确：通过Service接口调用
from app.internal.cms-biz-system.services import list_users

users = await list_users(db=db, page=1, page_size=10)

# ❌ 错误：直接import其他模块的ORM Model
from app.internal.cms-biz-system.models import User
```

### 3. 统一异常处理

所有业务异常使用 `BusinessException` 及其子类：

```python
from app.internal.cms-biz-system.core import (
    BusinessException,
    NotFoundException,
    ErrorCode
)

# 抛出业务异常
raise NotFoundException(message="用户不存在")
raise BusinessException(ErrorCode.USER_NOT_FOUND, "用户不存在")
```

### 4. 事务管理

使用 `@transactional` 装饰器自动管理事务：

```python
from app.internal.cms-biz-system.core import transactional

@transactional
async def create_user(db: AsyncSession, user_data: UserCreate):
    # 业务逻辑
    # 自动commit，异常自动rollback
    pass
```

### 5. 多语言支持

异常消息支持多语言：

```python
from app.internal.cms-biz-system.core import get_message, get_accept_language

# 根据Accept-Language头获取语言
lang = get_accept_language(request.headers.get("Accept-Language"))

# 获取多语言消息
message = get_message("USER_NOT_FOUND", lang=lang, username="admin")
```

## 模块依赖链

```
system → metada → scp → orchestration → package → publish → stat
```

**禁止循环依赖**：如果A→B且B→A，必须通过事件/回调解耦。

## 迁移策略

### 渐进式迁移

1. **保持向后兼容**：原有的 `api/`, `models/`, `services/` 目录暂时保留
2. **逐步迁移**：逐个模块迁移到新架构
3. **双轨运行**：新旧架构可以并存，直到完全迁移

### 当前状态

- ✅ **cms-biz-system**：已完成迁移框架
- ⏳ **cms-biz-scp**：已创建Service导出
- ⏳ **cms-biz-metada**：已创建Service导出
- ⏳ **cms-biz-orchestration**：已创建Service导出
- ⏳ **cms-biz-package**：已创建Service导出
- 🔲 **cms-biz-publish**：待实现
- 🔲 **cms-biz-stat**：待实现

## API路由变更

### 旧路由（保留兼容）
```
/api/v1/users
/api/v1/roles
/api/v1/configs
```

### 新路由（推荐）
```
/api/v1/system/users
/api/v1/system/roles
/api/v1/system/configs
/api/v1/scp/providers
/api/v1/metada/casts
```

## 开发规范

### 新增模块

1. 在 `internal/` 下创建模块目录
2. 创建 `core/`, `utils/`, `models/`, `services/` 子目录
3. 在 `services/__init__.py` 中显式导出公开接口
4. 在 `routers/` 下创建模块路由文件
5. 在 `routers/__init__.py` 中注册路由

### Service接口规范

```python
# internal/cms-biz-xxx/services/__init__.py
from .xxx_service import list_xxx, get_xxx, create_xxx

__all__ = ['list_xxx', 'get_xxx', 'create_xxx']
```

### 路由规范

```python
# routers/cms_biz_xxx.py
from fastapi import APIRouter, Depends
from app.cms_main.dependencies import get_db
from app.internal.cms-biz-xxx.services import list_xxx

router = APIRouter(prefix="/xxx", tags=["xxx"])

@router.get("")
async def api_list_xxx(db = Depends(get_db)):
    return await list_xxx(db=db)
```

## 注意事项

1. **模块名使用连字符**：目录名使用 `cms-biz-xxx`，但Python文件名使用下划线 `cms_biz_xxx.py`
2. **使用importlib导入**：由于连字符不是合法的Python标识符，需要使用 `importlib.import_module()`
3. **禁止跨模块直接访问Model**：只能通过Service接口交互
4. **统一异常处理**：所有业务异常必须使用 `BusinessException`
5. **事务管理**：使用 `@transactional` 装饰器，禁止手动commit/rollback

## 后续计划

1. 完成所有模块的Service迁移
2. 实现cms-biz-publish模块
3. 实现cms-biz-stat模块
4. 添加统一响应中间件
5. 完善多语言资源文件
6. 添加模块间事件机制
7. 编写集成测试
