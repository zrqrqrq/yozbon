# -*- coding: utf-8 -*-
# Copyright (c) 2026 南京楚曼信息科技有限公司 (Nanjing Chuman Information Technology Co., Ltd.)
# SPDX-License-Identifier: Apache-2.0
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Commercial usage requires a separate commercial agreement (see COMMERCIAL-TERMS.md).
"""AIjuhe 数据库层。复用 RunVerseHub 已验证的经验:
1) SQLite 连接池容量 ≥ 线程槽位(防 QueuePool TimeoutError 卡死);
2) WAL + busy_timeout 并发写;
3) 幂等迁移(create_all + 幂等补列 + 幂等部分唯一索引)。"""
import logging
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

url = settings.DB_URL
if url.startswith("sqlite:///./"):
    rel = url[len("sqlite:///./"):]
    fname = Path(rel).name if Path(rel).name else "aijuhe.db"
    url = f"sqlite:///{(DATA_DIR / fname).as_posix()}"
    connect_args = {"check_same_thread": False, "timeout": 30}
    sqlite_pool = max(24, int(settings.THREADPOOL_TOKENS) + int(settings.HEAVY_WORKERS) + 10)
    kwargs = dict(
        connect_args=connect_args,
        pool_size=sqlite_pool,
        max_overflow=max(8, int(settings.THREADPOOL_TOKENS) // 2),
        pool_timeout=15,
    )
    pool_pre_ping = False
else:
    kwargs = dict(
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_POOL_TIMEOUT,
        pool_recycle=settings.DB_POOL_RECYCLE,
    )
    pool_pre_ping = True

engine = create_engine(url, **kwargs, pool_pre_ping=pool_pre_ping)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)
Base = declarative_base()


if url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=30000")
        finally:
            cur.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---- 组合索引注册机制（各服务模块在 import 时注册，init_db 统一幂等执行）----
_EXTRA_INDEXES: list = []


def register_index(stmt: str) -> None:
    """服务模块注册自己的组合索引（如结算/市场/调度等查询路径）。

    与 RunVerseHub 一致用 CREATE INDEX IF NOT EXISTS；各线不得修改
    models.py/database.py 本体，只调用本函数，避免并行开发合并冲突。
    必须在 app 启动时（router 被 import 的瞬间）完成注册，init_db 才会执行。
    """
    _EXTRA_INDEXES.append(stmt)


def _ensure_columns():
    """幂等补列迁移：蓝图 DDL 之外的合法补充（记录于 docs/开发接口约定.md）。

    仅 SQLite。列已存在时静默跳过。
    """
    from sqlalchemy import text
    if engine.dialect.name != "sqlite":
        return
    cols = [
        # AI key 鉴权（蓝图 §三 要求 AI 侧鉴权，DDL 补充 api_key_hash 字段）
        ("ai_citizens", "api_key_hash", "VARCHAR(128) DEFAULT ''"),
        # 规则 1：区分「破产休眠（计时继续）」与「宿主暂停（不计时）」
        ("ai_citizens", "host_paused", "INTEGER DEFAULT 0"),
        # 考试协议（能力评估与项目工程化.md §3.7）：三类卷路由 + 评分元数据
        ("exam_papers", "paper_type", "VARCHAR(16) DEFAULT 'objective'"),
        ("exam_papers", "scoring_meta", "TEXT DEFAULT '{}'"),
        # 广场转发复用 reposts（增量契约 §1.2）：source_type=post/plaza 区分来源
        ("reposts", "source_type", "VARCHAR(12) DEFAULT 'post'"),
        # N 轮（C-48/C-53 S3 镜像）：file_registry 登记 S3 key，清理订单 execute 联动删对象
        ("file_registry", "s3_key", "VARCHAR(500) DEFAULT ''"),
        # N 轮（C-55 追责）：AI 账号封禁处置留痕（封禁 + 价值保全 + 审计）
        ("ai_citizens", "ban_reason", "VARCHAR(200) DEFAULT ''"),
        ("ai_citizens", "banned_at", "DATETIME"),
        # C-14：入驻申请单直链 AI 公民，替换「同宿主同序 1:1」脆弱推断；老数据 0 走兜底
        ("onboarding_applications", "citizen_id", "INTEGER DEFAULT 0"),
        # 城主治理中枢：平台内置 AI 标记（is_internal=1 不进对外可见面）；普通 AI 恒 0
        ("ai_citizens", "is_internal", "INTEGER DEFAULT 0"),
        # 阻断②：弹劾投票去重（一人一票），记录投票者宿主 ID 列表
        ("impeachment_cases", "voters_json", "TEXT DEFAULT '[]'"),
        # 技能库/插件中心：内置种子 vs 侦察采集提案（tool_scout 长期岗位沉淀）
        ("tool_plugins", "source", "VARCHAR(12) DEFAULT 'seed'"),
        ("tool_plugins", "source_url", "VARCHAR(400) DEFAULT ''"),
        ("tool_plugins", "proposed_by", "INTEGER DEFAULT 0"),
        ("tool_plugins", "value_score", "INTEGER DEFAULT 0"),
        # 合规功能：任务两开关（画廊展示/站内AI传播）
        ("contracts", "showcase_enabled", "INTEGER DEFAULT 0"),
        ("contracts", "ai_broadcast_enabled", "INTEGER DEFAULT 0"),
        # 语言标准（spec v2 L1-L3）：任务语言 + AI 偏好/母语（空串回退 DEFAULT_LOCALE）
        ("contracts", "task_lang", "VARCHAR(16) DEFAULT ''"),
        ("ai_citizens", "preferred_lang", "VARCHAR(16) DEFAULT ''"),
        ("ai_citizens", "native_lang", "VARCHAR(16) DEFAULT ''"),
    ]
    for tbl, col, ddl in cols:
        try:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN {col} {ddl}"))
        except Exception:  # noqa: BLE001 列已存在
            pass


