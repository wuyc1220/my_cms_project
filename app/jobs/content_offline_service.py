"""
ContentOffline 业务逻辑：扫描过期许可证并将关联内容状态切换为 NoActiveLicense。

业务规则（源自需求说明）：
1. **扫描范围只限定 `status == 'Published'`（发布成功）** 的内容 —— 这类内容
   发布当时必然有许可证，其它状态（None / WaitingForMaterials / InProgress /
   ReadyForPublish / Publishing / PublishFailed / NoActiveLicense / Closed）
   要么尚未走到授权阶段、要么已经是终态，不参与扫描，避免无效遍历。
2. 判定为过期的条件（在已 Published 的内容上再筛）：
   - 内容关联的未软删除许可证中，**至少存在一条** `end_date < today`（已过期）；且
   - 内容关联的未软删除许可证中，**不存在任何** `end_date IS NULL OR end_date >= today`（即没有任何有效许可证）。
3. 过期后将该内容的 ingest 状态 `content.status` 更新为 `NoActiveLicense`。
4. 已软删除（is_deleted=True）或已删除许可证（is_deleted=True）均不参与判定。

容错策略（关键）：
- 每条内容在独立事务中更新（循环逐条 + commit/rollback），单条失败不会影响其它内容；
- 每条失败都记录详细原因（内容 ID/Title + 异常类型 + 异常消息）；
- 循环整体用外层 try/except 兜底，防止框架级异常导致日志回写丢失；
- 定时任务 result 字段汇总：成功列表 + 失败列表 + 统计计数。

执行结果（写入 scheduled_task_log.result）：
- 全部成功：`Offlined N contents: [...具体ID+Title...]`
- 部分失败：`Offlined X/N contents. Failed Y: [...每条错误原因...]`
- 全部失败：调度器（scheduler.py）会根据本函数抛出的异常写入 `"ExceptionType: message"`。

定时任务在内置调度器中按每日 00:00:01 触发（见 `app/jobs/scheduler.py`）。
"""

from datetime import date

from loguru import logger
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_package.models.package import Content
from app.internal.cms_biz_scp.models.trade import License, LicenseContent


# 只扫描发布成功状态的内容（其它状态不可能产生有效授权 → 无效过期）
_SCAN_STATUS = "Published"

# result 字段最多展示的条目数量（避免 Text 字段过长，调度器会截断到 2000 字符）
_MAX_DETAIL_ITEMS = 30


async def scan_and_offline_expired_contents(db: AsyncSession) -> str:
    """
    扫描并将"关联过许可证且所有未软删除许可证都已过期"的内容置为 NoActiveLicense。

    处理方式：逐条处理，每条使用独立事务（commit / rollback），单条失败不影响其它内容。

    Returns:
        str: 执行结果摘要字符串。
    """
    today = date.today()
    logger.info(f"[ContentOffline] ========== 任务开始 ==========")
    logger.info(f"[ContentOffline] 开始扫描过期许可证，基准日期={today}")
    logger.info(f"[ContentOffline] 扫描条件: status='{_SCAN_STATUS}', is_deleted=False")

    # 1. 子查询 A：拥有"已过期许可证"的内容
    has_expired_license_subq = (
        select(LicenseContent.content_id)
        .join(License, License.id == LicenseContent.license_id)
        .where(
            LicenseContent.is_deleted.is_(False),
            License.is_deleted.is_(False),
            License.end_date.is_not(None),
            License.end_date < today,
        )
        .distinct()
    )

    has_active_license_subq = (
        select(LicenseContent.content_id)
        .join(License, License.id == LicenseContent.license_id)
        .where(
            LicenseContent.is_deleted.is_(False),
            License.is_deleted.is_(False),
            or_(
                License.end_date.is_(None),
                License.end_date >= today,
            ),
        )
        .distinct()
    )

    # 3. 目标：status == Published AND 有过期许可证 AND 没有任何有效许可证
    #    只扫描 Published 的内容（这些内容发布时必然有许可证），避免扫描无意义数据。
    stmt = (
        select(Content.id, Content.title)
        .where(
            Content.is_deleted.is_(False),
            Content.status == _SCAN_STATUS,
            Content.id.in_(has_expired_license_subq),
            Content.id.notin_(has_active_license_subq),
        )
        .order_by(Content.id.asc())
    )
    rows = (await db.execute(stmt)).all()
    logger.info(f"[ContentOffline] 扫描完成，共找到 {len(rows)} 条待处理内容")

    if not rows:
        msg = f"No contents to offline (today={today}). scanned=0, updated=0, failed=0"
        logger.info(f"[ContentOffline] {msg}")
        logger.info(f"[ContentOffline] ========== 任务结束 ==========")
        return msg

    # 4. 逐条处理：每条独立事务 + try/except 兜底
    success_items: list[tuple[int, str]] = []
    failed_items: list[tuple[int, str, str]] = []  # (id, title, error_desc)
    logger.info(f"[ContentOffline] 开始逐条处理 {len(rows)} 条内容")

    for idx, row in enumerate(rows, 1):
        content_id, title = row.id, row.title
        logger.info(f"[ContentOffline] [{idx}/{len(rows)}] 处理内容 ID={content_id}, Title={title}")
        try:
            await db.execute(
                update(Content)
                .where(Content.id == content_id)
                .values(status="NoActiveLicense", previous_status=None)
            )
            await db.commit()
            success_items.append((content_id, title))
            logger.info(f"[ContentOffline] [{idx}/{len(rows)}] 内容 ID={content_id} 下线成功")
        except Exception as exc:  # noqa: BLE001 — 这里刻意捕获所有异常
            # 回滚当前这条的事务，保证后续条目继续可用
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001 — 回滚本身失败也不要让循环中断
                logger.exception(
                    f"[ContentOffline] rollback 失败 content_id={content_id}"
                )
            err_desc = f"{type(exc).__name__}: {exc}"
            failed_items.append((content_id, title, err_desc))
            logger.exception(
                f"[ContentOffline] [{idx}/{len(rows)}] 内容下线失败 content_id={content_id}, title={title}, 错误={err_desc}"
            )

    # 5. 构造汇总结果
    total = len(rows)
    ok_count = len(success_items)
    fail_count = len(failed_items)
    logger.info(f"[ContentOffline] 处理完成，总计={total}, 成功={ok_count}, 失败={fail_count}")

    success_detail = ", ".join(
        f"[ID={cid}, Title={t}]" for cid, t in success_items[:_MAX_DETAIL_ITEMS]
    )
    if ok_count > _MAX_DETAIL_ITEMS:
        success_detail += f", ... and {ok_count - _MAX_DETAIL_ITEMS} more"

    if fail_count == 0:
        msg = f"Offlined {ok_count}/{total} contents (today={today}): {success_detail}"
    else:
        fail_detail = "; ".join(
            f"[ID={cid}, Title={t}, Error={err}]"
            for cid, t, err in failed_items[:_MAX_DETAIL_ITEMS]
        )
        if fail_count > _MAX_DETAIL_ITEMS:
            fail_detail += f"; ... and {fail_count - _MAX_DETAIL_ITEMS} more"
        msg = (
            f"Offlined {ok_count}/{total} contents (today={today}). "
            f"Success: {success_detail or 'none'}. "
            f"Failed {fail_count}: {fail_detail}"
        )

    logger.info(f"[ContentOffline] {msg}")
    logger.info(f"[ContentOffline] ========== 任务结束 ==========")
    return msg
