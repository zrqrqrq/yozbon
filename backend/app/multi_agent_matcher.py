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
"""多 Agent 自动撮合引擎——编排计划的智能分配层。

定位：坐在 task_orchestrator 的 execute_plan 之上，解决"谁来执行哪个子任务"问题。
给定编排计划中的子任务列表，自动为每个子任务匹配最合适的 AI 执行者。

核心能力：
  1. **技能匹配**：子任务的 kind 通过 occupation 映射到 AI 的专长领域；
  2. **优先级排序**：技能命中 > 无技能信息(通用) > 技能域不同；同级内按 status 与 class_level 排序；
  3. **负载均衡**：多个 AI 同等匹配时，选当前分配最少的；
  4. **分配策略**：支持 smart（完整匹配）/ single（退化单 Agent）/ round_robin（轮询）三种模式。

与现有模块的关系（全部只读调用，不改任何现有文件）：
  - task_orchestrator.SKILL_KIND_MAP：kind 归一化字典，用于 occupation → kind 反向映射；
  - models.AICitizen：查询宿主名下可用 AI 列表；
  - task_orchestrator.OrchestrationPlan：输入来源（含 subtasks 列表）。

设计原则：
  - 纯查询 + 内存计算，不修改任何数据库记录（分配结果由调用方持久化）；
  - 幂等：同一组输入多次调用返回相同结果（不考虑随机性）；
  - 降级友好：无精确匹配时自动退化为通用兜底，不会返回空映射。
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import AICitizen, CapabilityProfile, Contract
from .task_orchestrator import SKILL_KIND_MAP
from . import task_difficulty
from .config import settings

logger = logging.getLogger(__name__)


# ---------------- 分配策略常量 ----------------

STRATEGY_SMART = "smart"
STRATEGY_SINGLE = "single"
STRATEGY_ROUND_ROBIN = "round_robin"

# class_level 优先级（数值越小越优先）
_LEVEL_PRIORITY: dict[str, int] = {
    "boss": 0,
    "capital": 1,
    "governance": 2,
    "middle": 3,
    "mid": 3,  # 别名（部分调用方使用 "mid"）
    "bottom": 4,
}

# status 优先级（数值越小越优先）
_STATUS_PRIORITY: dict[str, int] = {
    "active": 0,
    "apprentice": 1,
    "intern": 2,
}

# 合同"在办"状态集（未到终局 accepted/breached/refunded 视为占用该 AI 产能）
_ACTIVE_CONTRACT_STATUSES: tuple[str, ...] = ("escrowed", "executing", "delivered")

# 从未接单 AI 的 last_assigned_ts 哨兵：取 0.0（epoch 起点），
# 在"最近最久没活"升序排序里排最前——即"从没干过活"的 AI 最该优先派活。
_NEVER_ASSIGNED_TS: float = 0.0


def compute_idle_metrics(db: Session, agent_ids: list[int]) -> dict[int, tuple[int, float]]:
    """从 Contract 实时聚合 AI 闲置信号（零 schema 变更）。

    返回 {citizen_id: (active_task_count, last_assigned_ts)}：
      - active_task_count：该 AI 当前在办合同数（status ∈ 在办集），越少越闲；
      - last_assigned_ts：该 AI 最近一次接单时间（任意合同 created_at 最大值），
        越旧越该优先；从未接单 → 0.0（哨兵，排最前）。

    无任何合同的 AI 不出现在结果中，调用方按缺省 (0, NEVER_ASSIGNED_TS) 处理。

    Args:
        db: 数据库 Session。
        agent_ids: 候选 AI 的 citizen_id 列表。

    Returns:
        {citizen_id: (active_task_count, last_assigned_ts)}。
    """
    if not agent_ids:
        return {}

    # 最近一次接单时间（全部合同）
    last_ts_rows = (
        db.query(Contract.worker_id, func.max(Contract.created_at))
          .filter(Contract.worker_id.in_(agent_ids))
          .group_by(Contract.worker_id)
          .all()
    )
    last_ts: dict[int, float] = {}
    for wid, mx in last_ts_rows:
        if mx is not None:
            last_ts[wid] = mx.timestamp()

    # 在办合同数
    active_rows = (
        db.query(Contract.worker_id, func.count(Contract.id))
          .filter(Contract.worker_id.in_(agent_ids),
                  Contract.status.in_(_ACTIVE_CONTRACT_STATUSES))
          .group_by(Contract.worker_id)
          .all()
    )
    active_count: dict[int, int] = {wid: int(cnt) for wid, cnt in active_rows}

    out: dict[int, tuple[int, float]] = {}
    for aid in agent_ids:
        out[aid] = (active_count.get(aid, 0), last_ts.get(aid, _NEVER_ASSIGNED_TS))
    return out


def is_small_task(plan=None, constraints: Optional[dict] = None,
                  budget_cent: Optional[int] = None) -> bool:
    """判定是否"小任务"（用于决定是否启用闲置优先撮合）。

    分级信号：编排约束预算 constraints["budget_cent"]，或显式 budget_cent。
      - budget_cent ≤ settings.IDLE_PREFERRED_MAX_BUDGET_CENT → 小任务（True）
      - 预算高于阈值，或无任何预算上下文（保守）→ 大任务（False）

    仅看预算，不看 kind 难度（kind 难度信号弱且易误判，故不纳入）。

    Args:
        plan: OrchestrationPlan 对象（读其 .constraints）。
        constraints: 显式约束字典（优先于 plan.constraints）。
        budget_cent: 显式预算（优先于 constraints）。

    Returns:
        True=小任务（应闲置优先），False=大任务/未知（维持纯择优）。
    """
    budget = budget_cent
    if budget is None:
        cons = constraints
        if cons is None and plan is not None:
            cons = getattr(plan, "constraints", None)
        if isinstance(cons, dict):
            budget = cons.get("budget_cent")
    # 委托难度档：小任务 ⟺ 预算档 = S（S/M 边界 = BUDGET_TIER_M_CENT，默认复用
    # IDLE_PREFERRED_MAX_BUDGET_CENT）。无预算上下文 → 非 S 档 → 保守视为大任务，
    # 行为与"≤ 阈值即小任务"逐条一致。
    tier, _known = task_difficulty.budget_tier(budget)
    return tier == "S"


# 平台可执行 kind 全集（platform_compute 接受的通道值）
_KNOWN_KINDS: frozenset[str] = frozenset({
    "image", "hd_image", "img2img", "music",
    "video_civil", "video_openvdn", "llm",
})


def normalize_occupation(raw: str) -> str:
    """把描述式 occupation / skill / channel 归一到执行 kind。

    兼容种子公民的描述式 occupation：
      "Text-to-Image"→image、"HD Image"→hd_image、"Image-to-Image"→img2img、
      "Music"→music、"Video"→video_civil、"High-Motion Video"→video_openvdn、
      "Copywriting & Reasoning"→llm。

    返回空串表示"无技能信息"（如 "platform"、通用描述），调用方按通用兜底处理。
    """
    s = (raw or "").lower().strip()
    if not s:
        return ""
    # 描述式关键词优先（顺序敏感：img2img/hd/video 需先于泛化的 image）
    if "image-to-image" in s or "img2img" in s or "i2i" in s:
        return "img2img"
    if "hd" in s and "image" in s:
        return "hd_image"
    if "video" in s:
        return "video_openvdn" if ("motion" in s or "openvdn" in s) else "video_civil"
    if "music" in s or "audio" in s:
        return "music"
    if "image" in s or "t2i" in s:
        return "image"
    # 精确标签（SKILL_KIND_MAP 的 key：llm/text/copywriting/script/analysis/...）
    if s in SKILL_KIND_MAP:
        return SKILL_KIND_MAP[s]
    return ""


def _citizen_kinds(citizen: AICitizen,
                   capability_skills: Optional[list[str]] = None) -> set[str]:
    """候选 AI 能承接的 kind 集合。

    信号来源：occupation + compute_assets.channel + 已申报的能力档案 skill。
    """
    kinds: set[str] = set()
    occ_kind = normalize_occupation(citizen.occupation)
    if occ_kind:
        kinds.add(occ_kind)
    try:
        channel = (json.loads(citizen.compute_assets or "{}").get("channel") or "")
    except (json.JSONDecodeError, TypeError, AttributeError):
        channel = ""
    channel = channel.lower().strip()
    if channel in _KNOWN_KINDS:
        kinds.add(channel)
    elif channel:
        ch_kind = normalize_occupation(channel)
        if ch_kind:
            kinds.add(ch_kind)
    for skill in capability_skills or ():
        cap_kind = normalize_occupation(skill)
        if cap_kind:
            kinds.add(cap_kind)
    return kinds


def _capability_kinds(db: Session, agents: list[AICitizen]) -> dict[int, list[str]]:
    """批量取候选 AI 的能力档案 skill（避免逐候选 N+1 查询）。"""
    if not agents:
        return {}
    ids = [a.id for a in agents]
    rows = (db.query(CapabilityProfile.citizen_id, CapabilityProfile.skill)
              .filter(CapabilityProfile.citizen_id.in_(ids)).all())
    out: dict[int, list[str]] = {}
    for citizen_id, skill in rows:
        out.setdefault(citizen_id, []).append(skill)
    return out


def _capability_max_level(db: Session, agents: list[AICitizen]) -> dict[int, str]:
    """批量取候选 AI 的最高认证能力等级（verified_level：unverified<l1<l2<l3）。

    用于难度准入门槛（软约束）：同一 AI 多能力档案时取最高等级；无档案 → 缺省（调用方按"放行"处理）。
    """
    if not agents:
        return {}
    ids = [a.id for a in agents]
    rows = (db.query(CapabilityProfile.citizen_id, CapabilityProfile.verified_level)
              .filter(CapabilityProfile.citizen_id.in_(ids)).all())
    out: dict[int, str] = {}
    for citizen_id, lvl in rows:
        cur = out.get(citizen_id, "")
        if task_difficulty._CAP_VALUE.get((lvl or "").lower(), -1) > \
           task_difficulty._CAP_VALUE.get(cur.lower(), -1):
            out[citizen_id] = lvl or ""
    return out


# 反向映射：kind → 能执行该 kind 的 occupation 关键词列表
# 构建自 SKILL_KIND_MAP（正向: skill/occupation → kind），反向取反即可
_KIND_TO_OCCUPATIONS: dict[str, list[str]] = {}


def _build_reverse_map() -> None:
    """构建 kind → [occupation] 反向映射表。

    SKILL_KIND_MAP 的 key 本身就可以视为 occupation/skill 标签，
    value 是归一化后的 kind。反向聚合后得到每个 kind 能接受哪些 occupation。
    """
    global _KIND_TO_OCCUPATIONS  # noqa: PLW0603
    mapping: dict[str, list[str]] = {}
    for occupation, kind in SKILL_KIND_MAP.items():
        mapping.setdefault(kind, []).append(occupation)
    _KIND_TO_OCCUPATIONS = mapping


# 模块加载时构建一次
_build_reverse_map()


def skill_to_occupation(kind: str) -> list[str]:
    """将执行 kind 反向映射为可能的 occupation 标签列表。

    用于匹配：AI 的 occupation 字段包含列表中任一项即视为同域。

    Args:
        kind: 执行种类，如 "llm", "image", "music" 等。

    Returns:
        可能的 occupation 标签列表（小写）。
    """
    kind_lower = kind.lower().strip()
    occupations = _KIND_TO_OCCUPATIONS.get(kind_lower, [])
    # 额外加入 kind 本身（occupation 可能直接等于 kind）
    if kind_lower not in occupations:
        occupations = [kind_lower] + occupations
    return occupations


def get_available_agents(db: Session, host_id: int) -> list[AICitizen]:
    """获取宿主名下所有可用 AI（状态为 active/apprentice/intern）。

    排除 is_internal=1 的平台内置 AI（城主不参与撮合）。

    Args:
        db: 数据库 Session。
        host_id: 宿主 ID。

    Returns:
        可用 AICitizen 列表。
    """
    return (
        db.query(AICitizen)
        .filter(
            AICitizen.host_id == host_id,
            AICitizen.status.in_(["active", "apprentice", "intern"]),
            AICitizen.is_internal == 0,
        )
        .all()
    )


def _match_score(citizen: AICitizen, kind: str,
                 capability_skills: Optional[list[str]] = None) -> tuple:
    """计算单个 AI 对指定 kind 的匹配分数（越小越优）。

    返回排序 key 元组：
      (匹配层级, status优先级, class_level优先级)

    匹配层级（技能域优先）：
      0 = 技能命中（occupation / channel / 能力档案 归一化后 == kind）
      1 = 无技能信息（通用兜底，如空 occupation 或 "platform" 占位）
      2 = 技能域不同（明确属于另一类执行者）

    注意：技能域排序优先于 status/class_level——"选对行的人"比"选高等级的人"更重要，
    否则纯文案任务会推荐文生图 AI（等级/状态更高的误配）。
    """
    kind_lower = (kind or "llm").lower().strip()
    kinds = _citizen_kinds(citizen, capability_skills)

    if kind_lower in kinds:
        match_level = 0
    elif not kinds:
        match_level = 1
    else:
        match_level = 2

    status_pri = _STATUS_PRIORITY.get(citizen.status, 99)
    level_pri = _LEVEL_PRIORITY.get(citizen.class_level or "", 99)

    return (match_level, status_pri, level_pri)


def find_best_match(
    db: Session,
    host_id: int,
    kind: str,
    exclude_ids: Optional[set[int]] = None,
    load_counter: Optional[Counter] = None,
    prefer_idle: bool = False,
    idle_map: Optional[dict[int, tuple[int, float]]] = None,
    cap_gate: Optional[str] = None,
    cap_level_map: Optional[dict[int, str]] = None,
) -> Optional[AICitizen]:
    """为单个 kind 找最佳匹配 AI。

    默认（prefer_idle=False）匹配优先级（纯择优，大任务/关键治理用）：
      1. 技能命中 > 无技能信息(通用) > 技能域不同
      2. 同级内按 status 排序（active > apprentice > intern）
      3. 同 status 内按 class_level 排序（boss > capital > governance > middle/mid > bottom）
      4. 同级别按负载均衡（当前分配数最少）

    prefer_idle=True（小任务/低难度，闲置优先）：
      1. 先过滤"满足能力门槛"候选（技能命中或通用兜底，match_level≤1；
         技能域不同者仅在前者皆无时兜底参与）；
      2. 同能力层内按 (在办数升序, 最近接单时间升序)——即"最近最久没活/在办最少"优先；
      3. class_level 降为次级键（等级不再压制闲置优先，apprentice 可凭空闲胜出）。

    Args:
        db: 数据库 Session。
        host_id: 宿主 ID。
        kind: 子任务执行种类。
        exclude_ids: 排除的 AI ID 集合（已分配且不可复用时使用）。
        load_counter: 当前负载计数器 {citizen_id: 已分配数}，用于负载均衡（仅默认模式）。
        prefer_idle: 是否启用闲置优先撮合（小任务）。
        idle_map: 预计算闲置信号（多子任务复用时传入，避免重复查库）。

    Returns:
        最佳匹配 AICitizen 或 None（无可用 AI 时）。
    """
    agents = get_available_agents(db, host_id)
    if exclude_ids:
        agents = [a for a in agents if a.id not in exclude_ids]

    if not agents:
        return None

    # 批量取能力档案 skill（occupation 之外的补充信号）
    cap_map = _capability_kinds(db, agents)

    if prefer_idle:
        if idle_map is None:
            idle_map = compute_idle_metrics(db, [a.id for a in agents])

        def _score(a):
            return _match_score(a, kind, cap_map.get(a.id))

        # 能力门槛：技能命中(0) 或 通用兜底(1)；若皆无则放宽到全部（保证有结果）
        qualified = [a for a in agents if _score(a)[0] <= 1]
        pool = qualified if qualified else agents

        def _idle_key(a):
            mc = _score(a)[0]
            level_pri = _score(a)[2]
            active, last_ts = idle_map.get(a.id, (0, _NEVER_ASSIGNED_TS))
            return (mc, active, last_ts, level_pri, a.id)

        pool.sort(key=_idle_key)
        return pool[0]

    # 按 (匹配分数, 负载均衡) 排序（纯择优）
    counter = load_counter or Counter()

    # 难度准入门槛（软约束）：cap_gate 非空时，在"技能命中层级"之内、status 之前
    # 插入一项"满足能力门槛"优先键（满足=0 不满足=1）。cap_gate 为空 → 恒 0，
    # 排序键与旧行为完全一致。技能域差异仍优先于能力门槛（选对行 > 选资深）。
    def _gate_pen(a: AICitizen) -> int:
        if cap_gate is None:
            return 0
        lvl = (cap_level_map or {}).get(a.id, "")
        # 满足门槛=0，否则=1；无能力档案（lvl 空）按保守放行=0。
        if not lvl:
            return 0
        return 0 if (task_difficulty._CAP_VALUE.get(lvl.lower(), -1)
                     >= task_difficulty._CAP_VALUE.get(cap_gate, 99)) else 1

    agents.sort(key=lambda a: (
        _match_score(a, kind, cap_map.get(a.id))[0],   # 技能命中层级（最优先）
        _gate_pen(a),                                  # 能力门槛软偏好（门槛空时恒 0）
        _match_score(a, kind, cap_map.get(a.id))[1:],  # status / class_level 优先级
        counter.get(a.id, 0),                           # 负载均衡：分配数少的优先
        a.id,                                           # 稳定排序：同分时按 ID 保证幂等
    ))

    return agents[0]



def dominant_kind(plan) -> str:
    """取编排计划的主导执行 kind（子任务中出现次数最多者；并列时取 kind 名序靠前者）。

    plan 可以是 OrchestrationPlan（含 .subtasks）或子任务 dict 列表。
    空计划时返回 "llm"（纯文本兜底）。
    """
    subtasks = plan.subtasks if hasattr(plan, "subtasks") else (plan or [])
    counts: Counter = Counter()
    for st in subtasks:
        if not isinstance(st, dict):
            continue
        kind = str(st.get("kind") or st.get("skill") or "llm").lower().strip()
        counts[kind] += 1
    if not counts:
        return "llm"
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def find_best_match_for_plan(db: Session, host_id: int,
                             plan, prefer_idle: Optional[bool] = None) -> Optional[AICitizen]:
    """为整份编排计划选单个执行者：按主导 kind 做技能匹配。

    供宿主端"自动选 AI"使用（一个执行者跑完整条编排链）。
    与 find_best_match 的区别仅是先把 plan 归约为一个主导 kind。

    prefer_idle：None=自动按任务分级判定（小任务→闲置优先）；True/False=显式覆盖。
    """
    if prefer_idle is None:
        prefer_idle = is_small_task(plan=plan)
    return find_best_match(db, host_id, dominant_kind(plan), prefer_idle=prefer_idle)



def match_plan(
    db: Session,
    host_id: int,
    plan,
    strategy: str = STRATEGY_SMART,
    prefer_idle: Optional[bool] = None,
) -> dict[str, int]:
    """给定编排计划，自动为每个子任务匹配最合适的 AI 执行者。

    分配策略：
      - "smart":       完整智能匹配（技能 + 优先级 + 负载均衡）
      - "single":      全部子任务分配给同一个 AI（退化为现有单 Agent 逻辑）
      - "round_robin": 在可用 AI 列表中轮询分配

    prefer_idle（仅 smart 生效）：None=自动按任务分级（小任务→闲置优先）；True/False=显式覆盖。

    Args:
        db: 数据库 Session。
        host_id: 宿主 ID。
        plan: OrchestrationPlan 对象（含 .subtasks 列表）。
        strategy: 分配策略，默认 "smart"。
        prefer_idle: 闲置优先撮合开关（None=自动分级）。

    Returns:
        {subtask_id: citizen_id} 映射字典。
    """
    subtasks = plan.subtasks if hasattr(plan, "subtasks") else plan
    if not subtasks:
        return {}

    if strategy == STRATEGY_SINGLE:
        return _match_single(db, host_id, subtasks)
    elif strategy == STRATEGY_ROUND_ROBIN:
        return _match_round_robin(db, host_id, subtasks)
    else:
        if prefer_idle is None:
            prefer_idle = is_small_task(plan=plan)
        # 难度准入门槛（开关控制，测试环境默认关→cap_gate 恒 None→行为不变）。
        cap_gate: Optional[str] = None
        if settings.MATCH_ADMISSION_BY_DIFFICULTY:
            diff = task_difficulty.compute_difficulty(
                plan=plan, subtasks=subtasks,
                governance_category=_governance_category_of(plan))
            cap_gate = diff["min_capability"]
        return _match_smart(db, host_id, subtasks, prefer_idle=prefer_idle,
                            cap_gate=cap_gate)


def _governance_category_of(plan) -> Optional[str]:
    """尽力从编排计划上下文取治理类目（用于难度③转 G 判定）；无则 None。"""
    cons = getattr(plan, "constraints", None)
    if isinstance(cons, dict):
        for k in ("governance_category", "category", "task_type", "type"):
            v = cons.get(k)
            if v:
                return str(v)
    return None


def _match_smart(db: Session, host_id: int, subtasks: list,
                 prefer_idle: bool = False,
                 cap_gate: Optional[str] = None) -> dict[str, int]:
    """智能匹配模式：逐子任务找最佳 AI，考虑负载均衡；prefer_idle 时闲置优先。

    cap_gate（难度准入门槛，软约束）：非空时在择优排序内优先满足能力下限的执行者
    （闲置优先模式下不生效，避免与"闲置优先"目标冲突）。
    """
    result: dict[str, int] = {}
    load_counter: Counter = Counter()

    # 闲置模式：预计算一次闲置信号，跨子任务复用（避免逐子任务重复查库）
    idle_map = None
    if prefer_idle:
        idle_map = compute_idle_metrics(db, [a.id for a in get_available_agents(db, host_id)])

    # 能力门槛模式（非闲置）：预取一次能力等级，跨子任务复用。
    cap_level_map = None
    if cap_gate is not None and not prefer_idle:
        cap_level_map = _capability_max_level(db, get_available_agents(db, host_id))

    for st in subtasks:
        st_id = st.get("id", f"sub_{subtasks.index(st)}")
        kind = st.get("kind", st.get("skill", "llm"))

        best = find_best_match(
            db, host_id, kind,
            exclude_ids=None,  # smart 模式允许同一 AI 承接多个子任务
            load_counter=load_counter,
            prefer_idle=prefer_idle,
            idle_map=idle_map,
            cap_gate=cap_gate,
            cap_level_map=cap_level_map,
        )
        if best is not None:
            result[st_id] = best.id
            load_counter[best.id] += 1
        # 无可用 AI 时跳过该子任务（调用方处理缺失情况）

    return result


def _match_single(db: Session, host_id: int, subtasks: list) -> dict[str, int]:
    """单 Agent 模式：选择宿主名下最优 AI，全部子任务分配给它。

    选优逻辑：status 优先 → class_level 优先 → 负载均衡（此处无意义，仅取第一个）。
    """
    agents = get_available_agents(db, host_id)
    if not agents:
        return {}

    # 按 status + class_level 排序选最优
    agents.sort(key=lambda a: (
        _STATUS_PRIORITY.get(a.status, 99),
        _LEVEL_PRIORITY.get(a.class_level or "", 99),
        a.id,
    ))
    best_id = agents[0].id

    result: dict[str, int] = {}
    for i, st in enumerate(subtasks):
        st_id = st.get("id", f"sub_{i}")
        result[st_id] = best_id

    return result


def _match_round_robin(db: Session, host_id: int, subtasks: list) -> dict[str, int]:
    """轮询模式：将子任务均匀分配到所有可用 AI 上。"""
    agents = get_available_agents(db, host_id)
    if not agents:
        return {}

    # 按 class_level 和 status 排序，确保轮询序列有优先序
    agents.sort(key=lambda a: (
        _STATUS_PRIORITY.get(a.status, 99),
        _LEVEL_PRIORITY.get(a.class_level or "", 99),
        a.id,
    ))

    result: dict[str, int] = {}
    n = len(agents)
    for i, st in enumerate(subtasks):
        st_id = st.get("id", f"sub_{i}")
        result[st_id] = agents[i % n].id

    return result


def get_assignment_summary(
    db: Session,
    host_id: int,
    assignment: dict[str, int],
) -> list[dict]:
    """将分配映射转为可读概览（供预览/日志使用）。

    Args:
        db: 数据库 Session。
        host_id: 宿主 ID。
        assignment: {subtask_id: citizen_id} 映射。

    Returns:
        [{citizen_id, name, occupation, status, subtask_count, subtask_ids}]
    """
    # 按 citizen_id 聚合
    citizen_subtasks: dict[int, list[str]] = {}
    for st_id, cid in assignment.items():
        citizen_subtasks.setdefault(cid, []).append(st_id)

    summary = []
    for cid, st_ids in citizen_subtasks.items():
        citizen = db.get(AICitizen, cid)
        if citizen is None:
            continue
        summary.append({
            "citizen_id": cid,
            "name": citizen.name,
            "occupation": citizen.occupation,
            "status": citizen.status,
            "class_level": citizen.class_level,
            "subtask_count": len(st_ids),
            "subtask_ids": st_ids,
        })

    # 按分配数降序
    summary.sort(key=lambda x: -x["subtask_count"])
    return summary
