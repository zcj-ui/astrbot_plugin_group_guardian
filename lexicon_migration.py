# -*- coding: utf-8 -*-
"""可追溯的辱骂词库扩展迁移。

这个模块只负责生成和写入 ``moderation_rules`` 中的 ``swear`` 规则，
不参与消息解析。规则被设计为字面量（或明确的正则）以便兼容
``HybridMatcher``。生成器只保留明确的核心短语，并在字符间隙组合安全
分隔符；不会自动替换同音字，也不会把任意单字加入词库。

``ensure_swear_expansion`` 可在插件启动时调用，使用 ``meta`` 中的版本
键保证已有运行库只迁移一次；脚本入口则用于更新发布包中的 seed
``lexicon.db``，支持 ``--dry-run`` 和完整性校验。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple


# v3 是对 v2 seed 的清理补丁：移除旧的无边界聚合正则，并补充边界英文词。
# 插件对外版本号与此内部词库迁移版本独立。
EXPANSION_VERSION = "swear-expansion-v3-20260723"
MIN_NEW_RULES = 20_000
TARGET_NEW_RULES = 24_000
DESCRIPTION_PREFIX = f"auto:{EXPANSION_VERSION}"
LEGACY_DESCRIPTION_PREFIXES: Tuple[str, ...] = (
    "auto:swear-expansion-v1-20260723",
)

# 这些短语本身具有明确的辱骂语义。生成器只插入安全分隔符，不做
# 同音字替换，也不从中性单字（如“妈”“草”“滚”）扩展，减少误报。
CORE_PHRASES = """
傻逼|傻比|傻屄|傻子|煞笔|煞逼|沙比|傻叉|傻吊|傻鸟|蠢货|蠢蛋|蠢材|蠢东西|蠢猪|蠢驴|蠢狗|笨蛋|笨猪|笨狗|笨鸟|脑残|脑瘫|智障|弱智|白痴|二货|废物|废柴|废狗|废材|废人|废渣|垃圾货|垃圾人|垃圾玩意|辣鸡|乐色|人渣|败类|败类玩意|杂种|杂碎|畜生|畜牲|禽兽|牲口|狗东西|狗屎|狗日的|狗杂种|狗娘养的|狗娘们|狗崽子|龟孙|王八蛋|王八羔子|贱人|贱货|贱种|贱骨头|骚货|骚娘们|骚婊子|婊子|婊砸|婊里婊气|荡妇|破鞋|破烂货|野鸡|死娘们|臭娘们|臭婊子|臭傻逼|臭煞笔|臭垃圾|滚蛋|滚犊子|滚粗|滚一边去|滚远点|滚开|滚回去|去死|死全家|死妈|死爹|死娘|妈的|妈逼|你妈逼|操你妈|草泥马|草你妈|草拟吗|艹尼玛|肏你妈|你妈死了|你娘死了|cnm|cao ni ma|nmsl|nmslese|wdnmd|wcnm|shabi|sha bi|鸡巴|鸡掰|鸡儿|臭鸡巴|鸡婆|鸡掰人|鸡巴毛|屌毛|妈了个逼|他妈的|他妈逼|你他妈|你妈的|你娘的|娘希匹|小瘪三|瘪三|小赤佬|赤佬|扑街|仆街|死扑街|仆街仔|契弟|戆鸠|戆卵|西八|西吧|阿西吧|他奶奶的|奶奶个熊|妈个鸡|草了个蛋|草蛋|脑子有病|脑子进水|缺心眼|缺心眼子|没脑子|不要脸|厚脸皮|不要碧莲|无耻之徒|无耻|无赖|卑鄙|下三滥|下流|变态|恶心货|臭不要脸|恶臭|贱兮兮|骚浪贱|浪货|绿茶婊|心机婊|白莲婊|茶婊|烂人|烂货|狗屁不通|狗屁|放你妈的屁|滚你妈的|滚你大爷|去你妈的|去你大爷|去你妹|去你姥姥|去你奶奶|你算个屁|你配个屁|你也配|你算老几|你个废物|你个垃圾|你这个傻逼|你这傻逼|真是个废物|真他妈的|操蛋|操你大爷|操你祖宗|操你姥姥|日你妈|日你娘|日了狗|日狗|干你妈|干你娘|干死你|干死全家|干你屁事|艹你大爷|艹你妈的|肏你祖宗|肏你全家|强奸你妈|轮奸|卖淫|嫖娼|婊子养的|操你妹|草你妹|草你姥姥|草你祖宗|尼玛逼|尼玛的|尼玛个逼|尼玛币|泥马|泥马币|泥马戈壁|马勒戈壁|马勒戈壁的|麻痹|麻痹的|麻痹玩意|麻辣隔壁|妈了巴子|妈拉个巴子|妈卖批|妈卖皮|妈的批|批你妈|碧池|碧莲|狗日|狗娘|没卵用|没种|孬种|怂货|怂包|窝囊废|孬货|脓包|脏东西|脏货|臭东西|恶心人|不要脸的|下作|缺德|没教养|没素质|没品|蠢货玩意|笨蛋玩意|死蠢|神经病|疯狗|疯婆子|臭男人|臭女人|老不死|老畜生|死变态|色鬼|色狼|淫棍|淫虫|流氓|地痞|骗子狗|坑人货|坑货|坑爹|坑逼|坑比|败家玩意
"""

# 仅使用不含正则元字符的分隔符，生成值可以直接进入 AC。
SAFE_SEPARATORS: Tuple[str, ...] = ("", " ", "_", "-", "/", "~", "·", "、", "，")
MAX_SEPARATOR_VARIANTS_PER_CORE = 256

# 这些短 token 需要 ASCII 边界而非 AC 子串，否则会误伤正常英文/课程名。
# 每个字母之间允许常见分隔符，兼容 ``c/n/m``、``n m s l`` 等规避写法。
# 这些词只在 ASCII 边界内匹配。``gun``/``five``/``fw`` 等旧版同音
# 替换不再保留，它们会误伤正常英文或技术文本。
SHORT_ASCII_TOKENS: Tuple[str, ...] = (
    "cs", "cnm", "cao", "nmsl", "wdnmd", "wcnm", "shabi", "sb",
    "fuck", "bitch", "shit", "damn", "asshole",
)
_SHORT_TOKEN_SEPARATOR = r"[\s._~·、，／/-]*"


def _short_token_regex(token: str) -> str:
    body = _SHORT_TOKEN_SEPARATOR.join(re.escape(char) for char in token)
    return rf"(?<![a-z0-9]){body}(?![a-z0-9])"


SHORT_TOKEN_REGEXES: Tuple[str, ...] = tuple(_short_token_regex(token) for token in SHORT_ASCII_TOKENS)
LOW_CONFIDENCE_LITERALS: Tuple[str, ...] = ("啥子",)
LEGACY_SEED_SHORT_TOKEN_PATTERN = "(?:cnm|cao|nmsl|草泥马|艹尼玛|草拟吗|你妈死了|nmslese)"
# v2.7.2 及更早 seed 中的聚合正则。它们把同音替换（例如 gun=f滚、
# five=废、sb=傻逼）当作无边界子串，导致 USB/Gundam/shitake 等误报。
# 只按完整 pattern + 空描述删除，带描述的用户自定义同形规则保留。
LEGACY_SEED_AGGREGATE_PATTERNS: Tuple[str, ...] = (
    "妈[的得]",
    "(?:你|他|她|它|尼|伱)[妈馬嗎玛]",
    "(?:傻|蠢|笨|白痴|弱智|脑残|智障|sb|s\\W*b|煞笔|傻逼|沙比|煞逼|二货)",
    "(?:操|草|艹|肏|日|干)(?:你|他|她|泥|拟|尼|呢|ma|吗|嘛)",
    "(?:fuck|f[u*]ck|bitch|shit|damn|asshole)",
    "(?:贱|骚|浪|婊|妓|鸡|鸭)(?:人|货|子|逼|B|b)",
    "(?:去死|滚蛋|滚粗|gun|死全家|死妈|死爹)",
    "(?:婊子|贱人|骚货|烂货|荡妇|破鞋|野鸡)",
    "(?:狗日的|狗东西|狗屎|垃圾|辣鸡|乐色)",
    "(?:废物|废柴|fw|five|拉胯)",
    "(?:杂种|杂碎|畜生|禽兽|狗娘养的|龟孙)",
    "(?:你算老几|你配吗|你个.{0,5}东西)",
)


def _unique(values: Iterable[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for value in values:
        value = str(value).strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _separator_variants(value: str, limit: int = MAX_SEPARATOR_VARIANTS_PER_CORE) -> List[str]:
    """在每个字符间隙独立选择安全分隔符，稳定生成有限规避写法。"""
    if len(value) < 2:
        return [value]
    result: List[str] = []
    combinations = len(SAFE_SEPARATORS) ** (len(value) - 1)
    sample_count = min(max(1, int(limit)), combinations)
    if sample_count == combinations:
        sample_indexes = range(combinations)
    else:
        # 均匀覆盖完整组合空间，避免上限较小时只有短语前几个间隙变化。
        sample_indexes = ((index * combinations) // sample_count for index in range(sample_count))
    for combination_index in sample_indexes:
        cursor = combination_index
        pieces: List[str] = []
        for index, char in enumerate(value):
            pieces.append(char)
            if index < len(value) - 1:
                pieces.append(SAFE_SEPARATORS[cursor % len(SAFE_SEPARATORS)])
                cursor //= len(SAFE_SEPARATORS)
        result.append("".join(pieces))
    return _unique(result)


def _core_phrases() -> List[str]:
    # 短 ASCII token 必须走带边界的正则，不能由这里生成无边界字面量。
    return [
        phrase
        for phrase in _unique(CORE_PHRASES.replace("\n", "").split("|"))
        if phrase.lower() not in SHORT_ASCII_TOKENS
    ]


def generate_swear_rules(target: int = TARGET_NEW_RULES) -> List[Tuple[str, str]]:
    """生成 ``(pattern, description)``，结果稳定且不含重复。

    生成顺序固定，超过目标时按词根轮询截取，避免前几个词根独占词库。
    """
    per_core: Dict[str, List[Tuple[str, str]]] = {}
    for base in _core_phrases():
        candidates: List[Tuple[str, str]] = []
        for pattern in _separator_variants(base):
            # 生成值仅含安全字面量；若未来词表加入元字符，直接跳过并
            # 让维护者显式添加转义正则，避免 HybridMatcher 误解析。
            if re.search(r"[.^$*+?{}\[\]|\\]", pattern):
                continue
            candidates.append((pattern, f"{DESCRIPTION_PREFIX}|core={base}"))
        per_core[base] = _unique_pairs(candidates)

    selected: List[Tuple[str, str]] = []
    seen = set()
    cores = list(per_core)
    index = 0
    while cores and len(selected) < target:
        base = cores[index % len(cores)]
        bucket = per_core[base]
        if bucket:
            pattern, description = bucket.pop(0)
            if pattern not in seen:
                seen.add(pattern)
                selected.append((pattern, description))
        if not bucket:
            cores.remove(base)
            index = 0
        else:
            index += 1
    if len(selected) < target:
        raise RuntimeError(f"辱骂变体不足 {target} 条，仅生成 {len(selected)} 条")
    return selected


def _unique_pairs(values: Iterable[Tuple[str, str]]) -> List[Tuple[str, str]]:
    seen = set()
    result: List[Tuple[str, str]] = []
    for pattern, description in values:
        if pattern not in seen:
            seen.add(pattern)
            result.append((pattern, description))
    return result


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS moderation_rules ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "category TEXT NOT NULL, pattern TEXT NOT NULL, "
        "enabled INTEGER NOT NULL DEFAULT 1, description TEXT, "
        "UNIQUE(category, pattern))"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")


def ensure_swear_expansion(conn: sqlite3.Connection, target: int = TARGET_NEW_RULES) -> Dict[str, object]:
    """将扩展幂等写入现有连接，返回迁移统计。

    ``conn`` 可以是 Storage 的普通 sqlite3 连接；函数使用 SAVEPOINT，
    不会要求调用方处于特定事务状态。
    """
    if int(target) < MIN_NEW_RULES:
        raise ValueError(f"辱骂扩展目标不能小于 {MIN_NEW_RULES}")
    _ensure_schema(conn)
    version_row = conn.execute("SELECT value FROM meta WHERE key='swear_expansion_version'").fetchone()
    if version_row and str(version_row[0]) == EXPANSION_VERSION:
        total = int(conn.execute("SELECT COUNT(*) FROM moderation_rules WHERE category='swear'").fetchone()[0])
        return {
            "version": EXPANSION_VERSION,
            "already_applied": True,
            "generated": 0,
            "inserted": 0,
            "removed_legacy_rules": 0,
            "updated_descriptions": 0,
            "total_swear_rules": total,
        }
    generated = generate_swear_rules(target)
    generated.extend(
        (pattern, f"{DESCRIPTION_PREFIX}|regex=short-token:{token}")
        for token, pattern in zip(SHORT_ASCII_TOKENS, SHORT_TOKEN_REGEXES)
    )
    generated.extend((token, f"{DESCRIPTION_PREFIX}|low-confidence={token}") for token in LOW_CONFIDENCE_LITERALS)
    generated = _unique_pairs(generated)

    savepoint = "swear_expansion"
    inserted = 0
    updated = 0
    removed_legacy = 0
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        # v1 的全字符同音替换会生成“白吃/四妈/麻的”等中性词。只删除
        # 带明确 v1 自动来源标记的行，用户自定义描述规则一律保留。
        for prefix in LEGACY_DESCRIPTION_PREFIXES:
            cursor = conn.execute(
                "DELETE FROM moderation_rules WHERE category='swear' AND description LIKE ?",
                (f"{prefix}%",),
            )
            removed_legacy += max(0, int(cursor.rowcount or 0))
        # 旧 seed 的聚合规则把 cao/cnm/nmsl 当无边界子串，会误伤 Macao、
        # cacao 等正常英文。它是可精确识别的内置规则；有自定义描述的同形
        # 用户规则仍保留。对应辱骂短语已由 v2 边界正则和核心词覆盖。
        cursor = conn.execute(
            "DELETE FROM moderation_rules WHERE category='swear' AND pattern=? "
            "AND (description IS NULL OR description='')",
            (LEGACY_SEED_SHORT_TOKEN_PATTERN,),
        )
        removed_legacy += max(0, int(cursor.rowcount or 0))
        for pattern in LEGACY_SEED_AGGREGATE_PATTERNS:
            cursor = conn.execute(
                "DELETE FROM moderation_rules WHERE category='swear' AND pattern=? "
                "AND (description IS NULL OR description='')",
                (pattern,),
            )
            removed_legacy += max(0, int(cursor.rowcount or 0))

        existing = {
            str(row[0]): (str(row[1]) if row[1] is not None else "")
            for row in conn.execute(
                "SELECT pattern, description FROM moderation_rules WHERE category='swear'"
            ).fetchall()
        }
        pending = [
            ("swear", pattern, 1, description)
            for pattern, description in generated
            if pattern not in existing
        ]
        # 新安装时 _ensure_seed_rules 只复制 pattern（兼容旧版 seed），因此
        # 对由本迁移生成但 description 为空的已有行补回来源；用户自定义描述
        # 则保留不覆盖。
        updates = [
            (description, pattern)
            for pattern, description in generated
            if pattern in existing and not existing[pattern]
        ]
        before_count = int(
            conn.execute("SELECT COUNT(*) FROM moderation_rules WHERE category='swear'").fetchone()[0]
        )
        if pending:
            conn.executemany(
                "INSERT OR IGNORE INTO moderation_rules(category, pattern, enabled, description) VALUES(?, ?, ?, ?)",
                pending,
            )
            after_count = int(conn.execute("SELECT COUNT(*) FROM moderation_rules WHERE category='swear'").fetchone()[0])
            inserted = max(0, after_count - before_count)
        if updates:
            descriptions_before = int(
                conn.execute(
                    "SELECT COUNT(*) FROM moderation_rules WHERE category='swear' AND description LIKE ?",
                    (f"{DESCRIPTION_PREFIX}%",),
                ).fetchone()[0]
            )
            conn.executemany(
                "UPDATE moderation_rules SET description=? WHERE category='swear' AND pattern=? AND (description IS NULL OR description='')",
                updates,
            )
            descriptions_after = int(
                conn.execute(
                    "SELECT COUNT(*) FROM moderation_rules WHERE category='swear' AND description LIKE ?",
                    (f"{DESCRIPTION_PREFIX}%",),
                ).fetchone()[0]
            )
            updated = max(0, descriptions_after - descriptions_before)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('swear_expansion_version', ?)",
            (EXPANSION_VERSION,),
        )
    except Exception:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        raise
    else:
        conn.execute(f"RELEASE {savepoint}")

    total = int(conn.execute("SELECT COUNT(*) FROM moderation_rules WHERE category='swear'").fetchone()[0])
    return {
        "version": EXPANSION_VERSION,
        "already_applied": bool(version_row and str(version_row[0]) == EXPANSION_VERSION),
        "generated": len(generated),
        "inserted": inserted,
        "removed_legacy_rules": removed_legacy,
        "updated_descriptions": updated,
        "total_swear_rules": total,
    }


def _database_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="扩展 seed lexicon.db 的 swear 规则")
    parser.add_argument("--db", type=Path, default=Path(__file__).with_name("lexicon.db"))
    parser.add_argument("--target", type=int, default=TARGET_NEW_RULES)
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写入数据库")
    args = parser.parse_args(argv)
    if args.target < MIN_NEW_RULES:
        parser.error(f"--target 不能小于 {MIN_NEW_RULES}")
    generated = generate_swear_rules(args.target)
    generated.extend(
        (pattern, f"{DESCRIPTION_PREFIX}|regex=short-token:{token}")
        for token, pattern in zip(SHORT_ASCII_TOKENS, SHORT_TOKEN_REGEXES)
    )
    generated.extend((token, f"{DESCRIPTION_PREFIX}|low-confidence={token}") for token in LOW_CONFIDENCE_LITERALS)
    generated = _unique_pairs(generated)
    if args.dry_run:
        print(json.dumps({"db": str(args.db), "generated": len(generated), "target": args.target}, ensure_ascii=False))
        return 0

    conn = sqlite3.connect(str(args.db), timeout=30.0)
    try:
        stats = ensure_swear_expansion(conn, args.target)
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity.lower() != "ok":
            raise RuntimeError(f"SQLite integrity_check 失败: {integrity}")
        # seed 数据库随插件发布，不应留下未合并的 WAL 临时文件。
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    stats["integrity_check"] = integrity
    stats["sha256"] = _database_sha256(args.db)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
