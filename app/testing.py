"""
测试夹具契约（实现阶段填充）。

tests/conftest.py 与 test_picking_complete.py 依赖本模块构造库存/任务前置并读库断言。
当前为占位：调用即抛 NotImplementedError，保持 TDD 红灯语义清晰。
"""

from __future__ import annotations

from typing import Any


def apply_inventory_scenario(scenario: str, *, task: dict[str, Any], **kwargs: Any) -> None:
    """
    支持的 scenario（与测设 TC 对齐）：

    - effective_short / effective_equals_demand / effective_available_zero
    - raw_low_effective_ok
    - multi_sku_partial_short
    - locked_makes_effective_short
    - reservation_held_zero
    - delete_inventory_balance
    - set_task_status
    """
    raise NotImplementedError(f"apply_inventory_scenario({scenario!r}) not implemented")


def get_balances(*, warehouse_id: int, sku_ids: list[int]) -> dict[int, dict]:
    raise NotImplementedError("get_balances not implemented")


def get_task(task_id: int) -> dict[str, Any]:
    raise NotImplementedError("get_task not implemented")


def get_order(order_id: int) -> dict[str, Any]:
    raise NotImplementedError("get_order not implemented")


def get_timeout_job(task_id: int) -> dict[str, Any]:
    raise NotImplementedError("get_timeout_job not implemented")


def list_ledgers_for_task(task_id: int) -> list[dict[str, Any]]:
    raise NotImplementedError("list_ledgers_for_task not implemented")


def list_actions_for_task(task_id: int) -> list[dict[str, Any]]:
    raise NotImplementedError("list_actions_for_task not implemented")
