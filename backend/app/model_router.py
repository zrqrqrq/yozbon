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
"""智能模型路由器（工位系统"调度面板"）。

定位：为 task_orchestrator 和外部调用方提供"任务特征 → 最优执行模型/通道"
的决策支持，是 model_fallback 引擎之上的高层路由策略。

与现有模块关系（只读调用，不修改）：
  - model_fallback：底层降级链引擎（resolve/report_failure），本模块是其调用方；
  - platform_compute：底层执行通道，本模块只做"选哪条路"决策，不做实际执行；
  - task_orchestrator：本模块主要消费方，用于子任务级智能路由。

路由策略（MVP）：
  1. 按 task_type（kind）查 model_fallback 规则 → 有规则走规则链；
  2. 无规则 → 按内置 SKILL_DEFAULTS 返回默认通道；
  3. 可选：按 citizen 能力等级做"高低搭配"（高级 Agent 用高质量模型，
     实习/见习用经济模型）——MVP 暂不实现，预留接口。
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from .model_fallback import model_fallback

# ---------------- 默认路由表（无 fallback 规则时的兜底映射） ----------------

SKILL_DEFAULTS = {
    "llm": {
        "primary": "qwen/qwen3.8-flash-next",
        "chain": ["gpt-4o-mini", "claude-3-haiku"],
        "description": "General text generation (copywriting/script/analysis/translation)",
    },
    "image": {
        "primary": "ZImage_T2I",
        "chain": [],
        "description": "Standard-resolution text-to-image",
    },
    "hd_image": {
        "primary": "ZImage_T2I_HD_2Stage",
        "chain": ["ZImage_T2I"],
        "description": "High-resolution two-stage text-to-image",
    },
    "img2img": {
        "primary": "ZImage_img2img_denoise",
        "chain": [],
        "description": "Image-to-image / denoise / style transfer",
    },
    "music": {
        "primary": "MiniMaxMusic3",
        "chain": [],
        "description": "AI music generation",
    },
    "video_civil": {
        "primary": "H3_video_civil",
        "chain": ["H3_video_openvdn"],
        "description": "Photorealistic video generation",
    },
    "video_openvdn": {
        "primary": "H3_video_openvdn",
        "chain": ["H3_video_civil"],
        "description": "Anime-style video generation",
    },
}


def resolve_route(db: Session, task_kind: str,
                  citizen_level: str = "") -> dict:
    """根据任务类型返回最优执行路由。

    策略优先级：
    1. 查 model_fallback 规则（如果注册过该 kind 的降级规则）；
    2. 回退内置默认路由。

    Args:
        db: Session。
        task_kind: 执行 kind（image/hd_image/.../llm）。
        citizen_level: 可选的公民能力等级（预留高低搭配）。

    Returns:
        {"kind": str, "primary_model": str, "fallback_chain": list,
         "source": "fallback_rule"|"default", "rule_id": int}
    """
    # 1. 尝试从 model_fallback 引擎获取
    result = model_fallback.resolve(db, task_kind)
    if result["rule_id"] > 0:
        return {
            "kind": task_kind,
            "primary_model": result["primary"],
            "fallback_chain": result["chain"],
            "source": "fallback_rule",
            "rule_id": result["rule_id"],
        }

    # 2. 回退内置默认
    default = SKILL_DEFAULTS.get(task_kind, SKILL_DEFAULTS["llm"])
    return {
        "kind": task_kind,
        "primary_model": default["primary"],
        "fallback_chain": default["chain"],
        "source": "default",
        "rule_id": 0,
        "description": default.get("description", ""),
    }


def batch_route(db: Session, kinds: list) -> list:
    """批量路由查询（工位系统批量分配子任务时使用）。

    Args:
        db: Session。
        kinds: 需要路由的 kind 列表。

    Returns:
        路由结果列表，与输入顺序一致。
    """
    return [resolve_route(db, k) for k in kinds]


def report_execution_result(db: Session, task_kind: str,
                            model_used: str, success: bool):
    """回报执行结果，驱动 model_fallback 的降级决策。

    调用方（通常是 task_orchestrator 的子任务执行后）反馈成功/失败，
    本函数自动更新失败计数。

    Args:
        db: Session。
        task_kind: 任务类型。
        model_used: 实际使用的模型标识。
        success: 执行是否成功。
    """
    result = model_fallback.resolve(db, task_kind)
    rule_id = result["rule_id"]
    if rule_id <= 0:
        return  # 无注册的规则，无需记录

    if success:
        model_fallback.report_success(db, rule_id, model_used)
    else:
        model_fallback.report_failure(db, rule_id, model_used)


def available_capabilities(db: Session) -> list:
    """返回当前平台所有可用能力（供工位系统前端展示）。

    综合 platform_compute.KINDS + model_fallback 已注册的规则，
    给出"这个平台能做什么"的全景视图。
    """
    from .platform_compute import KINDS, LLM_KIND

    capabilities = []

    # 媒体链路
    for kind, (_, node_key) in KINDS.items():
        default = SKILL_DEFAULTS.get(kind, {})
        capabilities.append({
            "kind": kind,
            "type": "media",
            "model": default.get("primary", node_key),
            "description": default.get("description", kind),
        })

    # LLM
    llm_route = resolve_route(db, LLM_KIND)
    capabilities.append({
        "kind": LLM_KIND,
        "type": "text",
        "model": llm_route["primary_model"],
        "description": SKILL_DEFAULTS["llm"]["description"],
    })

    return capabilities
