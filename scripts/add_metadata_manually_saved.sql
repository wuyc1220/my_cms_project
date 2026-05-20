-- =====================================================
-- 为 content 表新增 metadata_manually_saved 字段
-- =====================================================
-- 用途：区分用户手动保存元数据与同步接口自动写入元数据
--       确保前端只在用户点击保存后才打勾
-- 创建日期：2026-05-14
-- 对应迁移：0082_add_metadata_manually_saved_to_content.py
-- 注意：该字段已在 0083 迁移中删除，此脚本仅用于参考
-- =====================================================

-- Upgrade: 添加 metadata_manually_saved 字段
ALTER TABLE content ADD COLUMN metadata_manually_saved BOOLEAN NOT NULL DEFAULT false;

-- 添加字段注释
COMMENT ON COLUMN content.metadata_manually_saved IS '用户是否手动保存过元数据（通过元数据编辑弹框保存，非同步触发）';

-- 验证字段是否添加成功
-- SELECT column_name, data_type, column_default, is_nullable 
-- FROM information_schema.columns 
-- WHERE table_name = 'content' AND column_name = 'metadata_manually_saved';

-- =====================================================
-- Downgrade (回滚脚本，仅在需要恢复时执行)
-- =====================================================
-- ALTER TABLE content DROP COLUMN IF EXISTS metadata_manually_saved;
