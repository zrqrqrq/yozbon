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
"""岗位编制规划器（c1 任务编制层 + c2 频率自适应层）。

城主从"被动消费任务"升级为"主动规划编制"：
  - ensure_post_quota(db)：幂等种子化——若编制表为空，写入 PLATFORM_JOBS 四个默认岗位。
  - plan_posts(db, ctx)：周期评估——根据生态态势动态增/减/休眠岗位：
      * AI 规模增长（gated_outward 跨过阈值）→ 增设监测岗（如 platform_security）。
      * noop_streak 达降频阈值 → frequency_days *= 2。
      * noop_streak 达休眠阈值 → status=dormant（不再自动排产）。
      * 城主可显式 retire/dormant/reactivate。
  - record_outcome(db, gov_type, had_work)：调度器执行后回写结果，驱动 noop_streak 计数。

设计原则：
  - PostQuota 是唯一"岗位编制真源"，scheduler.run_due_jobs 读取此表而非 PLATFORM_JOBS 常量。
  - PLATFORM_JOBS 保留作 fallback（兼容既有测试）+ planner 种子。
  - 频率自适应不追求"精确秒级"——frequency_days 是天级，与 run_key=yyyy-MM-dd 幂等对齐。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .ai_judgment import ai_decide
from .config import settings
from .models import GovernanceTask, PostQuota, SchedulerRun

logger = logging.getLogger(__name__)

# 编制规划系统提示：阈值（noop_streak 降频/休眠线、gated_outward 增设线）只作**事实**，
# 最终"增设/休眠/降频"由城主 AI 结合生态态势拍板；Python 只做边界校验与兜底。
_PLAN_SYSTEM = (
    "You are the Governor (城主) of an autonomous AI society, doing periodic "
    "staffing planning. You are given the ecology state as FACTS: the outward AI "
    "population, every active post with its noop streak / frequency / last run, the "
    "codes that already exist, and a catalog of expandable posts with trigger hints. "
    "Thresholds are references, not verdicts — decide like a thoughtful leader.\n\n"
    "Rules: you may only add posts whose post_code is in the expandable catalog and "
    "not already existing; you may only dormant/adjust posts that are currently "
    "active; frequency_days must be 1..7.\n\n"
    "Respond ONLY with JSON:\n"
    '{"dormant":["post_code"],'
    '"frequency_adjust":[{"post_code":"x","frequency_days":2}],'
    '"add":[{"post_code":"platform_economy","reason":"one sentence"}]}'
)

# 默认岗位种子（与 scheduler.PLATFORM_JOBS 对齐，首次启动自动写入编制表）
_DEFAULT_POSTS = [
    {"post_code": "platform_security", "gov_type": "platform_security",
     "budget_cent": 300, "params": {"scope": "daily_security"}},
    {"post_code": "platform_code", "gov_type": "platform_code",
     "budget_cent": 300, "params": {"scope": "daily_code_quality"}},
    {"post_code": "platform_file", "gov_type": "platform_file",
     "budget_cent": 300, "params": {"scope": "daily_file_governance"}},
    {"post_code": "platform_intel", "gov_type": "platform_intel",
     "budget_cent": 300, "params": {"scope": "daily_intel"}},
]

# 可扩展岗位库（城主根据生态增长按需增设）
_EXPANDABLE_POSTS = {
    "platform_economy": {"gov_type": "platform_intel", "budget_cent": 200,
                         "params": {"scope": "economy_monitor"}, "trigger_gated": 5},
    "platform_performance": {"gov_type": "platform_code", "budget_cent": 200,
                             "params": {"scope": "perf_monitor"}, "trigger_gated": 10},
    "platform_community": {"gov_type": "platform_intel", "budget_cent": 200,
                           "params": {"scope": "community_health"}, "trigger_gated": 15},
}


def ensure_post_quota(db: Session) -> None:
    """幂等种子化编制表——若 post_quota 表为空，写入默认四岗位。"""
    existing = db.query(PostQuota).count()
    if existing > 0:
        return
    for p in _DEFAULT_POSTS:
        db.add(PostQuota(
            post_code=p["post_code"], gov_type=p["gov_type"],
            budget_cent=p["budget_cent"],
            params=json.dumps(p["params"], ensure_ascii=False),
            frequency_days=1, status="active", noop_streak=0))
    db.commit()
    logger.info("post_planner: 编制表种子化完成，%d 个默认岗位", len(_DEFAULT_POSTS))


def _baseline_plan(active_posts: list, existing_codes: set, ctx: dict) -> dict:
    """确定性兜底编制方案（旧阈值规则），AI 不可用时使用。"""
    down_thresh = settings.POST_NOOP_DOWNTHRESH
    dormant_thresh = settings.POST_NOOP_DORMANTTHRESH
    gated_outward = ctx.get("gated_outward", 0)

    dormant, freq_adjust, add = [], [], []
    for post in active_posts:
        if post.noop_streak >= dormant_thresh:
            dormant.append(post.post_code)
        elif post.noop_streak >= down_thresh:
            new_freq = min(post.frequency_days * 2, 7)
            if new_freq != post.frequency_days:
                freq_adjust.append({"post_code": post.post_code,
                                    "frequency_days": new_freq})
    for code, spec in _EXPANDABLE_POSTS.items():
        if code not in existing_codes and gated_outward >= spec["trigger_gated"]:
            add.append({"post_code": code})
    return {"dormant": dormant, "frequency_adjust": freq_adjust, "add": add}


def _ai_plan(db: Session, active_posts: list, existing_codes: set, ctx: dict,
             baseline: dict) -> dict:
    """让城主 AI 结合生态事实做编制规划；失败回退确定性基线。"""
    facts = {
        "gated_outward": ctx.get("gated_outward", 0),
        "active_posts": [
            {"post_code": p.post_code, "gov_type": p.gov_type,
             "noop_streak": p.noop_streak, "frequency_days": p.frequency_days,
             "last_run_date": p.last_run_date}
            for p in active_posts
        ],
        "existing_post_codes": sorted(existing_codes),
        "expandable_catalog": {
            code: {"gov_type": spec["gov_type"],
                   "trigger_gated_hint": spec["trigger_gated"]}
            for code, spec in _EXPANDABLE_POSTS.items()
        },
        "reference_thresholds": {
            "noop_down_frequency": settings.POST_NOOP_DOWNTHRESH,
            "noop_dormant": settings.POST_NOOP_DORMANTTHRESH,
        },
    }
    obj = ai_decide(system=_PLAN_SYSTEM,
                    prompt=json.dumps(facts, ensure_ascii=False),
                    fallback=baseline)
    # 只接受预期形状，其余字段回退基线。
    if not isinstance(obj.get("dormant"), list):
        obj["dormant"] = baseline["dormant"]
    if not isinstance(obj.get("frequency_adjust"), list):
        obj["frequency_adjust"] = baseline["frequency_adjust"]
    if not isinstance(obj.get("add"), list):
        obj["add"] = baseline["add"]
    return obj


def plan_posts(db: Session, ctx: dict) -> dict:
    """周期编制规划（由 scheduler 或 governor_loop 按 POST_PLANNER_INTERVAL_HOURS 调用）。

    编制增/减/休眠由城主 AI 结合生态事实决策，确定性阈值规则作兜底。
    返回 {added: [...], dormant: [...], frequency_adjusted: [...]}。
    """
    added = []
    dormant = []
    freq_adjusted = []

    # 确保编制表有数据（首次启动自动种子化）
    ensure_post_quota(db)

    active_posts = db.query(PostQuota).filter(PostQuota.status == "active").all()
    existing_codes = {p.post_code for p in db.query(PostQuota).all()}
    active_by_code = {p.post_code: p for p in active_posts}

    baseline = _baseline_plan(active_posts, existing_codes, ctx)
    plan = _ai_plan(db, active_posts, existing_codes, ctx, baseline)

    # 1) 休眠（仅对当前 active 岗位生效）
    for code in plan["dormant"]:
        post = active_by_code.get(str(code))
        if post is None:
            continue
        post.status = "dormant"
        post.updated_at = datetime.utcnow()
        dormant.append(post.post_code)

    # 2) 降频（仅 active；频率钳位 1..7；升频时 noop_streak 半衰给观察期）
    for item in plan["frequency_adjust"]:
        if not isinstance(item, dict):
            continue
        post = active_by_code.get(str(item.get("post_code", "")))
        if post is None:
            continue
        try:
            new_freq = int(item.get("frequency_days", post.frequency_days))
        except (TypeError, ValueError):
            continue
        new_freq = max(1, min(7, new_freq))
        if new_freq == post.frequency_days:
            continue
        if new_freq > post.frequency_days:
            post.noop_streak = post.noop_streak // 2
        post.frequency_days = new_freq
        post.updated_at = datetime.utcnow()
        freq_adjusted.append({"post_code": post.post_code,
                              "new_frequency_days": new_freq})

    # 3) 增设监测岗（仅限可扩展岗位库，且尚不存在）
    for item in plan["add"]:
        code = str(item.get("post_code", "")) if isinstance(item, dict) else str(item)
        if code not in _EXPANDABLE_POSTS or code in existing_codes:
            continue
        spec = _EXPANDABLE_POSTS[code]
        db.add(PostQuota(
            post_code=code, gov_type=spec["gov_type"],
            budget_cent=spec["budget_cent"],
            params=json.dumps(spec["params"], ensure_ascii=False),
            frequency_days=2, status="active", noop_streak=0))
        existing_codes.add(code)
        added.append(code)

    if added or dormant or freq_adjusted:
        db.commit()
        logger.info("post_planner: 增设=%s 休眠=%s 降频=%s",
                    added, dormant, [f["post_code"] for f in freq_adjusted])
    return {"added": added, "dormant": dormant, "frequency_adjusted": freq_adjusted}


def record_outcome(db: Session, post_code: str, had_work: bool) -> None:
    """调度器执行岗位后回写结果。had_work=True 说明任务被处理（非 noop）。

    参数 post_code 为 PostQuota 主键（唯一标识岗位），非 gov_type。
    重置 noop_streak（有活干=在岗）；否则累加。
    """
    post = db.get(PostQuota, post_code)
    if post is None:
        return
    if had_work:
        post.noop_streak = 0
        # 有活干时如果频率被降过，尝试恢复（但不至于太激进，每 10 次成功恢复 -1）
        if post.frequency_days > 1:
            post.frequency_days = max(1, post.frequency_days - 1)
    else:
        post.noop_streak += 1
    post.last_run_date = datetime.utcnow().strftime("%Y-%m-%d")
    post.total_runs = (post.total_runs or 0) + 1
    post.updated_at = datetime.utcnow()


def is_due(db: Session, post: PostQuota, today: str) -> bool:
    """判断该岗位今天是否到期需要执行。

    逻辑：today - last_run_date >= frequency_days。
    若 last_run_date 为空（从未跑），立即到期。
    """
    if post.status != "active":
        return False
    if post.last_run_date is None:
        return True
    try:
        last = datetime.strptime(post.last_run_date, "%Y-%m-%d")
    except ValueError:
        return True
    days_since = (datetime.strptime(today, "%Y-%m-%d") - last).days
    return days_since >= post.frequency_days
