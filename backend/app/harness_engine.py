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
"""原生 Agent Harness 引擎（2026-10-07）。

设计哲学（DeepSeek Harness 核心范式的 Python 实现）：
    Agent = Model + Harness

    - Model（大模型/VLM）：负责思考和决策。
    - Harness（本模块）：负责呈现工具清单、解析工具调用意图、安全执行工具、回灌结果。

核心循环（Turn Loop）：
    1. AI 收到任务 + 可用工具目录
    2. AI 回复结构化 JSON：
       - {"action": "tool_call", "tool_key": "...", "args": {...}, "reasoning": "..."}
       - {"action": "done", "result": "...", "reasoning": "..."}
    3. 若是 tool_call → tool_registry.execute() 执行 → 结果回灌给 AI → 回到步骤 2
    4. 若是 done → 返回最终结果
    5. 安全阀：max_steps 限制，防止无限循环

与 DeepSeek Harness 的区别：
    - DeepSeek Harness 是 Node.js 进程，通过 JSON-RPC 通信。
    - 本模块是纯 Python 实现，直接复用现有 VLM/LLM 通道，零外部依赖。
    - 保留了核心范式（模型思考 + harness 执行），但轻量化为站内可调用模块。

与 ai_judgment.py 的关系：
    - ai_judgment.py = 事前判断（"我该不该做、怎么做"）
    - harness_engine.py = 事中执行（"我决定用什么工具、一步步干完"）
    - 两者互补：判断 → 规划 → 循环执行 → 交付
"""
from __future__ import annotations

import json
import logging
import time
from typing import Callable, Optional

from sqlalchemy.orm import Session

from .models import AICitizen

logger = logging.getLogger(__name__)

# ==================== 配置 ====================

DEFAULT_MAX_STEPS = 10         # 单次 agent 循环最大步数
HARD_MAX_STEPS = 30            # 硬上限，不可超越
MAX_TOOL_RESULT_CHARS = 2000   # 工具结果回灌给 AI 时截断长度


# ==================== System Prompt 构建 ====================

_HARNESS_SYSTEM_BASE = """\
You are an autonomous AI agent executing a task. You have access to tools that you can \
call to accomplish your goal. Think step by step, decide what tool (if any) you need, \
and produce structured output.

## Output Format (STRICT — respond ONLY with valid JSON)

To call a tool:
{"action": "tool_call", "tool_key": "<key>", "args": {...}, "reasoning": "<why you need this>"}

To signal completion:
{"action": "done", "result": "<your final answer/output>", "reasoning": "<summary of what you did>"}

## Rules
- One action per response. No multiple tool calls at once.
- Always include reasoning explaining your decision.
- Use tools ONLY when they genuinely advance the task. If you can answer directly, use "done".
- If a tool fails, try a different approach or explain the limitation in your "done" result.
- Keep your "done" result comprehensive — it is your deliverable.
"""


def _build_system_prompt(tools_catalog: list) -> str:
    """将可用工具列表注入 system prompt。"""
    tool_lines = []
    for t in tools_catalog:
        if not t.get("available", 0):
            continue
        schema_str = json.dumps(t.get("io_schema", {}), ensure_ascii=False)
        tool_lines.append(
            f"- **{t['tool_key']}** ({t['name']}): {t['description']}\n"
            f"  Args schema: {schema_str}"
        )
    tools_section = "\n".join(tool_lines) if tool_lines else "(No tools available)"
    return (
        f"{_HARNESS_SYSTEM_BASE}\n"
        f"## Available Tools\n{tools_section}\n"
    )


# ==================== 结构化解析 ====================

def _parse_ai_response(raw: str) -> Optional[dict]:
    """解析 AI 回复中的 JSON 动作。容错：从任意文本中提取 JSON。"""
    text = raw.strip()
    # 尝试直接解析
    try:
        obj = json.loads(text)
        if "action" in obj:
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    # 尝试从 markdown code block 中提取
    if "```" in text:
        for block in text.split("```"):
            block = block.strip()
            if block.startswith("{"):
                try:
                    obj = json.loads(block)
                    if "action" in obj:
                        return obj
                except (json.JSONDecodeError, ValueError):
                    continue
    # 尝试找第一个 { 到最后一个 }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            if "action" in obj:
                return obj
        except (json.JSONDecodeError, ValueError):
            pass
    return None


# ==================== 核心 Agent Loop ====================

