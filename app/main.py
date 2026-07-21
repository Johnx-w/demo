"""FastAPI 入口：TDD 红灯阶段仅注册路由壳，业务未实现。"""

from __future__ import annotations

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="picking-inventory-demo", version="0.1.0")


def _not_implemented(feature: str) -> JSONResponse:
    return JSONResponse(
        status_code=501,
        content={
            "result": "FAILED",
            "reason_code": "NOT_IMPLEMENTED",
            "message": f"{feature} not implemented yet (TDD red)",
        },
    )


@app.post("/api/v1/order-imports")
async def order_imports(
    request: Request,
    x_user_id: int | None = Header(default=None, alias="X-User-Id"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
):
    _ = (request, x_user_id, idempotency_key, x_request_id)
    return _not_implemented("POST /api/v1/order-imports")


@app.get("/api/v1/picking-tasks")
async def list_picking_tasks(
    x_user_id: int | None = Header(default=None, alias="X-User-Id"),
):
    _ = x_user_id
    return _not_implemented("GET /api/v1/picking-tasks")


@app.get("/api/v1/picking-tasks/{task_id}")
async def get_picking_task(
    task_id: int,
    x_user_id: int | None = Header(default=None, alias="X-User-Id"),
):
    _ = (task_id, x_user_id)
    return _not_implemented("GET /api/v1/picking-tasks/{task_id}")


@app.post("/api/v1/picking-tasks/{task_id}/complete")
async def complete_picking_task(
    task_id: int,
    request: Request,
    x_user_id: int | None = Header(default=None, alias="X-User-Id"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    _ = (task_id, request, x_user_id, idempotency_key)
    return _not_implemented("POST .../complete")


@app.post("/api/v1/picking-tasks/{task_id}/retry")
async def retry_picking_task(
    task_id: int,
    request: Request,
    x_user_id: int | None = Header(default=None, alias="X-User-Id"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    _ = (task_id, request, x_user_id, idempotency_key)
    return _not_implemented("POST .../retry")


@app.post("/api/v1/picking-tasks/{task_id}/force-complete")
async def force_complete_picking_task(
    task_id: int,
    request: Request,
    x_user_id: int | None = Header(default=None, alias="X-User-Id"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    _ = (task_id, request, x_user_id, idempotency_key)
    return _not_implemented("POST .../force-complete")


@app.post("/api/v1/picking-tasks/{task_id}/cancel")
async def cancel_picking_task(
    task_id: int,
    request: Request,
    x_user_id: int | None = Header(default=None, alias="X-User-Id"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    _ = (task_id, request, x_user_id, idempotency_key)
    return _not_implemented("POST .../cancel")


@app.post("/api/v1/internal/reservation-timeouts/run")
async def run_reservation_timeouts(request: Request):
    _ = request
    return _not_implemented("POST /api/v1/internal/reservation-timeouts/run")
