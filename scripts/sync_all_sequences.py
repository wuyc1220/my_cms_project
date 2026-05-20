"""
同步所有数据库序列到最大值

用途: 主备切换后,在新主中心执行此脚本,防止ID冲突

使用方法:
    cd backend
    python scripts/sync_all_sequences.py

注意:
    1. 只在主备切换后执行
    2. 确保当前数据库是主中心
    3. 执行前建议备份数据库
"""
import asyncio
from sqlalchemy import text
from app.database import engine


async def sync_all_sequences():
    """同步所有序列到对应表的最大ID值"""
    
    async with engine.begin() as conn:
        print("=" * 60)
        print("开始同步所有数据库序列")
        print("=" * 60)
        
        # 1. 获取所有序列及其对应的表和字段
        result = await conn.execute(text("""
            SELECT 
                s.sequencename,
                t.tablename,
                a.attname as column_name
            FROM pg_sequences s
            JOIN pg_tables t ON s.schemaname = t.schemaname
            JOIN pg_class c ON c.relname = s.sequencename
            JOIN pg_attrdef d ON d.adrelid = c.oid
            JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
            WHERE s.schemaname = 'public'
                AND t.tablename NOT LIKE 'alembic%'
            ORDER BY s.sequencename
        """))
        
        sequences = result.fetchall()
        
        if not sequences:
            print("\n未找到需要同步的序列")
            return
        
        print(f"\n找到 {len(sequences)} 个序列\n")
        
        success_count = 0
        error_count = 0
        
        # 2. 同步每个序列
        for seq_name, table_name, column_name in sequences:
            try:
                # 获取表的最大ID
                max_result = await conn.execute(text(
                    f"SELECT COALESCE(MAX({column_name}), 1) FROM {table_name}"
                ))
                max_id = max_result.scalar()
                
                # 同步序列
                await conn.execute(text(
                    f"SELECT setval('{seq_name}', {max_id})"
                ))
                
                print(f"✓ {seq_name:30s} -> {table_name}.{column_name:20s} (max_id={max_id})")
                success_count += 1
                
            except Exception as e:
                print(f"✗ {seq_name:30s} -> 错误: {e}")
                error_count += 1
        
        # 3. 输出统计
        print("\n" + "=" * 60)
        print(f"同步完成:")
        print(f"  成功: {success_count}")
        print(f"  失败: {error_count}")
        print(f"  总计: {len(sequences)}")
        print("=" * 60)
        
        if error_count > 0:
            print("\n⚠️  有序列同步失败,请检查日志")
        else:
            print("\n✅ 所有序列同步成功!")


if __name__ == "__main__":
    print("\n⚠️  警告: 此脚本会修改数据库序列值")
    print("请确认:")
    print("  1. 当前数据库是主中心")
    print("  2. 已完成主备切换")
    print("  3. 已备份数据库 (可选但推荐)")
    print()
    
    confirm = input("是否继续? (yes/no): ")
    
    if confirm.lower() == 'yes':
        asyncio.run(sync_all_sequences())
    else:
        print("已取消操作")
