-- =====================================================
-- 为 content_process 表新增 node_code 字段
-- =====================================================
-- =====================================================

-- Upgrade: 添加 node_code 字段
ALTER TABLE content_process
    ADD COLUMN node_code VARCHAR(100);

COMMENT ON COLUMN content_process.node_code IS '节点编码，与 workflow_node_config.node_code 对应';



-- 如果列需要设置为 NOT NULL（视业务需求决定是否执行）

