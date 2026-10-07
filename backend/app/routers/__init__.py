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
"""路由自动发现：各线将 router 文件丢进本目录即自动注册（无需改 main.py/__init__.py）。

约定：每个模块定义模块级 `router = APIRouter(...)`；无 router 的模块被跳过。
"""
import importlib
import pkgutil

all_routers = []
for _m in pkgutil.iter_modules(__path__):
    if _m.name == "__init__":
        continue
    mod = importlib.import_module(f"{__name__}.{_m.name}")
    if hasattr(mod, "router"):
        all_routers.append(mod.router)