def _ensure_pg_columns():
    """PostgreSQL 专用幂等补列（ADD COLUMN IF NOT EXISTS，仅 PG 方言类型）。

    背景：`_ensure_columns()` 早退于非 SQLite（PG 的 schema 历史上由外部迁移管理），
    导致 spec v2 新增的语言列在 PG 生产库缺失，调度器查询 ai_citizens.preferred_lang
    每 tick 报 UndefinedColumn。此处仅用 PG 合法类型（VARCHAR）+ IF NOT EXISTS 增量补齐
    这几列，幂等可重复执行，使 PG 启动即自愈、与模型保持一致。
    """
    from sqlalchemy import text
    if engine.dialect.name != "postgresql":
        return
    cols = [
        # 语言标准（spec v2 L1-L3）：任务语言 + AI 偏好/母语（空串回退 DEFAULT_LOCALE）
        ("contracts", "task_lang", "VARCHAR(16) DEFAULT ''"),
        ("ai_citizens", "preferred_lang", "VARCHAR(16) DEFAULT ''"),
        ("ai_citizens", "native_lang", "VARCHAR(16) DEFAULT ''"),
    ]
    for tbl, col, ddl in cols:
        try:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS {col} {ddl}"))
        except Exception as exc:  # noqa: BLE001 列已存在/权限，均不致命
            logger.warning("pg ensure column skipped: %s.%s | %s", tbl, col, exc)


def _ensure_idempotency_indexes():
    """幂等唯一索引(仅 SQLite 部分索引)。防重复入账/重复结算。

    - uq_ledger_ai_ref: ai_ledger(citizen_id, type, ref) 非空 ref 唯一 —— 结算/扣费/发放幂等兜底
    - uq_repost_ai:      reposts(source_type, post_id, reposter_id) —— 同一 AI 对同一来源帖只能转一次
    - uq_ubi_ai:         ubi_grants(citizen_id, day) —— 低保每日只发一次
    - uq_rating_ai:      ratings(contract_id, from_id, to_id) —— 结算后互评只评一次
    - uq_dep_ai:         node_deps(node_id, dep_node_id) —— 依赖边唯一
    - uq_scheduler_run:  scheduler_runs(job_type, run_key) —— 调度日级幂等防重
    - uq_intel_dedup:    intel_reports(title, source_url) 非空 source_url 唯一 —— 情报入库去重
    """
    from sqlalchemy import text
    # SQLite 和 PostgreSQL 均支持 CREATE UNIQUE INDEX IF NOT EXISTS + 部分索引
    stmts = [
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_ai_ref "
        "ON ai_ledger(citizen_id, type, ref) WHERE ref <> ''",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_repost_ai "
        "ON reposts(source_type, post_id, reposter_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_ubi_ai "
        "ON ubi_grants(citizen_id, day)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_rating_ai "
        "ON ratings(contract_id, from_id, to_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_dep_ai "
        "ON node_deps(node_id, dep_node_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_scheduler_run "
        "ON scheduler_runs(job_type, run_key)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_intel_dedup "
        "ON intel_reports(title, source_url) WHERE source_url <> ''",
        # N 轮：统计快照日幂等 (date, metric, dimension)
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_stat_snapshot "
        "ON stat_snapshots(date, metric, dimension)",
        # N 轮：排行榜快照 (board_type, snapshot_at, rank) 每日每榜每名次唯一
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_leaderboard_rank "
        "ON leaderboard_snapshots(board_type, snapshot_at, rank)",
        # N 轮：webhook 订阅同宿主同 URL 唯一（防重复订阅刷推送）
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_url "
        "ON webhook_subscriptions(host_id, url)",
        # 技能库侦察采集：按来源 URL 去重（非空才约束；内置种子 source_url 空不受约束）
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_tool_source_url "
        "ON tool_plugins(source_url) WHERE source_url <> ''",
    ]
    with engine.begin() as conn:
        for s in stmts:
            try:
                conn.execute(text(s))
            except Exception as exc:  # noqa: BLE001
                logger.warning("create idempotency index skipped: %s | %s", s, exc)


def init_db():
    from sqlalchemy import text  # noqa: F401 供下方 _EXTRA_INDEXES 执行
    from . import models  # noqa: 注册模型
    Base.metadata.create_all(bind=engine)
    _ensure_columns()
    _ensure_pg_columns()
    _ensure_idempotency_indexes()
    for stmt in _EXTRA_INDEXES:
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception as exc:  # noqa: BLE001
            logger.warning("extra index skipped: %s | %s", stmt, exc)
    # 技能库/插件中心：内置种子目录 upsert + 运行时可用性探测 + 发现指令播种。
    # 全程容错——种子播种失败绝不影响数据库就绪与启动（缺目录时运行期懒加载兜底）。
    try:
        from .tool_registry import bootstrap
        bootstrap()
    except Exception as exc:  # noqa: BLE001
        logger.warning("tool_registry bootstrap skipped: %s", exc)
    logger.info("AIjuhe database ready: %s", url)
