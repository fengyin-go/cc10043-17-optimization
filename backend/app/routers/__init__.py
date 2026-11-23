"""业务路由汇总：续采流转专用路由先注册，再注册通用资源路由，避免固定路径被动态路径抢占。"""
from __future__ import annotations

from app.routers import resource, resume_flow

ROUTERS = [resume_flow, resource]
