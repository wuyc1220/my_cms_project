-- 清理内容审批测试数据脚本
-- 用于测试"被拒绝后重新提交"功能
-- 执行此脚本会将指定内容的审批记录、审批任务、流程记录软删除

-- 使用方法：
-- 1. 将下面的 :content_id 替换为实际的内容ID（或者直接使用 \set content_id 91）
-- 2. 在数据库中执行此脚本

-- 方式1：使用 psql 的变量设置（推荐）
-- \set content_id 91

-- 方式2：直接替换下面的数字
DO $$
DECLARE
    v_content_id INTEGER := 91;  -- 请修改这里的内容ID
    affected_rows INTEGER;
BEGIN
    -- 开始事务
    -- 1. 软删除 content_review 审批记录
    UPDATE content_review
    SET is_deleted = true,
        updated_at = NOW()
    WHERE content_id = v_content_id
      AND is_deleted = false;

    GET DIAGNOSTICS affected_rows = ROW_COUNT;
    RAISE NOTICE '软删除审批记录: % 条', affected_rows;

    -- 2. 软删除 task 审批任务 (review L1/L2/L3)
    UPDATE task
    SET is_deleted = true,
        updated_at = NOW()
    WHERE content_id = v_content_id
      AND task_type IN ('review L1', 'review L2', 'review L3')
      AND is_deleted = false;

    GET DIAGNOSTICS affected_rows = ROW_COUNT;
    RAISE NOTICE '软删除审批任务: % 条', affected_rows;

    -- 3. 软删除 content_process 流程记录 (ContentReview)
    UPDATE content_process
    SET is_deleted = true,
        updated_at = NOW()
    WHERE content_id = v_content_id
      AND name = 'ContentReview'
      AND is_deleted = false;

    GET DIAGNOSTICS affected_rows = ROW_COUNT;
    RAISE NOTICE '软删除流程记录: % 条', affected_rows;

    -- 4. 恢复 arrangement 任务为 Pending 状态（可选，如果需要重新编排）
    -- UPDATE task
    -- SET task_status = 'Pending',
    --     end_time = NULL,
    --     updated_at = NOW()
    -- WHERE content_id = v_content_id
    --   AND task_type = 'arrangement'
    --   AND is_deleted = false;
    --
    -- GET DIAGNOSTICS affected_rows = ROW_COUNT;
    -- RAISE NOTICE '恢复编排任务: % 条', affected_rows;

    RAISE NOTICE '内容ID % 的审批数据已清理完成，可以重新发起审核', v_content_id;
END $$;
