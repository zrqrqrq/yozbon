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
"""T1 发布细化七要素校验（M8；契约 §9.3.1）。

七要素：goal/scope/deliverable_std/acceptance_criteria/deadline/budget/limits。
五核心硬门槛：goal/scope/deliverable_std/deadline/budget。

兼容策略（保护既有发包测试，老格式只带 title+budget_cent）：
- 老格式（未传 requirements）：从 title→goal、budget_cent→budget 推导；
  scope/deliverable_std/deadline 视为节点级要素（后续 submit_nodes 填写），软通过不硬拒。
- 新格式（显式传 requirements dict）：合并推导后校验五核心，任一缺失 → T1Error(400)。
"""

# 五核心要素（任一缺失即拒受理）
CORE_FIELDS = ("goal", "scope", "deliverable_std", "deadline", "budget")


class T1Error(Exception):
    """T1 需求细化校验业务异常（路由层映射 HTTP 400）。"""


def _s(v) -> str:
    return str(v or "").strip()


def normalize_requirements(title: str, budget_cent, requirements) -> dict:
    """把 (title, budget_cent, requirements) 归一成七要素 dict。"""
    req = requirements if isinstance(requirements, dict) else {}
    return {
        "goal": _s(req.get("goal")) or _s(title),
        "scope": _s(req.get("scope")),
        "deliverable_std": _s(req.get("deliverable_std")),
        "acceptance_criteria": _s(req.get("acceptance_criteria")),
        "deadline": _s(req.get("deadline")),
        "budget": _s(req.get("budget")) or _s(budget_cent),
        "limits": _s(req.get("limits")),
    }


def validate_requirements(title: str, budget_cent, requirements) -> dict:
    """校验并返回归一后的七要素 dict。

    - requirements 为 None（老格式）→ 软通过（节点级要素后补）；
    - requirements 显式给出（含空 dict）→ 硬校验五核心，缺失即 T1Error。
    """
    norm = normalize_requirements(title, budget_cent, requirements)
    if requirements is None:
        return norm
    missing = [f for f in CORE_FIELDS if not norm.get(f)]
    if missing:
        raise T1Error(
            "Requirement is incomplete; refine per the T1 seven elements. Missing core elements: " + ", ".join(missing))
    return norm