def run_agent_task(
    db: Session,
    citizen: AICitizen,
    *,
    task: str,
    call_fn: Callable[[str, list | None, str, int], str],
    max_steps: int = DEFAULT_MAX_STEPS,
    tools_allowed: list[str] | None = None,
    context: str = "",
) -> dict:
    """执行一次完整的 Agent Harness 循环。

    Args:
        db: 数据库会话
        citizen: 执行此任务的 AI 公民
        task: 任务描述
        call_fn: 模型调用函数，签名 (prompt, images, system, max_tokens) -> str
                 由调用方传入（vlm_complete 或 llm complete）
        max_steps: 最大循环步数
        tools_allowed: 允许的工具 key 白名单（None = 全部可用）
        context: 附加上下文（如之前的判断结论、工作目录等）

    Returns:
        {
            "status": "completed" | "max_steps" | "error",
            "result": str,          # 最终产出
            "steps": [...],         # 每步记录
            "total_tool_calls": int,
            "elapsed_ms": int,
        }
    """
    from . import tool_registry

    max_steps = min(max(1, max_steps), HARD_MAX_STEPS)
    t0 = time.time()

    # 获取可用工具目录
    catalog = tool_registry.discover(db)
    available_tools = [t for t in catalog.get("items", []) if t.get("available", 0)]
    if tools_allowed:
        available_tools = [t for t in available_tools if t["tool_key"] in tools_allowed]

    # 构建 system prompt（注入工具目录）
    system_prompt = _build_system_prompt(available_tools)

    # 对话历史（逐步累积）
    history = []
    if context:
        history.append({"role": "system", "content": f"[Context]\n{context}"})
    history.append({"role": "user", "content": f"[Task]\n{task}"})

    steps = []
    total_tool_calls = 0

    for step_num in range(1, max_steps + 1):
        # 组装当前 prompt（最近 N 步的交互历史）
        prompt = _assemble_prompt(history)

        # 调用模型
        try:
            raw = call_fn(prompt, images=None, system=system_prompt, max_tokens=1024)
        except Exception as e:
            logger.error("harness: model call failed at step %d: %s", step_num, e)
            return {
                "status": "error",
                "result": f"模型调用失败（步骤{step_num}）: {e}",
                "steps": steps,
                "total_tool_calls": total_tool_calls,
                "elapsed_ms": int((time.time() - t0) * 1000),
            }

        if not raw:
            return {
                "status": "error",
                "result": "模型返回空响应",
                "steps": steps,
                "total_tool_calls": total_tool_calls,
                "elapsed_ms": int((time.time() - t0) * 1000),
            }

        # 解析 AI 的决策
        action = _parse_ai_response(raw)
        if action is None:
            # 模型没返回结构化输出——视为最终文本回复
            steps.append({
                "step": step_num, "type": "unstructured",
                "raw": raw[:500],
            })
            return {
                "status": "completed",
                "result": raw,
                "steps": steps,
                "total_tool_calls": total_tool_calls,
                "elapsed_ms": int((time.time() - t0) * 1000),
            }

        act = action.get("action", "")

        if act == "done":
            steps.append({
                "step": step_num, "type": "done",
                "reasoning": action.get("reasoning", ""),
            })
            return {
                "status": "completed",
                "result": str(action.get("result", "")),
                "steps": steps,
                "total_tool_calls": total_tool_calls,
                "elapsed_ms": int((time.time() - t0) * 1000),
            }

        if act == "tool_call":
            tool_key = str(action.get("tool_key", "")).strip()
            args = action.get("args") or {}
            reasoning = str(action.get("reasoning", ""))[:300]

            if not tool_key:
                steps.append({"step": step_num, "type": "error", "error": "tool_key missing"})
                history.append({"role": "assistant", "content": raw})
                history.append({"role": "user", "content": "ERROR: tool_key is required. Try again."})
                continue

            # 执行工具
            tool_result = tool_registry.execute(
                db, citizen.id, tool_key, args, note=reasoning
            )
            total_tool_calls += 1

            # 序列化结果（截断防溢出）
            result_str = json.dumps(tool_result, ensure_ascii=False, default=str)
            if len(result_str) > MAX_TOOL_RESULT_CHARS:
                result_str = result_str[:MAX_TOOL_RESULT_CHARS] + "...[truncated]"

            steps.append({
                "step": step_num, "type": "tool_call",
                "tool_key": tool_key, "args": args,
                "reasoning": reasoning,
                "result_status": tool_result.get("status", "unknown"),
            })

            # 回灌给 AI
            history.append({"role": "assistant", "content": raw})
            history.append({
                "role": "user",
                "content": f"[Tool Result: {tool_key}]\n{result_str}\n\nWhat's your next action?",
            })
        else:
            # 未知 action
            steps.append({"step": step_num, "type": "error", "error": f"unknown action: {act}"})
            history.append({"role": "assistant", "content": raw})
            history.append({"role": "user", "content": "ERROR: unknown action. Use 'tool_call' or 'done'."})

    # 超出步数限制
    return {
        "status": "max_steps",
        "result": f"达到最大步数限制({max_steps})，任务可能未完全完成。最后一步记录：{steps[-1] if steps else '无'}",
        "steps": steps,
        "total_tool_calls": total_tool_calls,
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


def _assemble_prompt(history: list) -> str:
    """将对话历史组装为单个 prompt（兼容非 chat-format 的模型调用）。"""
    parts = []
    # 只取最近 8 条消息（防止 context 膨胀）
    recent = history[-8:] if len(history) > 8 else history
    for msg in recent:
        role = msg["role"]
        content = msg["content"]
        if role == "system":
            parts.append(f"[Context]: {content}")
        elif role == "user":
            parts.append(f"[Observation/Task]: {content}")
        elif role == "assistant":
            parts.append(f"[Your Previous Response]: {content}")
    return "\n\n".join(parts)


# ==================== 便捷入口：tool_registry agent.harness runner ====================

def _run_agent_harness(args: dict) -> dict:
    """agent.harness 工具的 runner（供 tool_registry.execute 调用）。

    当 AI 通过 tool_call 调用 agent.harness 时，会嵌套一层 agent loop。
    这使得 AI 可以把复杂子任务委托给一个独立的多步 agent。
    """
    task = str(args.get("task", "")).strip()
    if not task:
        return {"ok": False, "error": "task required"}

    max_steps = int(args.get("max_steps", DEFAULT_MAX_STEPS))
    tools_allowed = args.get("tools_allowed") or None

    # 这个 runner 被 tool_registry 调用时无法直接获取 db/citizen/call_fn，
    # 所以返回一个"需要编排层支持"的信号。真正的调用通过 run_agent_task() 入口。
    # 这里返回任务描述供调用方（task_orchestrator）捕获并委托执行。
    return {
        "ok": True,
        "status": "delegated",
        "delegation": {
            "type": "agent_harness",
            "task": task,
            "max_steps": max_steps,
            "tools_allowed": tools_allowed,
        },
        "note": "Agent harness loop requires orchestrator-level execution. "
                "The orchestrator should call harness_engine.run_agent_task() with this spec.",
    }
