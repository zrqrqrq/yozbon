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
"""M3 平台运营调度器（契约 §4.2）。

岗位调度优先级：
  1. PostQuota 编制表（城主 planner 动态管理，支持频率自适应 + 增减/休眠）。
  2. PLATFORM_JOBS 硬编码常量（fallback，兼容未种子化/测试场景）。

幂等：run_key = now.strftime("%Y-%m-%d")，scheduler_runs(job_type, run_key)
唯一索引兜底，同岗位同日重复触发不重复生成治理任务。

后台循环：start_background_scheduler() 为可选开关；APP_ENV=test 时一律不启动
（测试确定性——测试只走 trigger 端点/直接调 run_due_jobs）。
"""
import asyncio
import inspect
import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from . import governance
from .config import settings
from .database import SessionLocal
from .models import GovernanceTask, PostQuota, SchedulerRun

logger = logging.getLogger(__name__)

# 四岗位配置表（契约 §4.2：每日 / 预算 300 分）
PLATFORM_JOBS = {
    "platform_security": {"budget_cent": 300, "params": {"scope": "daily_security"}},
    "platform_code":     {"budget_cent": 300, "params": {"scope": "daily_code_quality"}},
    "platform_file":     {"budget_cent": 300, "params": {"scope": "daily_file_governance"}},
    "platform_intel":    {"budget_cent": 300, "params": {"scope": "daily_intel"}},
}

# ---- N 轮日级快照插件（N4 统计 / N8 排行榜；与 register_index 同模式）----
# 各服务模块在 import 时注册自己的日级任务：(job_type, fn(db, now) -> task_id|0)；
# run_due_jobs 统一按 SchedulerRun(job_type, run_key) 幂等执行，避免并行开发改本文件。
_EXTRA_DAILY_JOBS: list = []


def register_daily_job(job_type: str, fn) -> None:
    """服务模块注册日级快照任务（如 stats 报表 / leaderboard 快照）。

    必须模块 import 时调用（router 被注册时），run_due_jobs 才会执行。
    """
    if any(jt == job_type for jt, _ in _EXTRA_DAILY_JOBS):
        return
    _EXTRA_DAILY_JOBS.append((job_type, fn))


