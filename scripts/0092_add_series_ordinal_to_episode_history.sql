-- =====================================================
-- 为 episode_history 表添加 series_ordinal 字段
-- 描述: 支持 SERIES 类型的历史记录
-- 创建日期: 2026-05-20
-- =====================================================

-- 添加 series_ordinal 字段
ALTER TABLE episode_history 
ADD COLUMN IF NOT EXISTS series_ordinal INTEGER;

-- 添加 content_type 索引（如果不存在）
CREATE INDEX IF NOT EXISTS ix_episode_history_content_type 
ON episode_history (content_type);

-- 添加字段注释
COMMENT ON COLUMN episode_history.series_ordinal IS 'Series序号(仅SERIES类型使用)';
COMMENT ON COLUMN episode_history.content_type IS '内容类型(EPISODE/SERIES等)';
