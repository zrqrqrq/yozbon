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
"""Alembic 环境配置（G-01）。

使用项目 config.py 的 DB_URL 作为迁移数据库连接；
target_metadata 指向 models.Base.metadata，支持 autogenerate。
"""
import sys
from pathlib import Path

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

# 添加项目根到 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.database import Base, engine as app_engine
from app import models  # noqa: F401 注册所有模型

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 使用 app.database 中实际的 engine URL（database.py 会将 sqlite 路径重映射到 data/ 目录）
_resolved_url = str(app_engine.url)
config.set_main_option("sqlalchemy.url", _resolved_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：生成 SQL 而不连接数据库。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=True,  # SQLite 兼容
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连接数据库执行迁移。

    直接使用 app.database 的 engine，确保与运行时的连接池/WAL 配置一致。
    """
    with app_engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=True,  # SQLite ALTER TABLE 兼容
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