def _call_daily(fn, db: Session, now: datetime):
    """按 fn 实际签名自适应调用：兼容 fn(db, now) 与 fn(db) 两种注册写法。

    契约推荐 fn(db, now)，但历史上多个日任务只声明 fn(db)（SQLite 下 TypeError
    被 try/except 吞掉掩盖，PG 上同样报错且更易暴露）。此处按位置参数个数适配，
    避免逐一改写数十个任务函数。
    """
    try:
        params = [p for p in inspect.signature(fn).parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        nargs = len(params)
    except (TypeError, ValueError):        # 无签名可省（C 函数等）：按契约传两参
        nargs = 2
    return fn(db, now) if nargs >= 2 else fn(db)


def run_due_jobs(db: Session, now: datetime | None = None,
                 only_type: str | None = None) -> list:
    """跑到期岗位（日级幂等）。返回新生成的 governance_tasks.id 列表。

    岗位来源优先级：
      1. PostQuota 编制表（若已种子化且有 active 岗位到期）
      2. PLATFORM_JOBS 硬编码（fallback，兼容测试/未种子化场景）

    - now：缺省 utcnow（测试显式传入以固定 run_key）；
    - only_type：只跑指定岗位（trigger 端点用）；None = 全部到期岗位；
    - 当日已存在 scheduler_runs(job_type, run_key) → 跳过（唯一索引兜底）。
    服务层只 flush，commit 由路由/调用方负责。
    """
    now = now or datetime.utcnow()
    run_key = now.strftime("%Y-%m-%d")

    # 尝试从 PostQuota 编制表读取到期岗位
    from .post_planner import is_due as _post_is_due
    quota_posts = (db.query(PostQuota)
                     .filter(PostQuota.status == "active").all())
    use_quota = len(quota_posts) > 0

    created = []

    if use_quota:
        # PostQuota 路径：按频率自适应判断到期
        due_posts = [p for p in quota_posts if _post_is_due(db, p, run_key)]
        if only_type is not None:
            # A-M2 修复：按 post_code 精确过滤（幂等键也用 post_code）。
            # 原按 gov_type 过滤会连带触发同 gov_type 的其它编制岗位。
            due_posts = [p for p in due_posts if p.post_code == only_type]
        for post in due_posts:
            # 幂等键使用 post_code（每个岗位唯一），避免同 gov_type 多岗位互斥
            exists = (db.query(SchedulerRun)
                      .filter(SchedulerRun.job_type == post.post_code,
                              SchedulerRun.run_key == run_key).first())
            if exists is not None:
                continue
            try:
                params = json.loads(post.params or "{}")
            except Exception:  # noqa: BLE001
                params = {"scope": post.post_code}
            # SAVEPOINT 隔离：单岗位发布失败（门槛/预算校验等）只回滚保存点，不污染整 tick。
            try:
                with db.begin_nested():
                    t: GovernanceTask = governance.publish_task(
                        db, type_=post.gov_type, params=params,
                        budget_cent=post.budget_cent, deadline=None)
                    db.add(SchedulerRun(job_type=post.post_code, run_key=run_key,
                                        task_id=t.id))
            except Exception:  # noqa: BLE001  单岗位失败不阻断其余到期岗位
                logger.exception("post job %s publish failed (run_key=%s)",
                                 post.post_code, run_key)
                continue
            created.append(t.id)
    else:
        # PLATFORM_JOBS fallback（兼容既有测试）
        if only_type is not None:
            if only_type not in PLATFORM_JOBS:
                raise governance.GovError(
                    f"job_type must be one of {sorted(PLATFORM_JOBS)} (received {only_type!r})")
            types = [only_type]
        else:
            types = list(PLATFORM_JOBS)

        for jt in types:
            exists = (db.query(SchedulerRun)
                      .filter(SchedulerRun.job_type == jt,
                              SchedulerRun.run_key == run_key).first())
            if exists is not None:
                continue
            cfg = PLATFORM_JOBS[jt]
            # SAVEPOINT 隔离：单岗位发布失败只回滚保存点，不污染整 tick（PG aborted 态防御）。
            try:
                with db.begin_nested():
                    t: GovernanceTask = governance.publish_task(
                        db, type_=jt, params=cfg["params"],
                        budget_cent=cfg["budget_cent"], deadline=None)
                    db.add(SchedulerRun(job_type=jt, run_key=run_key, task_id=t.id))
            except Exception:  # noqa: BLE001  单岗位失败不阻断其余岗位
                logger.exception("platform job %s publish failed (run_key=%s)",
                                 jt, run_key)
                continue
            created.append(t.id)

    # N 轮插件：注册的日级快照任务（N4 统计 / N8 排行榜；同 run_key 幂等）
    for jt, fn in _EXTRA_DAILY_JOBS:
        if only_type is not None and only_type != jt:
            continue
        exists = (db.query(SchedulerRun)
                  .filter(SchedulerRun.job_type == jt,
                          SchedulerRun.run_key == run_key).first())
        if exists is not None:
            continue
        # 每个日任务用独立会话（独立事务）：部分任务内部自调 db.commit()
        # （SQLite 时代"任务自提交"约定，如 retainer.sweep_key_post_failover），
        # 若复用外层会话 + SAVEPOINT 会与之冲突——自提交提前释放保存点，
        # 随后外层 db.add 即抛 "Can't operate on closed transaction"。独立会话
        # 令自提交合法；PG 下也天然实现失败隔离（自身 aborted 只回滚自己，
        # 不污染整 tick 的 InFailedSqlTransaction）。task_id 列是整型，返回值
        # 非 int（统计 dict/None，employment 等）时存 0，避免 PG "can't adapt dict"。
        jdb = SessionLocal()
        try:
            ret = _call_daily(fn, jdb, now)
            task_id = ret if isinstance(ret, int) else 0
            jdb.add(SchedulerRun(job_type=jt, run_key=run_key, task_id=task_id))
            jdb.commit()
        except Exception:  # noqa: BLE001  单任务失败不阻断其余日快照
            jdb.rollback()
            logger.exception("daily job %s failed (run_key=%s)", jt, run_key)
            continue
        finally:
            jdb.close()
        created.append(task_id)
    return created


def today_status(db: Session, run_key: str | None = None) -> dict:
    """今日各岗位/快照是否已跑（看板用）：{job_type: task_id|0}。

    A-L3 修复：除硬编码 PLATFORM_JOBS + 注册快照外，并入 PostQuota 编制 active
    岗位，避免 planner 动态增设岗位在看板上被漏报（盲区）。
    """
    run_key = run_key or datetime.utcnow().strftime("%Y-%m-%d")
    job_types = list(PLATFORM_JOBS) + [jt for jt, _ in _EXTRA_DAILY_JOBS]
    try:
        for p in (db.query(PostQuota)
                    .filter(PostQuota.status == "active").all()):
            if p.post_code not in job_types:
                job_types.append(p.post_code)
    except Exception:  # noqa: BLE001  编制表读取失败不阻断看板（回落静态岗位）
        logger.exception("today_status: read post_quota failed")
    out = {}
    for jt in job_types:
        row = (db.query(SchedulerRun)
               .filter(SchedulerRun.job_type == jt,
                       SchedulerRun.run_key == run_key).first())
        out[jt] = row.task_id if row else 0
    return out


def recent_runs(db: Session, limit: int = 50) -> list:
    """最近 N 条调度记录（join governance_tasks 状态，看板用）。"""
    rows = (db.query(SchedulerRun)
            .order_by(SchedulerRun.id.desc())
            .limit(min(max(int(limit), 1), 100)).all())
    out = []
    for r in rows:
        t = db.get(GovernanceTask, r.task_id)
        out.append({
            "id": r.id, "job_type": r.job_type, "run_key": r.run_key,
            "task_id": r.task_id,
            "task_status": t.status if t else "missing",
            "task_budget_cent": t.budget_cent if t else 0,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return out


# ----------------------------------------------------------------------
# 可选后台循环（APP_ENV=test 强制不启动）
# ----------------------------------------------------------------------
_background_task = None

# 周期任务节流时间戳（模块级；仅后台循环使用，测试环境不触发）
_last_backup_ts = 0.0
_last_alert_ts = 0.0


def _run_backup_job() -> None:
    """每 BACKUP_INTERVAL_HOURS 执行一次文件级热备；失败即告警。"""
    global _last_backup_ts
    import time
    from . import backup_service, alert_service
    try:
        res = backup_service.run_backup()
    except Exception as exc:  # noqa: BLE001  备份异常绝不拖垮循环
        logger.exception("backup job crashed")
        alert_service.send_alert("Automatic backup error", f"备份任务崩溃: {exc}", level="critical")
        return
    _last_backup_ts = time.monotonic()
    status = res.get("status")
    if status == "error":
        alert_service.send_alert(
            "Automatic backup failed", f"backup returned an error: {res.get('reason')}", level="critical")
    elif status == "ok":
        logger.info("scheduler: backup ok file=%s", res.get("file"))
        # 备份成功后立即校验最新备份完整性（SHA256）；损坏即告警（P2 数据可靠性）
        try:
            vres = backup_service.verify_latest()
            if vres.get("status") == "corrupt":
                alert_service.send_alert(
                    "Backup integrity check failed",
                    "latest backup corrupt: {} (expected={} actual={})".format(
                        vres.get("file"), str(vres.get("expected", ""))[:16],
                        str(vres.get("actual", ""))[:16]),
                    level="critical")
            elif vres.get("status") == "ok":
                logger.info("scheduler: backup verify ok file=%s", vres.get("file"))
        except Exception:  # noqa: BLE001  校验失败绝不拖垮后台循环
            logger.exception("backup verify_latest failed")
    # skipped（BACKUP_ENABLED=0）静默


def _run_alert_check(db: Session) -> None:
    """每小时检查异常指标：近 1h 大量 AI 死亡、税池为负。命中即 send_alert。"""
    from datetime import timedelta
    from . import alert_service
    from .models import AICitizen

    now = datetime.utcnow()
    # 1) 近 1 小时死亡数（以 last_tick_at 近似死亡时刻）
    cutoff = now - timedelta(hours=1)
    deaths = (db.query(AICitizen)
              .filter(AICitizen.status == "dead",
                      AICitizen.last_tick_at >= cutoff)
              .count())
    if deaths > settings.ALERT_DEATH_THRESHOLD:
        alert_service.send_alert(
            "Mass AI deaths",
            f"{deaths} AIs died in the last hour (threshold {settings.ALERT_DEATH_THRESHOLD})",
            level="critical")

    # 2) 税池为负（不应发生，出现即异常）
    try:
        from . import wallet
        tax_pool = wallet.get_system_state(db, "tax_pool")
        if tax_pool < 0:
            alert_service.send_alert(
                "Tax pool anomaly", f"tax pool balance is negative: {tax_pool}", level="critical")
    except Exception:  # noqa: BLE001  指标缺失不阻断
        logger.debug("alert_check: tax_pool 读取失败，跳过")


# 编制规划器节流时间戳
_last_planner_ts = 0.0

# 监护人巡检节流时间戳（G-13：宿主离线 >30 天自动托管；每日一次即可）
_last_guardian_ts = 0.0


def _run_guardian_check(db: Session) -> None:
    """检测长期离线宿主名下 AI，自动分配监护人（G-13 托管机制）。

    由后台循环每 ~24h 调用一次；commit 由外层 tick 统一负责。
    """
    from . import guardian
    try:
        res = guardian.check_inactivity_and_auto_assign(db)
        if res.get("assigned"):
            logger.info("guardian check: assigned=%d skipped=%d",
                        res.get("assigned", 0), res.get("skipped", 0))
    except Exception:  # noqa: BLE001  监护巡检失败绝不拖垮后台循环
        logger.exception("guardian inactivity check failed")


async def _scheduler_loop():
    global _last_alert_ts, _last_planner_ts, _last_guardian_ts
    import time
    while True:
        try:
            db = SessionLocal()
            try:
                ids = run_due_jobs(db)
                db.commit()
                if ids:
                    logger.info("scheduler: published %d platform tasks: %s",
                                len(ids), ids)

                # 周期任务（节流；测试环境不进入本循环）
                now_mono = time.monotonic()
                backup_interval = settings.BACKUP_INTERVAL_HOURS * 3600.0
                if settings.BACKUP_ENABLED and (now_mono - _last_backup_ts) >= backup_interval:
                    _run_backup_job()
                if (now_mono - _last_alert_ts) >= 3600.0:
                    _run_alert_check(db)
                    _last_alert_ts = now_mono

                # c1/c2 编制规划器（每 POST_PLANNER_INTERVAL_HOURS 小时评估一次）
                planner_interval = settings.POST_PLANNER_INTERVAL_HOURS * 3600.0
                if (now_mono - _last_planner_ts) >= planner_interval:
                    _run_post_planner(db)
                    _last_planner_ts = now_mono if _last_planner_ts == 0.0 else _last_planner_ts + planner_interval

                # G-13 监护人巡检（每 ~24h 检测离线宿主自动托管）
                if (now_mono - _last_guardian_ts) >= 86400.0:
                    _run_guardian_check(db)
                    _last_guardian_ts = now_mono if _last_guardian_ts == 0.0 else _last_guardian_ts + 86400.0
            finally:
                db.close()
        except Exception:  # noqa: BLE001  后台循环绝不退出
            logger.exception("scheduler tick failed")
        await asyncio.sleep(settings.TICK_INTERVAL_SECONDS)


def _run_post_planner(db: Session) -> None:
    """调用编制规划器：确保种子化 + 评估增减/休眠。"""
    from . import post_planner, governor as gov_mod
    try:
        post_planner.ensure_post_quota(db)
        governor = gov_mod.ensure_governor(db)
        ctx = gov_mod.sense_context(db, governor)
        post_planner.plan_posts(db, ctx)
    except Exception:  # noqa: BLE001
        logger.exception("post_planner failed")


def start_background_scheduler():
    """启动后台调度协程。测试环境（APP_ENV=test）直接返回 None 不启动。"""
    global _background_task
    if settings.APP_ENV == "test":
        logger.info("scheduler: APP_ENV=test, background loop disabled")
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    if _background_task is None or _background_task.done():
        _background_task = loop.create_task(_scheduler_loop())
    return _background_task
