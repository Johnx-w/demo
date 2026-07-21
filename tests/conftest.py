"""共享 fixtures：对齐 Schema 种子用户与 API 鉴权约定。"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app

# 对齐 docs/sql/001_schema_picking_inventory.sql 种子
USER_SYSTEM = 1
USER_OPS = 2
USER_PICKER = 3
USER_SUPERVISOR = 4

WAREHOUSE_ID = 1
SKU_ID = 10
SKU_ID_B = 11
SUBSTITUTE_SKU_ID = 99


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def auth_headers(
    user_id: int,
    *,
    action_id: str | None = None,
    request_id: str | None = None,
) -> dict[str, str]:
    headers = {"X-User-Id": str(user_id)}
    if action_id is not None:
        headers["Idempotency-Key"] = action_id
    if request_id is not None:
        headers["X-Request-Id"] = request_id
    return headers


def new_action_id(prefix: str = "act") -> str:
    return f"{prefix}-{uuid.uuid4()}"


def import_payload(
    *,
    action_id: str | None = None,
    external_order_no: str = "SO-20260721-001",
    warehouse_id: int = WAREHOUSE_ID,
    lines: list[dict[str, Any]] | None = None,
    sku_id: int = SKU_ID,
    qty: str = "2.0000",
) -> dict[str, Any]:
    if lines is None:
        lines = [{"line_no": 1, "sku_id": sku_id, "qty": qty}]
    return {
        "action_id": action_id or new_action_id("import"),
        "external_order_no": external_order_no,
        "warehouse_id": warehouse_id,
        "lines": lines,
    }


def d(value: str | float | Decimal) -> Decimal:
    return Decimal(str(value))


def apply_inventory_scenario(scenario: str, *, task: dict[str, Any], **kwargs: Any) -> None:
    """
    调整库存/任务前置，供 complete 用例构造边界。

    实现阶段提供 `app.testing.apply_inventory_scenario`；
    未提供时抛出明确错误，避免静默用错库存状态。
    """
    try:
        from app.testing import apply_inventory_scenario as _apply
    except ImportError as exc:  # pragma: no cover - TDD 红灯阶段
        raise RuntimeError(
            "需要 app.testing.apply_inventory_scenario 才能构造库存前置；"
            f"scenario={scenario!r}"
        ) from exc
    _apply(scenario, task=task, **kwargs)


def snapshot_balances(client: TestClient, *, warehouse_id: int, sku_ids: list[int]) -> dict[int, dict]:
    """读取库存快照；实现阶段提供 app.testing.get_balances。"""
    try:
        from app.testing import get_balances
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("需要 app.testing.get_balances") from exc
    return get_balances(warehouse_id=warehouse_id, sku_ids=sku_ids)


def get_task_row(client: TestClient, task_id: int) -> dict[str, Any]:
    try:
        from app.testing import get_task
    except ImportError:
        resp = client.get(
            f"/api/v1/picking-tasks/{task_id}",
            headers=auth_headers(USER_PICKER),
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["task"]
    return get_task(task_id)


def get_order_row(client: TestClient, order_id: int) -> dict[str, Any]:
    try:
        from app.testing import get_order
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("需要 app.testing.get_order 或详情接口带 order") from exc
    return get_order(order_id)


def get_timeout_job(client: TestClient, task_id: int) -> dict[str, Any]:
    try:
        from app.testing import get_timeout_job
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("需要 app.testing.get_timeout_job") from exc
    return get_timeout_job(task_id)


def list_ledgers(client: TestClient, *, task_id: int) -> list[dict[str, Any]]:
    try:
        from app.testing import list_ledgers_for_task
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("需要 app.testing.list_ledgers_for_task") from exc
    return list_ledgers_for_task(task_id)


def list_actions(client: TestClient, *, task_id: int) -> list[dict[str, Any]]:
    try:
        from app.testing import list_actions_for_task
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("需要 app.testing.list_actions_for_task") from exc
    return list_actions_for_task(task_id)


def seed_pending_pick(
    client: TestClient,
    *,
    order_no: str,
    lines: list[dict[str, Any]] | None = None,
    qty: str = "2.0000",
) -> dict[str, Any]:
    """导入成功 → PENDING_PICK + held（期望 HTTP 201）。"""
    action_id = new_action_id("seed-import")
    payload = import_payload(
        action_id=action_id,
        external_order_no=order_no,
        lines=lines,
        qty=qty,
    )
    resp = client.post(
        "/api/v1/order-imports",
        json=payload,
        headers=auth_headers(USER_OPS, action_id=action_id),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["task"]["status"] == "PENDING_PICK"
    assert body["task"]["reservation_held"] is True
    return body
