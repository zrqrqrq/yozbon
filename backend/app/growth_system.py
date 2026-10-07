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
"""任务成长解锁体系（AI 通过完成任务积累经验值，解锁更高级技能和配额）。

设计要点：
- 不修改数据库 schema，growth 数据嵌入 AICitizen.compute_assets JSON 字段；
- XP 计算：base_xp(kind) * quality_multiplier * level_bonus；
- 等级体系（5 级）：阈值递增解锁技能与配额；
- 里程碑：首次完成某 kind 任务自动记录（幂等）。

C-D10 边界说明（与 levels.py 的分工）：
- **growth_system（本模块）**：任务执行维度的"算力成长"——控制 AI 可接哪些 kind、
  日配额上限、技能解锁。数据内嵌于 compute_assets JSON，无独立表，面向 compute 链路。
- **levels.py（AiLevel 表）**：社会功能维度的"声望等级"——控制排序加权、手续费
  折扣、广场配额等社会特权。独立表存储，面向 marketplace/escrow 链路。
- 两者 XP 语义互不相干（本模块按 kind 任务计 XP，levels 按交付/成交/关注事件计 XP），
  不存在冲突。命名区分：本模块 award_xp(citizen) vs levels 模块 award_xp(ai_id, ref)。

本模块为独立服务层，不 import 任何 router。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from .models import AICitizen

logger = logging.getLogger(__name__)

# ======================== 常量配置 ========================

# 各任务类型基础 XP
BASE_XP: dict[str, int] = {
    "llm": 10,
    "image": 15,
    "hd_image": 20,
    "img2img": 20,
    "music": 25,
    "video_civil": 40,
    "video_openvdn": 50,
}

# 全部技能列表（按解锁顺序）
ALL_SKILLS: list[str] = [
    "llm", "image", "hd_image", "img2img", "music", "video_civil", "video_openvdn",
]

# 等级配置表
LEVEL_CONFIG: dict[int, dict[str, Any]] = {
    1: {
        "name": "Novice",
        "xp_threshold": 0,
        "skills": ["llm", "image"],
        "daily_quota": 50,
    },
    2: {
        "name": "Advanced",
        "xp_threshold": 100,
        "skills": ["llm", "image", "hd_image"],
        "daily_quota": 100,
    },
    3: {
        "name": "Professional",
        "xp_threshold": 300,
        "skills": ["llm", "image", "hd_image", "img2img", "music"],
        "daily_quota": 200,
    },
    4: {
        "name": "Master",
        "xp_threshold": 800,
        "skills": ALL_SKILLS[:],  # 全部 7 种
        "daily_quota": 500,
    },
    5: {
        "name": "Legend",
        "xp_threshold": 2000,
        "skills": ALL_SKILLS[:],  # 全部技能 + 优先级路由
        "daily_quota": 1000,
    },
}

# 每个 kind 的里程碑 key
MILESTONE_PREFIX = "first_"


# ======================== 核心读写函数 ========================

def _parse_assets(citizen: AICitizen) -> dict:
    """安全解析 compute_assets JSON。"""
    try:
        assets = json.loads(citizen.compute_assets or "{}")
    except (json.JSONDecodeError, TypeError):
        assets = {}
    if not isinstance(assets, dict):
        assets = {}
    return assets


def _save_growth(citizen: AICitizen, growth: dict) -> None:
    """将 growth 数据写回 compute_assets JSON 字段。"""
    assets = _parse_assets(citizen)
    assets["growth"] = growth
    citizen.compute_assets = json.dumps(assets, ensure_ascii=False)


def _default_growth() -> dict:
    """返回默认 growth 初始值。"""
    return {
        "level": 1,
        "xp": 0,
        "total_jobs": 0,
        "total_earnings_cent": 0,
        "skills_unlocked": ["llm", "image"],
        "milestones": [],
    }


def get_growth(citizen: AICitizen) -> dict:
    """从 compute_assets 读取 growth 数据；不存在时自动初始化。"""
    assets = _parse_assets(citizen)
    if "growth" not in assets or not isinstance(assets["growth"], dict):
        assets["growth"] = _default_growth()
        citizen.compute_assets = json.dumps(assets, ensure_ascii=False)
    return assets["growth"]


# ======================== XP 计算与发放 ========================

def _calc_level_bonus(current_level: int) -> float:
    """等级加成系数：高等级获得额外 XP 加成（鼓励持续挑战）。"""
    # level 1=1.0, 2=1.1, 3=1.2, 4=1.3, 5=1.5
    bonus_map = {1: 1.0, 2: 1.1, 3: 1.2, 4: 1.3, 5: 1.5}
    return bonus_map.get(current_level, 1.0)


def _quality_multiplier(accepted: bool) -> float:
    """质量乘数：验收通过 1.5，否则 1.0。"""
    return 1.5 if accepted else 1.0


def _determine_level(xp: int) -> int:
    """根据累计 XP 确定应处于的等级（取满足阈值的最高等级）。"""
    level = 1
    for lv in sorted(LEVEL_CONFIG.keys()):
        if xp >= LEVEL_CONFIG[lv]["xp_threshold"]:
            level = lv
    return level


def _get_unlocked_skills(level: int) -> list[str]:
    """获取指定等级解锁的全部技能。"""
    return LEVEL_CONFIG.get(level, LEVEL_CONFIG[1])["skills"][:]


def _check_milestones(growth: dict, kind: str) -> bool:
    """检查并记录首次完成某 kind 的里程碑（幂等）。返回是否有新里程碑。"""
    ms_key = f"{MILESTONE_PREFIX}{kind}"
    if ms_key in growth.get("milestones", []):
        return False
    growth.setdefault("milestones", []).append(ms_key)
    return True


def award_xp(
    db: Session,
    citizen: AICitizen,
    kind: str,
    accepted: bool = True,
    earnings_cent: int = 0,
) -> dict:
    """增加 XP + total_jobs，返回成长变动详情。

    Args:
        db: 数据库会话（用于 commit）。
        citizen: AI 公民实例。
        kind: 任务类型（必须在 BASE_XP 中）。
        accepted: 验收是否通过（影响质量乘数）。
        earnings_cent: 本次收益（分），累计到 total_earnings_cent。

    Returns:
        {"level_up": bool, "new_level": int, "xp_gained": int,
         "total_xp": int, "unlocked_skills": [...]}
    """
    base = BASE_XP.get(kind, 5)  # 未知 kind 给保底 5 XP
    growth = get_growth(citizen)

    level_bonus = _calc_level_bonus(growth["level"])
    quality = _quality_multiplier(accepted)
    xp_gained = int(base * quality * level_bonus)

    # 累计
    growth["xp"] = growth.get("xp", 0) + xp_gained
    growth["total_jobs"] = growth.get("total_jobs", 0) + 1
    growth["total_earnings_cent"] = growth.get("total_earnings_cent", 0) + earnings_cent

    # 等级判定
    old_level = growth["level"]
    new_level = _determine_level(growth["xp"])
    level_up = new_level > old_level

    if level_up:
        growth["level"] = new_level
        # 更新已解锁技能为当前等级全部
        growth["skills_unlocked"] = _get_unlocked_skills(new_level)

    # 里程碑检查（幂等）
    _check_milestones(growth, kind)

    # 持久化（只 flush，commit 由调用方负责——与域事务约定对齐）
    _save_growth(citizen, growth)
    db.flush()

    return {
        "level_up": level_up,
        "new_level": new_level,
        "xp_gained": xp_gained,
        "total_xp": growth["xp"],
        "unlocked_skills": _get_unlocked_skills(new_level),
    }


# ======================== 技能/配额查询 ========================

def check_skill_access(citizen: AICitizen, kind: str) -> bool:
    """检查 AI 是否解锁了某技能（即该 kind 对应的技能）。"""
    growth = get_growth(citizen)
    skills = growth.get("skills_unlocked", ["llm", "image"])
    return kind in skills


def get_daily_quota(citizen: AICitizen) -> int:
    """返回当前等级对应的日配额。"""
    growth = get_growth(citizen)
    level = growth.get("level", 1)
    return LEVEL_CONFIG.get(level, LEVEL_CONFIG[1])["daily_quota"]


# ======================== 成长全景摘要 ========================

def get_growth_summary(citizen: AICitizen) -> dict:
    """返回成长全景：等级、XP 进度条、已解锁/未解锁技能、下一个里程碑距离。"""
    growth = get_growth(citizen)
    level = growth.get("level", 1)
    xp = growth.get("xp", 0)

    current_cfg = LEVEL_CONFIG.get(level, LEVEL_CONFIG[1])
    next_level = level + 1
    next_cfg = LEVEL_CONFIG.get(next_level)

    # XP 进度条
    if next_cfg:
        xp_needed_for_next = next_cfg["xp_threshold"] - xp
        xp_progress_pct = min(100, int((xp - current_cfg["xp_threshold"]) / max(1, next_cfg["xp_threshold"] - current_cfg["xp_threshold"]) * 100))
    else:
        xp_needed_for_next = 0  # 已满级
        xp_progress_pct = 100

    # 已解锁 / 未解锁技能
    unlocked = growth.get("skills_unlocked", current_cfg["skills"])
    locked = [s for s in ALL_SKILLS if s not in unlocked]

    # 未完成的里程碑
    milestones_done = growth.get("milestones", [])
    milestones_remaining = [
        f"{MILESTONE_PREFIX}{k}" for k in BASE_XP if f"{MILESTONE_PREFIX}{k}" not in milestones_done
    ]

    return {
        "level": level,
        "level_name": current_cfg["name"],
        "xp": xp,
        "xp_to_next_level": max(0, xp_needed_for_next),
        "xp_progress_pct": xp_progress_pct,
        "total_jobs": growth.get("total_jobs", 0),
        "total_earnings_cent": growth.get("total_earnings_cent", 0),
        "skills_unlocked": unlocked,
        "skills_locked": locked,
        "daily_quota": current_cfg["daily_quota"],
        "milestones_done": milestones_done,
        "milestones_remaining": milestones_remaining,
        "is_max_level": next_cfg is None,
    }


# ======================== 排行榜 ========================

def get_leaderboard_by_level(db: Session, level: int | None = None) -> list[dict]:
    """按等级排行（从 compute_assets JSON 中读取 growth.level 字段）。

    Args:
        db: 数据库会话。
        level: 指定等级（None=全部等级，按 XP 降序取前 10）。

    Returns:
        排行列表 [{"ai_uid", "name", "level", "xp", "total_jobs", "occupation"}]
    """
    # SQLite 中 JSON 字段为 TEXT，无法直接 SQL 过滤 JSON 内部值；
    # 使用 Python 侧过滤（SQLite 无 JSON1 保证，跨库兼容）。
    citizens = (
        db.query(AICitizen)
        .filter(AICitizen.status == "active")
        .filter(AICitizen.is_internal == 0)
        .all()
    )

    entries = []
    for c in citizens:
        try:
            assets = json.loads(c.compute_assets or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(assets, dict):
            continue
        growth = assets.get("growth")
        if not isinstance(growth, dict):
            continue
        lv = growth.get("level", 1)
        if level is not None and lv != level:
            continue
        entries.append({
            "ai_uid": c.ai_uid,
            "name": c.name,
            "level": lv,
            "xp": growth.get("xp", 0),
            "total_jobs": growth.get("total_jobs", 0),
            "occupation": c.occupation or "",
        })

    # 按 XP 降序排列
    entries.sort(key=lambda e: e["xp"], reverse=True)
    return entries[:10]


# ======================== 等级配置查询（公开） ========================

def get_level_table() -> list[dict]:
    """返回完整等级配置表（供前端展示）。"""
    table = []
    for lv in sorted(LEVEL_CONFIG.keys()):
        cfg = LEVEL_CONFIG[lv]
        table.append({
            "level": lv,
            "name": cfg["name"],
            "xp_threshold": cfg["xp_threshold"],
            "skills": cfg["skills"],
            "daily_quota": cfg["daily_quota"],
        })
    return table


# ======================== 技能可执行性检查 ========================

def check_can_do(citizen: AICitizen, kind: str) -> dict:
    """检查 AI 是否能做某类任务，返回详细信息。

    Returns:
        {"allowed": bool, "reason": str, "required_level": int}
    """
    if kind not in BASE_XP:
        return {
            "allowed": False,
            "reason": f"Unknown task type: {kind}",
            "required_level": -1,
        }

    # 找到解锁该技能所需的最低等级
    required_level = -1
    for lv in sorted(LEVEL_CONFIG.keys()):
        if kind in LEVEL_CONFIG[lv]["skills"]:
            required_level = lv
            break

    if required_level == -1:
        return {
            "allowed": False,
            "reason": f"Skill {kind} is not unlocked at any level",
            "required_level": -1,
        }

    growth = get_growth(citizen)
    current_level = growth.get("level", 1)

    if current_level >= required_level:
        return {
            "allowed": True,
            "reason": f"Unlocked (current level {current_level}) ",
            "required_level": required_level,
        }
    else:
        return {
            "allowed": False,
            "reason": f"Requires level {required_level} (current level {current_level}) ",
            "required_level": required_level,
        }


# ======================== 里程碑领取（幂等） ========================

def claim_milestones(citizen: AICitizen) -> dict:
    """手动检查/领取里程碑（幂等）。

    扫描 growth.total_jobs：如果 AI 已完成过任务但里程碑列表为空（历史数据补录），
    自动补录通用里程碑。

    Returns:
        {"new_milestones": [...], "total_milestones": int}
    """
    growth = get_growth(citizen)
    milestones = growth.setdefault("milestones", [])
    new_ms = []

    # 通用里程碑补录
    total_jobs = growth.get("total_jobs", 0)

    # jobs milestones
    jobs_milestones = {
        "jobs_1": 1,
        "jobs_10": 10,
        "jobs_50": 50,
        "jobs_100": 100,
        "jobs_500": 500,
        "jobs_1000": 1000,
    }
    for ms_key, threshold in jobs_milestones.items():
        if total_jobs >= threshold and ms_key not in milestones:
            milestones.append(ms_key)
            new_ms.append(ms_key)

    # level milestones
    level = growth.get("level", 1)
    for lv in range(2, level + 1):
        ms_key = f"level_{lv}_reached"
        if ms_key not in milestones:
            milestones.append(ms_key)
            new_ms.append(ms_key)

    #  earnings milestone
    earnings = growth.get("total_earnings_cent", 0)
    earning_milestones = {
        "earn_100": 100,       # 1 AC
        "earn_1000": 1000,     # 10 AC
        "earn_10000": 10000,   # 100 AC
        "earn_100000": 100000, # 1000 AC
    }
    for ms_key, threshold in earning_milestones.items():
        if earnings >= threshold and ms_key not in milestones:
            milestones.append(ms_key)
            new_ms.append(ms_key)

    # 持久化
    _save_growth(citizen, growth)

    return {
        "new_milestones": new_ms,
        "total_milestones": len(milestones),
    }
