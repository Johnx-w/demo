"""
PickingComplete 可执行用例 — 对齐 docs/test-design/PickingComplete-test-cases.md

覆盖 TC-001 … TC-023；期望输入/输出以测设表与 api §2.4 为准。
库存/账本等 DB 断言通过 app.testing.*（实现阶段提供）。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from tests.conftest import (
    SKU_ID,
    SKU_ID_B,
    USER_OPS,
    USER_PICKER,
    WAREHOUSE_ID,
    apply_inventory_scenario,
    auth_headers,
    d,
    get_order_row,
    get_task_row,
    get_timeout_job,
    list_actions,
    list_ledgers,
    new_action_id,
    seed_pending_pick,
    snapshot_balances,
)


def _complete(
    client,
    *,
    task_id: int,
    task_version: int,
    action_id: str | None = None,
    user_id: int = USER_PICKER,
    json_body: dict | None = None,
):
    aid = action_id or new_action_id("complete")
    body = json_body if json_body is not None else {"action_id": aid, "task_version": task_version}
    return client.post(
        f"/api/v1/picking-tasks/{task_id}/complete",
        json=body,
        headers=auth_headers(user_id, action_id=body.get("action_id", aid)),
    ), body.get("action_id", aid)


def _balances(client, sku_ids: list[int]) -> dict[int, dict]:
    return snapshot_balances(client, warehouse_id=WAREHOUSE_ID, sku_ids=sku_ids)


# ---------------------------------------------------------------------------
# Smoke / P1 主路径
# ---------------------------------------------------------------------------


class TestTC001SufficientComplete:
    """TC-001 Smoke：库存充足 → COMPLETED 原子扣减。"""

    def test_complete_when_stock_sufficient(self, client):
        seeded = seed_pending_pick(client, order_no="SO-TC001")
        task = seeded["task"]
        order = seeded["order"]
        demand = d("2.0000")
        before = _balances(client, [SKU_ID])
        on_hand_before = d(before[SKU_ID]["on_hand"])
        reserved_before = d(before[SKU_ID]["reserved"])

        resp, action_id = _complete(
            client, task_id=task["id"], task_version=task["version"]
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["result"] == "SUCCESS"
        assert data.get("reason_code") in (None, "")
        assert data["task"]["status"] == "COMPLETED"
        assert data["task"]["reservation_held"] is False
        assert data["task"]["version"] == task["version"] + 1
        assert data["task"]["terminal_action_id"] == action_id
        assert data["task"].get("reservation_release_reason") == "COMPLETE"

        after = _balances(client, [SKU_ID])
        assert d(after[SKU_ID]["on_hand"]) == on_hand_before - demand
        assert d(after[SKU_ID]["reserved"]) == reserved_before - demand

        order_row = get_order_row(client, order["id"])
        assert order_row["status"] == "COMPLETED"

        job = get_timeout_job(client, task["id"])
        assert job["status"] == "SKIPPED"

        ledgers = list_ledgers(client, task_id=task["id"])
        deducts = [x for x in ledgers if x.get("entry_type") == "DEDUCT"]
        assert len(deducts) >= 1
        assert d(deducts[0]["delta_on_hand"]) == -demand
        assert d(deducts[0]["delta_reserved"]) == -demand
        assert deducts[0]["task_action_id"] is not None


class TestTC002InsufficientException:
    """TC-002 Smoke：不足 → INSUFFICIENT_STOCK，不扣减。"""

    def test_complete_when_stock_insufficient(self, client):
        seeded = seed_pending_pick(client, order_no="SO-TC002")
        task = seeded["task"]
        order = seeded["order"]
        apply_inventory_scenario("effective_short", task=task, epsilon="0.5000")
        before = _balances(client, [SKU_ID])

        resp, _ = _complete(client, task_id=task["id"], task_version=task["version"])

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["result"] == "SUCCESS"
        assert data["reason_code"] == "INSUFFICIENT_STOCK"
        assert data["task"]["status"] == "INSUFFICIENT_STOCK"
        assert data["task"]["reservation_held"] is True
        assert data["task"]["version"] == task["version"] + 1
        assert data["task"].get("terminal_action_id") in (None, "")
        assert isinstance(data["shortages"], list) and len(data["shortages"]) >= 1
        s0 = data["shortages"][0]
        assert set(s0) >= {"sku_id", "demand_qty", "available", "shortage_qty"}

        after = _balances(client, [SKU_ID])
        assert after[SKU_ID]["on_hand"] == before[SKU_ID]["on_hand"]
        assert after[SKU_ID]["reserved"] == before[SKU_ID]["reserved"]
        assert get_order_row(client, order["id"])["status"] == "PICKING"
        assert get_timeout_job(client, task["id"])["status"] == "PENDING"
        assert not any(x.get("entry_type") == "DEDUCT" for x in list_ledgers(client, task_id=task["id"]))


class TestTC003RawLowButEffectiveOk:
    """TC-003：R<D 但 E≥D 仍应完成（禁止误用 raw）。"""

    def test_complete_uses_effective_not_raw(self, client):
        seeded = seed_pending_pick(client, order_no="SO-TC003", qty="2.0000")
        task = seeded["task"]
        # 典型：raw≈0、own≈demand → E≥D
        apply_inventory_scenario("raw_low_effective_ok", task=task)

        resp, _ = _complete(client, task_id=task["id"], task_version=task["version"])

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["result"] == "SUCCESS"
        assert data.get("reason_code") in (None, "")
        assert data["task"]["status"] == "COMPLETED"
        assert data["task"]["reservation_held"] is False


class TestTC004ExactEqualBoundary:
    """TC-004：E=D 恰足 → COMPLETED。"""

    def test_complete_when_effective_equals_demand(self, client):
        seeded = seed_pending_pick(client, order_no="SO-TC004", qty="2.0000")
        task = seeded["task"]
        apply_inventory_scenario("effective_equals_demand", task=task)

        resp, _ = _complete(client, task_id=task["id"], task_version=task["version"])

        assert resp.status_code == 200, resp.text
        assert resp.json()["task"]["status"] == "COMPLETED"


class TestTC005ExactShortBoundary:
    """TC-005：E=D−ε → INSUFFICIENT_STOCK。"""

    def test_complete_when_effective_just_below_demand(self, client):
        seeded = seed_pending_pick(client, order_no="SO-TC005", qty="2.0000")
        task = seeded["task"]
        apply_inventory_scenario("effective_short", task=task, epsilon="0.0001")

        resp, _ = _complete(client, task_id=task["id"], task_version=task["version"])

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["reason_code"] == "INSUFFICIENT_STOCK"
        assert data["task"]["status"] == "INSUFFICIENT_STOCK"
        assert d(data["shortages"][0]["shortage_qty"]) > 0


class TestTC006MultiSkuAllSufficient:
    """TC-006：多 SKU 全部充足一次完成。"""

    def test_complete_multi_sku_all_ok(self, client):
        lines = [
            {"line_no": 1, "sku_id": SKU_ID, "qty": "1.0000"},
            {"line_no": 2, "sku_id": SKU_ID_B, "qty": "1.0000"},
        ]
        seeded = seed_pending_pick(client, order_no="SO-TC006", lines=lines)
        task = seeded["task"]
        before = _balances(client, [SKU_ID, SKU_ID_B])

        resp, _ = _complete(client, task_id=task["id"], task_version=task["version"])

        assert resp.status_code == 200, resp.text
        assert resp.json()["task"]["status"] == "COMPLETED"
        after = _balances(client, [SKU_ID, SKU_ID_B])
        assert d(after[SKU_ID]["on_hand"]) == d(before[SKU_ID]["on_hand"]) - d("1.0000")
        assert d(after[SKU_ID_B]["on_hand"]) == d(before[SKU_ID_B]["on_hand"]) - d("1.0000")
        deducts = [x for x in list_ledgers(client, task_id=task["id"]) if x.get("entry_type") == "DEDUCT"]
        assert len(deducts) >= 2


class TestTC007MultiSkuPartialShortAllOrNothing:
    """TC-007：多 SKU 部分不足 → 全有或全无，整单不扣。"""

    def test_complete_multi_sku_partial_short_no_partial_deduct(self, client):
        lines = [
            {"line_no": 1, "sku_id": SKU_ID, "qty": "2.0000"},
            {"line_no": 2, "sku_id": SKU_ID_B, "qty": "2.0000"},
        ]
        seeded = seed_pending_pick(client, order_no="SO-TC007", lines=lines)
        task = seeded["task"]
        apply_inventory_scenario("multi_sku_partial_short", task=task, short_sku_id=SKU_ID_B)
        before = _balances(client, [SKU_ID, SKU_ID_B])

        resp, _ = _complete(client, task_id=task["id"], task_version=task["version"])

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["reason_code"] == "INSUFFICIENT_STOCK"
        after = _balances(client, [SKU_ID, SKU_ID_B])
        assert after[SKU_ID]["on_hand"] == before[SKU_ID]["on_hand"]
        assert after[SKU_ID_B]["on_hand"] == before[SKU_ID_B]["on_hand"]
        assert after[SKU_ID]["reserved"] == before[SKU_ID]["reserved"]
        assert after[SKU_ID_B]["reserved"] == before[SKU_ID_B]["reserved"]
        assert not any(x.get("entry_type") == "DEDUCT" for x in list_ledgers(client, task_id=task["id"]))
        short_skus = {s["sku_id"] for s in data["shortages"]}
        assert SKU_ID_B in short_skus


class TestTC008SameSkuMultiLineAggregate:
    """TC-008：同行多行同 SKU 按聚合 demand 扣减。"""

    def test_complete_aggregates_same_sku_lines(self, client):
        lines = [
            {"line_no": 1, "sku_id": SKU_ID, "qty": "1.0000"},
            {"line_no": 2, "sku_id": SKU_ID, "qty": "1.5000"},
        ]
        seeded = seed_pending_pick(client, order_no="SO-TC008", lines=lines)
        task = seeded["task"]
        before = _balances(client, [SKU_ID])
        total = d("2.5000")

        resp, _ = _complete(client, task_id=task["id"], task_version=task["version"])

        assert resp.status_code == 200, resp.text
        assert resp.json()["task"]["status"] == "COMPLETED"
        after = _balances(client, [SKU_ID])
        assert d(after[SKU_ID]["on_hand"]) == d(before[SKU_ID]["on_hand"]) - total
        assert d(after[SKU_ID]["reserved"]) == d(before[SKU_ID]["reserved"]) - total


class TestTC009LockedMakesEffectiveShort:
    """TC-009：locked 抬高使 E 恰不足。"""

    def test_complete_short_when_locked_reduces_effective(self, client):
        seeded = seed_pending_pick(client, order_no="SO-TC009", qty="2.0000")
        task = seeded["task"]
        apply_inventory_scenario("locked_makes_effective_short", task=task)
        before = _balances(client, [SKU_ID])

        resp, _ = _complete(client, task_id=task["id"], task_version=task["version"])

        assert resp.status_code == 200, resp.text
        assert resp.json()["reason_code"] == "INSUFFICIENT_STOCK"
        after = _balances(client, [SKU_ID])
        assert after[SKU_ID]["on_hand"] == before[SKU_ID]["on_hand"]


class TestTC010VersionConflict:
    """TC-010：task_version 不匹配 → 409，库存不变。"""

    def test_complete_version_conflict_no_inventory_change(self, client):
        seeded = seed_pending_pick(client, order_no="SO-TC010")
        task = seeded["task"]
        before = _balances(client, [SKU_ID])
        ledger_before = list_ledgers(client, task_id=task["id"])

        resp, _ = _complete(
            client,
            task_id=task["id"],
            task_version=task["version"] + 99,
        )

        assert resp.status_code == 409, resp.text
        data = resp.json()
        assert data["reason_code"] == "TASK_VERSION_CONFLICT"
        row = get_task_row(client, task["id"])
        assert row["status"] == "PENDING_PICK"
        assert row["version"] == task["version"]
        after = _balances(client, [SKU_ID])
        assert after[SKU_ID] == before[SKU_ID]
        assert list_ledgers(client, task_id=task["id"]) == ledger_before


class TestTC011ConcurrentComplete:
    """TC-011：并发双 complete → 至多一次扣减。"""

    def test_concurrent_complete_at_most_one_deduct(self, client):
        seeded = seed_pending_pick(client, order_no="SO-TC011")
        task = seeded["task"]
        before = _balances(client, [SKU_ID])
        demand = d("2.0000")
        v = task["version"]

        def once(prefix: str):
            aid = new_action_id(prefix)
            return client.post(
                f"/api/v1/picking-tasks/{task['id']}/complete",
                json={"action_id": aid, "task_version": v},
                headers=auth_headers(USER_PICKER, action_id=aid),
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(once, "c1")
            f2 = pool.submit(once, "c2")
            r1, r2 = f1.result(), f2.result()

        statuses = sorted([r1.status_code, r2.status_code])
        assert 200 in statuses
        assert statuses.count(200) == 1
        assert 409 in statuses

        after = _balances(client, [SKU_ID])
        assert d(after[SKU_ID]["on_hand"]) == d(before[SKU_ID]["on_hand"]) - demand
        row = get_task_row(client, task["id"])
        assert row["status"] == "COMPLETED"
        assert row.get("terminal_action_id") not in (None, "")


class TestTC012CasFailNoDirtyWrite:
    """TC-012：CAS 失败无半成功脏写。"""

    def test_cas_failure_leaves_no_dirty_ledger_or_balance(self, client):
        seeded = seed_pending_pick(client, order_no="SO-TC012")
        task = seeded["task"]
        before = _balances(client, [SKU_ID])
        actions_before = list_actions(client, task_id=task["id"])
        ledgers_before = list_ledgers(client, task_id=task["id"])

        resp, _ = _complete(
            client, task_id=task["id"], task_version=task["version"] + 1
        )
        assert resp.status_code == 409, resp.text

        row = get_task_row(client, task["id"])
        assert row["status"] == "PENDING_PICK"
        assert row.get("terminal_action_id") in (None, "")
        assert _balances(client, [SKU_ID])[SKU_ID] == before[SKU_ID]
        # 允许记 DENIED/失败审计，但不允许 SUCCESS+DEDUCT 半成功
        new_ledgers = list_ledgers(client, task_id=task["id"])
        assert new_ledgers == ledgers_before
        new_actions = list_actions(client, task_id=task["id"])
        assert not any(
            a.get("result") == "SUCCESS" and a.get("action_type") == "COMPLETE"
            for a in new_actions
            if a not in actions_before
        )


# ---------------------------------------------------------------------------
# Workflow / 防御 / 横切点到
# ---------------------------------------------------------------------------


class TestTC013CompletedThenCompleteIllegal:
    """TC-013：COMPLETED 后再 complete → 409。"""

    def test_complete_after_completed_rejected(self, client):
        seeded = seed_pending_pick(client, order_no="SO-TC013")
        task = seeded["task"]
        r1, _ = _complete(client, task_id=task["id"], task_version=task["version"])
        assert r1.status_code == 200, r1.text
        done = get_task_row(client, task["id"])
        before = _balances(client, [SKU_ID])

        r2, _ = _complete(
            client,
            task_id=task["id"],
            task_version=done["version"],
            action_id=new_action_id("again"),
        )

        assert r2.status_code == 409, r2.text
        assert r2.json()["reason_code"] in (
            "INVALID_STATUS",
            "TASK_VERSION_CONFLICT",
        )
        assert _balances(client, [SKU_ID])[SKU_ID] == before[SKU_ID]


class TestTC014InsufficientThenCompleteIllegal:
    """TC-014：INSUFFICIENT_STOCK 上误用 complete（应走 retry）。"""

    def test_complete_on_insufficient_status_rejected(self, client):
        seeded = seed_pending_pick(client, order_no="SO-TC014")
        task = seeded["task"]
        apply_inventory_scenario("effective_short", task=task)
        r1, _ = _complete(client, task_id=task["id"], task_version=task["version"])
        assert r1.status_code == 200, r1.text
        assert r1.json()["task"]["status"] == "INSUFFICIENT_STOCK"
        cur = get_task_row(client, task["id"])
        before = _balances(client, [SKU_ID])

        r2, _ = _complete(
            client,
            task_id=task["id"],
            task_version=cur["version"],
            action_id=new_action_id("bad-complete"),
        )

        assert r2.status_code == 409, r2.text
        assert r2.json()["reason_code"] == "INVALID_STATUS"
        assert get_task_row(client, task["id"])["status"] == "INSUFFICIENT_STOCK"
        assert _balances(client, [SKU_ID])[SKU_ID] == before[SKU_ID]


@pytest.mark.parametrize("terminal_status", ["FORCE_COMPLETED", "CANCELLED"])
class TestTC015TerminalStatesRejectComplete:
    """TC-015：FORCE_COMPLETED / CANCELLED 再 complete → 409。"""

    def test_complete_on_terminal_rejected(self, client, terminal_status):
        seeded = seed_pending_pick(client, order_no=f"SO-TC015-{terminal_status}")
        task = seeded["task"]
        # 本模块只断言 complete 对终态拒绝；终态由 harness 注入，避免依赖 force/cancel 实现细节
        apply_inventory_scenario(
            "set_task_status",
            task=task,
            status=terminal_status,
            reservation_held=False,
        )
        cur = get_task_row(client, task["id"])
        assert cur["status"] == terminal_status
        before = _balances(client, [SKU_ID])

        resp, _ = _complete(
            client,
            task_id=task["id"],
            task_version=cur["version"],
            action_id=new_action_id("after-term"),
        )
        assert resp.status_code == 409, resp.text
        assert _balances(client, [SKU_ID])[SKU_ID] == before[SKU_ID]


class TestTC016ReservationNotHeld:
    """TC-016：held=0 → 409 RESERVATION_NOT_HELD。"""

    def test_complete_when_held_false(self, client):
        seeded = seed_pending_pick(client, order_no="SO-TC016")
        task = seeded["task"]
        apply_inventory_scenario("reservation_held_zero", task=task)
        before = _balances(client, [SKU_ID])
        cur = get_task_row(client, task["id"])

        resp, _ = _complete(client, task_id=task["id"], task_version=cur["version"])

        assert resp.status_code == 409, resp.text
        assert resp.json()["reason_code"] == "RESERVATION_NOT_HELD"
        assert _balances(client, [SKU_ID])[SKU_ID] == before[SKU_ID]


class TestTC017TaskNotFound:
    """TC-017：任务不存在 → 404。"""

    def test_complete_unknown_task(self, client):
        aid = new_action_id("nf")
        resp = client.post(
            "/api/v1/picking-tasks/999999999/complete",
            json={"action_id": aid, "task_version": 0},
            headers=auth_headers(USER_PICKER, action_id=aid),
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["reason_code"] == "NOT_FOUND"


class TestTC018InventoryBalanceMissing:
    """TC-018：余额行缺失 → 422 INVENTORY_NOT_FOUND，无静默建行。"""

    def test_complete_when_balance_row_missing(self, client):
        seeded = seed_pending_pick(client, order_no="SO-TC018")
        task = seeded["task"]
        apply_inventory_scenario("delete_inventory_balance", task=task, sku_id=SKU_ID)

        resp, _ = _complete(client, task_id=task["id"], task_version=task["version"])

        assert resp.status_code == 422, resp.text
        assert resp.json()["reason_code"] == "INVENTORY_NOT_FOUND"
        row = get_task_row(client, task["id"])
        assert row["status"] == "PENDING_PICK"
        assert row.get("terminal_action_id") in (None, "")


class TestTC019PermissionDenied:
    """TC-019：无 picking.complete → 403 DENIED。"""

    def test_complete_without_permission(self, client):
        seeded = seed_pending_pick(client, order_no="SO-TC019")
        task = seeded["task"]
        before = _balances(client, [SKU_ID])

        resp, _ = _complete(
            client,
            task_id=task["id"],
            task_version=task["version"],
            user_id=USER_OPS,  # ops: order.import / picking.view，无 complete
        )

        assert resp.status_code == 403, resp.text
        data = resp.json()
        assert data["result"] == "DENIED"
        assert data["reason_code"] == "NO_PERMISSION"
        assert get_task_row(client, task["id"])["status"] == "PENDING_PICK"
        assert _balances(client, [SKU_ID])[SKU_ID] == before[SKU_ID]


class TestTC020IdempotentReplay:
    """TC-020：同 action_id 重放不二次扣减。"""

    def test_complete_replay_same_action_id(self, client):
        seeded = seed_pending_pick(client, order_no="SO-TC020")
        task = seeded["task"]
        action_id = new_action_id("idem-complete")
        r1, _ = _complete(
            client,
            task_id=task["id"],
            task_version=task["version"],
            action_id=action_id,
        )
        assert r1.status_code == 200, r1.text
        after_first = _balances(client, [SKU_ID])
        terminal = get_task_row(client, task["id"])["terminal_action_id"]

        r2 = client.post(
            f"/api/v1/picking-tasks/{task['id']}/complete",
            json={"action_id": action_id, "task_version": task["version"]},
            headers=auth_headers(USER_PICKER, action_id=action_id),
        )

        assert r2.status_code == 200, r2.text
        assert _balances(client, [SKU_ID])[SKU_ID] == after_first
        assert get_task_row(client, task["id"])["terminal_action_id"] == terminal


class TestTC021ValidationErrors:
    """TC-021：缺 action_id / task_version 类型非法 → 4xx，业务不变。"""

    def test_complete_missing_action_id(self, client):
        seeded = seed_pending_pick(client, order_no="SO-TC021A")
        task = seeded["task"]
        before = _balances(client, [SKU_ID])
        resp = client.post(
            f"/api/v1/picking-tasks/{task['id']}/complete",
            json={"task_version": task["version"]},
            headers=auth_headers(USER_PICKER),
        )
        assert resp.status_code >= 400
        assert get_task_row(client, task["id"])["status"] == "PENDING_PICK"
        assert _balances(client, [SKU_ID])[SKU_ID] == before[SKU_ID]

    def test_complete_invalid_task_version_type(self, client):
        seeded = seed_pending_pick(client, order_no="SO-TC021B")
        task = seeded["task"]
        before = _balances(client, [SKU_ID])
        aid = new_action_id("bad-ver")
        resp = client.post(
            f"/api/v1/picking-tasks/{task['id']}/complete",
            json={"action_id": aid, "task_version": "abc"},
            headers=auth_headers(USER_PICKER, action_id=aid),
        )
        assert resp.status_code >= 400
        assert get_task_row(client, task["id"])["status"] == "PENDING_PICK"
        assert _balances(client, [SKU_ID])[SKU_ID] == before[SKU_ID]


class TestTC022ShortageFieldsWhenFullyShort:
    """TC-022：available=0 时 shortages 字段完整。"""

    def test_shortages_when_available_zero(self, client):
        seeded = seed_pending_pick(client, order_no="SO-TC022", qty="2.0000")
        task = seeded["task"]
        apply_inventory_scenario("effective_available_zero", task=task)

        resp, _ = _complete(client, task_id=task["id"], task_version=task["version"])

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["reason_code"] == "INSUFFICIENT_STOCK"
        s0 = data["shortages"][0]
        assert d(s0["shortage_qty"]) == d(s0["demand_qty"])
        assert d(s0["available"]) == Decimal("0")


class TestTC023InsufficientNoTerminalNoJobSkipped:
    """TC-023：不足分支不得 SKIPPED job / 不得写终态键。"""

    def test_insufficient_keeps_job_pending_and_no_terminal(self, client):
        seeded = seed_pending_pick(client, order_no="SO-TC023")
        task = seeded["task"]
        apply_inventory_scenario("effective_short", task=task)

        resp, _ = _complete(client, task_id=task["id"], task_version=task["version"])

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["task"]["status"] == "INSUFFICIENT_STOCK"
        assert data["task"]["reservation_held"] is True
        assert data["task"].get("terminal_action_id") in (None, "")
        assert get_timeout_job(client, task["id"])["status"] == "PENDING"
