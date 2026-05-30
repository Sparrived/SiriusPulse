"""修复重复的 token 使用记录。

问题：某些任务（diary_generate, biography_distill, biography_update, 
cognition_analyze, diary_consolidate）的 token 记录被重复写入：
- 一条有 breakdown_json 但 prompt_tokens=0
- 另一条有正确的 prompt_tokens 但 breakdown_json 为空

修复逻辑：
1. 找到同一时间戳（±1秒）、同一 task_name 的重复记录
2. 合并：保留有正确 token 数量的记录，补充 breakdown_json
3. 删除多余的记录
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


def find_token_db() -> Path | None:
    """查找 token_usage.db 文件。"""
    # 常见位置
    candidates = [
        Path.home() / ".sirius_pulse" / "data" / "token_usage.db",
        Path.home() / ".sirius" / "data" / "token_usage.db",
        Path("data/token_usage.db"),
    ]
    
    for p in candidates:
        if p.exists():
            return p
    
    # 递归查找
    for p in Path.home().rglob("token_usage.db"):
        return p
    
    return None


def find_duplicate_pairs(conn: sqlite3.Connection) -> list[tuple[dict, dict]]:
    """查找重复的记录对。"""
    # 查找所有可能重复的记录（同一 task_name，时间戳相差 1 秒内）
    query = """
    SELECT 
        a.id as id_a, a.timestamp as ts_a, a.task_name, a.model,
        a.prompt_tokens as pt_a, a.completion_tokens as ct_a, 
        a.total_tokens as tt_a, a.breakdown_json as bd_a,
        b.id as id_b, b.timestamp as ts_b,
        b.prompt_tokens as pt_b, b.completion_tokens as ct_b,
        b.total_tokens as tt_b, b.breakdown_json as bd_b
    FROM token_usage a
    JOIN token_usage b ON 
        a.task_name = b.task_name 
        AND a.model = b.model
        AND ABS(a.timestamp - b.timestamp) < 1.0
        AND a.id < b.id
    WHERE 
        (a.prompt_tokens = 0 AND a.breakdown_json != '' AND b.prompt_tokens > 0 AND b.breakdown_json = '')
        OR
        (b.prompt_tokens = 0 AND b.breakdown_json != '' AND a.prompt_tokens > 0 AND a.breakdown_json = '')
    """
    
    rows = conn.execute(query).fetchall()
    
    pairs = []
    for row in rows:
        record_a = {
            "id": row[0],
            "timestamp": row[1],
            "task_name": row[2],
            "model": row[3],
            "prompt_tokens": row[4],
            "completion_tokens": row[5],
            "total_tokens": row[6],
            "breakdown_json": row[7],
        }
        record_b = {
            "id": row[8],
            "timestamp": row[9],
            "task_name": row[2],  # 同一 task_name
            "model": row[3],      # 同一 model
            "prompt_tokens": row[10],
            "completion_tokens": row[11],
            "total_tokens": row[12],
            "breakdown_json": row[13],
        }
        pairs.append((record_a, record_b))
    
    return pairs


def merge_records(pair: tuple[dict, dict]) -> tuple[dict, int]:
    """合并重复记录对，返回 (更新后的记录, 要删除的记录ID)。"""
    a, b = pair
    
    # 确定哪条记录有正确的 token 数量
    if a["prompt_tokens"] > 0 and b["prompt_tokens"] == 0:
        # a 有正确的 token，b 有 breakdown
        keep = a
        remove = b
    elif b["prompt_tokens"] > 0 and a["prompt_tokens"] == 0:
        # b 有正确的 token，a 有 breakdown
        keep = b
        remove = a
    else:
        # 无法确定，跳过
        return None, None
    
    # 如果 keep 已经有 breakdown，不需要合并
    if keep["breakdown_json"]:
        return keep, remove["id"]
    
    # 合并 breakdown_json
    keep["breakdown_json"] = remove["breakdown_json"]
    
    return keep, remove["id"]


def fix_duplicates(db_path: Path, dry_run: bool = True) -> None:
    """修复重复记录。"""
    print(f"数据库路径: {db_path}")
    print(f"模式: {'预览' if dry_run else '执行修复'}")
    print()
    
    conn = sqlite3.connect(str(db_path))
    
    # 查找重复记录
    pairs = find_duplicate_pairs(conn)
    
    if not pairs:
        print("未发现重复记录。")
        return
    
    print(f"发现 {len(pairs)} 组重复记录：")
    print()
    
    updates = []
    delete_ids = []
    
    for i, pair in enumerate(pairs, 1):
        merged, remove_id = merge_records(pair)
        if merged is None:
            continue
        
        a, b = pair
        print(f"[{i}] task={a['task_name']}, model={a['model']}")
        print(f"    记录A (id={a['id']}): pt={a['prompt_tokens']}, ct={a['completion_tokens']}, bd={'有' if a['breakdown_json'] else '无'}")
        print(f"    记录B (id={b['id']}): pt={b['prompt_tokens']}, ct={b['completion_tokens']}, bd={'有' if b['breakdown_json'] else '无'}")
        print(f"    操作: 保留 id={merged['id']}, 删除 id={remove_id}")
        
        # 解析 breakdown 显示
        if merged["breakdown_json"]:
            try:
                bd = json.loads(merged["breakdown_json"])
                print(f"    breakdown: total={bd.get('total', 0)}, "
                      f"user_message={bd.get('user_message', 0)}, "
                      f"output={bd.get('output_total', 0)}")
            except json.JSONDecodeError:
                pass
        print()
        
        updates.append((
            merged["breakdown_json"],
            merged["id"]
        ))
        delete_ids.append(remove_id)
    
    if not updates:
        print("无需修复。")
        return
    
    print(f"总计: {len(updates)} 条记录需要更新, {len(delete_ids)} 条记录需要删除")
    
    if dry_run:
        print("\n[预览模式] 如需执行修复，请添加 --execute 参数")
        return
    
    # 执行修复
    print("\n执行修复...")
    
    # 更新记录：添加 breakdown_json
    for breakdown_json, record_id in updates:
        conn.execute(
            "UPDATE token_usage SET breakdown_json = ? WHERE id = ?",
            (breakdown_json, record_id)
        )
    
    # 删除多余的记录
    placeholders = ",".join(["?"] * len(delete_ids))
    conn.execute(f"DELETE FROM token_usage WHERE id IN ({placeholders})", delete_ids)
    
    conn.commit()
    print(f"修复完成！已更新 {len(updates)} 条记录，删除 {len(delete_ids)} 条重复记录。")
    
    conn.close()


def main():
    """主函数。"""
    import argparse
    
    parser = argparse.ArgumentParser(description="修复重复的 token 使用记录")
    parser.add_argument("--db", type=str, help="指定数据库文件路径")
    parser.add_argument("--execute", action="store_true", help="执行修复（默认仅预览）")
    parser.add_argument("--search", type=str, help="在指定目录下搜索数据库文件")
    
    args = parser.parse_args()
    
    db_path = None
    
    if args.db:
        db_path = Path(args.db)
        if not db_path.exists():
            print(f"错误: 数据库文件不存在: {db_path}")
            sys.exit(1)
    elif args.search:
        search_dir = Path(args.search)
        if not search_dir.exists():
            print(f"错误: 搜索目录不存在: {search_dir}")
            sys.exit(1)
        for p in search_dir.rglob("token_usage.db"):
            db_path = p
            break
        if db_path is None:
            print(f"错误: 在 {search_dir} 下未找到 token_usage.db")
            sys.exit(1)
    else:
        db_path = find_token_db()
        if db_path is None:
            print("错误: 未找到 token_usage.db，请使用 --db 或 --search 参数指定")
            sys.exit(1)
    
    fix_duplicates(db_path, dry_run=not args.execute)


if __name__ == "__main__":
    main()
