-- =====================================================
-- 新增 episode_history 表
-- 描述: 记录单集（EPISODE）的增删操作历史
-- 创建日期: 2026-05-20
-- =====================================================

-- 创建 episode_history 表
CREATE TABLE IF NOT EXISTS episode_history (
    id SERIAL NOT NULL,
    parent_id INTEGER NOT NULL,
    content_id INTEGER NOT NULL,
    content_name VARCHAR(500) NOT NULL,
    content_type VARCHAR(50) DEFAULT 'EPISODE' NOT NULL,
    series_ordinal INTEGER,
    processed_by VARCHAR(100),
    processed_type VARCHAR(20) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    is_deleted BOOLEAN DEFAULT FALSE NOT NULL,
    created_by INTEGER,
    updated_by INTEGER,
    CONSTRAINT pk_episode_history PRIMARY KEY (id),
    CONSTRAINT fk_episode_history_parent FOREIGN KEY(parent_id) REFERENCES content(id) ON DELETE CASCADE,
    CONSTRAINT fk_episode_history_created_by FOREIGN KEY(created_by) REFERENCES cms_user(id) ON DELETE SET NULL,
    CONSTRAINT fk_episode_history_updated_by FOREIGN KEY(updated_by) REFERENCES cms_user(id) ON DELETE SET NULL
);

-- 创建索引
CREATE INDEX IF NOT EXISTS ix_episode_history_parent_id ON episode_history (parent_id);
CREATE INDEX IF NOT EXISTS ix_episode_history_content_id ON episode_history (content_id);
CREATE INDEX IF NOT EXISTS ix_episode_history_content_type ON episode_history (content_type);
CREATE INDEX IF NOT EXISTS ix_episode_history_created_at ON episode_history (created_at);
CREATE INDEX IF NOT EXISTS ix_episode_history_is_deleted ON episode_history (is_deleted);

-- 添加表注释
COMMENT ON TABLE episode_history IS '内容增删操作历史记录表(支持EPISODE/SERIES等多种类型)';

-- 添加字段注释
COMMENT ON COLUMN episode_history.id IS '主键ID';
COMMENT ON COLUMN episode_history.parent_id IS '父级内bb容ID(关联content表，可以是SERIES或SEASON)';
COMMENT ON COLUMN episode_history.content_id IS '子内容ID(EPISODE或SERIES)';
COMMENT ON COLUMN episode_history.content_name IS '内容名称';
COMMENT ON COLUMN episode_history.content_type IS '内容类型(EPISODE/SERIES等)';
COMMENT ON COLUMN episode_history.series_ordinal IS 'Series序号(仅SERIES类型使用)';
COMMENT ON COLUMN episode_history.processed_by IS '处理人';
COMMENT ON COLUMN episode_history.processed_type IS '处理类型(ADD/DELETE等)';
COMMENT ON COLUMN episode_history.created_at IS '创建时间';
COMMENT ON COLUMN episode_history.updated_at IS '更新时间';
COMMENT ON COLUMN episode_history.is_deleted IS '软删除标记';
COMMENT ON COLUMN episode_history.created_by IS '创建人ID';
COMMENT ON COLUMN episode_history.updated_by IS '更新人ID';
