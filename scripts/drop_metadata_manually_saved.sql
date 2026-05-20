-- =====================================================
-- 删除 content 表的 metadata_manually_saved 字段
-- =====================================================
-- 用途：改为使用流程表判断 Metadata 节点完成状态，不再需要此冗余字段
-- 创建日期：2026-05-14
-- 对应迁移：0083_drop_metadata_manually_saved_from_content.py
-- =====================================================

-- Upgrade: 删除 metadata_manually_saved 字段
ALTER TABLE content DROP COLUMN IF EXISTS metadata_manually_saved;

-- 验证字段是否已删除
-- SELECT column_name FROM information_schema.columns 
-- WHERE table_name = 'content' AND column_name = 'metadata_manually_saved';

-- =====================================================
-- Downgrade (回滚脚本，仅在需要恢复时执行)
-- =====================================================
-- ALTER TABLE content ADD COLUMN metadata_manually_saved BOOLEAN NOT NULL DEFAULT false;
-- COMMENT ON COLUMN content.metadata_manually_saved IS '用户是否手动保存过元数据（通过元数据编辑弹框保存，非同步触发）';
