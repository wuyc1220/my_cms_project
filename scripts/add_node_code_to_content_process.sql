-- =====================================================
-- 为 content_process 表新增 node_code 字段
-- =====================================================
-- 用途：以节点编码作为流程匹配条件，替代 name 字段做硬编码匹配
--       避免节点显示名称变更导致匹配失效
-- 创建日期：2026-05-14
-- 对应迁移：0084_add_node_code_to_content_process.py
-- =====================================================

-- Upgrade: 添加 node_code 字段
ALTER TABLE content_process ADD COLUMN node_code VARCHAR(100);

-- 添加字段注释
COMMENT ON COLUMN content_process.node_code IS '节点编码，与 workflow_node_config.node_code 对应';

-- 回填历史数据：从 name 字段复制到 node_code
UPDATE content_process SET node_code = name WHERE node_code IS NULL;

-- 验证数据
-- SELECT id, name, node_code FROM content_process LIMIT 10;

-- =====================================================
-- Downgrade (回滚脚本，仅在需要恢复时执行)
-- =====================================================
-- ALTER TABLE content_process DROP COLUMN IF EXISTS node_code;
