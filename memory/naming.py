"""
命名与标识规则（开发文档 §6.18）—— 全系统唯一实现，任何生成 id/文件名处都调用此模块。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from infrastructure.naming import im_attachment_name, pending_id  # noqa: F401 — re-export
from infrastructure.timeutil import now_cst

# md 文件名与 domain 目录中需要替换为下划线的非法字符
_ILLEGAL_FS = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def memory_id(seq: int) -> str:
    """mem_{6位零填充自增}。"""
    return f"mem_{seq:06d}"


def entity_id(name: str, disambiguator: str = "",
              project_id: str | None = None) -> str:
    """ent_{规范化名 + 消歧位 [+ project_id] 后的 SHA1 前 10 位}。

    P4-B：加 disambiguator（如"客户"/"同事"/所属 domain）后 sha1，同名不同实体
    可以共存。老调用无 disambiguator 时行为与之前完全一致（""）。

    M2：project_id 参与哈希 → 同名实体在不同项目自然得到不同 entity_id，
    memory_entities 表 PK 约束天然满足；project_id=None（全局）时与旧行为字节一致。
    """
    norm = normalize_entity_name(name)
    key = f"{norm}|{(disambiguator or '').strip().lower()}" if disambiguator else norm
    if project_id:
        key = f"{key}|{project_id}"
    return "ent_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def normalize_entity_name(name: str) -> str:
    # NFKC 把全角转半角，再去空格转小写
    return unicodedata.normalize("NFKC", name).strip().lower()


# P4-A：domain 别名归并表（常见 LLM 蒸馏同义 slug 收敛到规范 domain）
# 保守选择：只归并明确同义、不覆盖细分领域；未列出的走原样，交后台补翻
_DOMAIN_ALIASES = {
    # 产品相关
    "product": "product",
    "product_design": "product",
    "product_management": "product",
    "product_development": "product",
    "product_strategy": "product",
    "pm": "product",
    "产品": "product",
    "产品设计": "product",
    "产品管理": "product",
    "产品经理": "product",
    # 前端 / Web
    "frontend": "frontend",
    "frontend_architecture": "frontend",
    "frontend_engineering": "frontend",
    "web_development": "frontend",
    "web_frontend": "frontend",
    "前端": "frontend",
    "前端开发": "frontend",
    "前端架构": "frontend",
    # 后端 / 存储
    "backend": "backend",
    "backend_engineering": "backend",
    "server_side": "backend",
    "backend_architecture": "backend",
    "后端": "backend",
    "后端架构": "backend",
    # AI / ML
    "ai": "ai",
    "artificial_intelligence": "ai",
    "machine_learning": "ai",
    "ml": "ai",
    "deep_learning": "ai",
    "llm": "ai",
    "人工智能": "ai",
    "机器学习": "ai",
    # 数据
    "data": "data",
    "data_science": "data",
    "data_engineering": "data",
    "data_analytics": "data",
    "数据科学": "data",
    "数据分析": "data",
    "数据工程": "data",
    # 项目管理
    "project_management": "project_management",
    "pmp": "project_management",
    "项目管理": "project_management",
    # 通用
    "general": "general",
    "misc": "general",
    "其他": "general",
    "通用": "general",
}


def normalize_domain(domain: str) -> str:
    """domain：小写英文或中文，长度 1-32，不含路径分隔符与空格（空格转下划线）。

    写盘前的最后一道防线：LLM 蒸馏可能产出含反斜杠/多段式的脏 domain
    （如"组织行为学 \\ 人力资源管理"），Windows 下 mkdir 会失败并造成
    写请求永久重试，必须规范化后才能拼入路径。

    P4-A：字符清洗完成后再过一次别名归并表，把 product_design/
    product_management 等同义 slug 收敛到 `product` 一族。
    """
    d = unicodedata.normalize("NFKC", str(domain)).strip() if domain else ""
    d = _ILLEGAL_FS.sub("_", d).replace(" ", "_")
    d = re.sub("_+", "_", d).strip("_")  # 压缩连续下划线并去两端
    d = d.lower() if d.isascii() else d
    if not d:
        d = "general"
    d = d[:32]
    # 别名归并（key 大小写敏感对 ASCII；中文原样）
    canonical = _DOMAIN_ALIASES.get(d)
    if canonical:
        return canonical
    return d


def memory_filename(mid: str, title: str, existing: set[str] | None = None) -> str:
    """mem_{id}_{title前20字符}.md，非法字符转下划线，冲突追加 _2/_3。

    P4-G：Emoji/特殊符号一并替换为下划线（Windows/备份工具/云盘同步在带 emoji
    的文件名上偶发失败）；只保留 [\\w一-鿿] 与常见中英文标点。
    """
    # 先按原规则去 FS 非法字符
    stripped = _ILLEGAL_FS.sub("_", title or "")
    # 再挑白名单字符：字母/数字/下划线 + 汉字 + 若干安全标点
    cleaned = "".join(
        ch if (ch.isalnum() or ch == "_" or "一" <= ch <= "鿿"
               or ch in ".。,，-_ ")
        else "_"
        for ch in stripped)
    cleaned = re.sub("_+", "_", cleaned).strip("_ .")
    safe_title = cleaned[:20].strip() or "untitled"
    base = f"{mid}_{safe_title}"
    name = f"{base}.md"
    if existing:
        n = 2
        while name in existing:
            name = f"{base}_{n}.md"
            n += 1
    return name


def _ts() -> str:
    return now_cst().strftime("%Y%m%d_%H%M%S")


def backup_filename(label: str | None = None) -> str:
    suffix = f"_{_ILLEGAL_FS.sub('_', label)}" if label else ""
    return f"sp_backup_{_ts()}{suffix}.zip"


def suggestion_id() -> str:
    return f"sug_{uuid.uuid4().hex[:8]}"


def task_id(task_type: str) -> str:
    return f"{task_type}_{_ts()}_{uuid.uuid4().hex[:8]}"


def provider_id(seq: int) -> str:
    return f"prov_{seq:03d}"


def raw_doc_id(seq: int) -> str:
    return f"doc_{seq:04d}"


def session_id(seq: int) -> str:
    return f"sess_{seq:04d}"


def project_id() -> str:
    """proj_{8 位 hex}。项目 ID 无自增序列，直接 uuid 截断。"""
    return f"proj_{uuid.uuid4().hex[:8]}"
