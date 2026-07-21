# PickingComplete — Test Cases

| 属性 | 内容 |
|------|------|
| Module | PickingComplete |
| API | `POST /api/v1/picking-tasks/{task_id}/complete` |
| Technique Stack | L1 Scenario → L2 Equivalence+Boundary → L3 Decision Table → L5 Workflow/State → L6 Error Guessing |
| Count | 23（Smoke 2；目标 20–25） |
| Sources | PRD S2/S3；api §0/§2.4；US-02/US-03；[PickingComplete-test-analysis.md](./PickingComplete-test-analysis.md) |

> 前置约定：除特别说明外，操作人持有 `picking.complete`；任务由导入产生，初始 `PENDING_PICK`、`reservation_held=true`、订单 `PICKING`、timeout job `PENDING`。数量字段可用字符串十进制。

---

## Unified Test Case Table

| ID | Technique | Test Level | Test Scenario | Preconditions | Steps | Expected Result | Priority | Smoke | Requirement Ref |
|---|---|---|---|---|---|---|---|---|---|
| TC-001 | Scenario | API/DB | 库存充足完成：原子扣减并终态 | 任务 PENDING_PICK+held；各 SKU effective_available≥demand；唯一 action_id；已知 task_version | 1. POST .../complete 带 action_id 与正确 task_version 2. 查询 task/order/inventory/ledger/job/action | HTTP 200；result=SUCCESS；reason_code 空；task=COMPLETED、held=false、version+1、terminal_action_id=action_id、release_reason=COMPLETE；order=COMPLETED；on_hand-=demand、reserved-=demand；ledger 有 DEDUCT（delta_on_hand=-q, delta_reserved=-q）且 task_action_id 指向本 action；job=SKIPPED；写序满足 CAS→balance→action→ledger | P1 | ✓ | S2, FR-03, API-2.4-OK, API-2.4-ORDER, API-2.4-CAS, INV-TERM, PRD-6.4-ATOMIC |
| TC-002 | Scenario | API/DB | 完成时不足：进异常且不扣减 | PENDING_PICK+held；至少一 SKU effective_available\<demand | 1. POST .../complete 2. 比对完成前后 inventory 快照与 job/order | HTTP 200；result=SUCCESS；reason_code=INSUFFICIENT_STOCK；task=INSUFFICIENT_STOCK、held=true、version+1；shortages 含 sku_id/demand_qty/available/shortage_qty；on_hand/reserved 不变；无 DEDUCT ledger；terminal_action_id 仍空；order 仍 PICKING；job 仍 PENDING；action.result=SUCCESS 且 reason=INSUFFICIENT_STOCK | P1 | ✓ | S3, FR-04, API-2.4-SHORT, PRD-6.4-ATOMIC |
| TC-003 | Error Guessing | API/Logic | 误用 raw 风险：R\<D 但 E≥D 仍应完成 | held=1；本任务 Own≈demand 使 raw_available\<demand，但 effective_available≥demand | 1. POST .../complete（版本正确） | 200 COMPLETED 并扣减释预留；不得因 raw 不足进 INSUFFICIENT_STOCK | P1 | | API-0-EFF, FR-03, S2 |
| TC-004 | Boundary | API/Logic | effective 恰等于 demand 判定充足 | 单 SKU；E=D（恰足） | 1. POST .../complete | 200 COMPLETED；正常扣减 | P1 | | API-0-EFF, S2, FR-03 |
| TC-005 | Boundary | API/Logic | effective 恰小于 demand 进异常 | 单 SKU；E=D−ε（ε\>0） | 1. POST .../complete | 200 + INSUFFICIENT_STOCK；不扣减；shortages.shortage_qty≈ε | P1 | | API-0-EFF, S3, API-2.4-SHORT |
| TC-006 | Scenario | API/DB | 多 SKU 全部充足一次完成 | ≥2 SKU；各 E_i≥D_i | 1. POST .../complete | 200 COMPLETED；各 SKU on_hand/reserved 均按聚合 demand 扣减；各有 DEDUCT（或等价按 sku 流水） | P1 | | S2, API-2.4-AGG, FR-03 |
| TC-007 | Decision Table | API/DB | 多 SKU 部分不足：全有或全无 | ≥2 SKU；仅一 SKU E\<D，其余充足 | 1. POST .../complete 2. 检查全部 SKU 余额与 ledger | 200 INSUFFICIENT_STOCK；**所有** SKU on_hand/reserved 均不变；无任何 DEDUCT；shortages 至少含不足 SKU | P1 | | PRD-6.1-AON, FR-04, S3, API-2.4-AGG |
| TC-008 | Equivalence | API/DB | 同行多行同 SKU：按 sku 聚合判定与扣减 | 同一 task 两行同 sku_id，demand 分别为 d1、d2；E≥d1+d2 | 1. POST .../complete | 200 COMPLETED；对该 sku 一次按 sum=d1+d2 扣减，非按行漏扣/双扣 | P1 | | API-2.4-AGG, S2, FR-03 |
| TC-009 | Boundary | Logic/API | locked 使 effective 恰不足 | on_hand/reserved/own 固定；提高 locked 使 E=D−ε | 1. POST .../complete | 200 INSUFFICIENT_STOCK；不扣减 | P2 | | API-0-EFF, S3 |
| TC-010 | Decision Table | API/DB | task_version 不匹配：409 且不改库存 | PENDING_PICK+held+库存足；客户端 task_version=version+99（或 version−1） | 1. 记录库存与 version 2. POST .../complete 3. 再查库存/ledger | HTTP 409；reason_code=TASK_VERSION_CONFLICT；task 状态/version 不变；无库存变更；无新 DEDUCT ledger | P1 | | API-2.4-CAS, API-ERR-409, US-02-CONCUR, FR-03 |
| TC-011 | Error Guessing | API/DB | 并发双 complete：至多一次成功扣减 | 同任务 PENDING；两请求不同 action_id、相同正确 version 近乎同时提交 | 1. 并行 POST complete×2 2. 查最终库存与终态 | 恰一笔成功终态 COMPLETED；另一笔 409/冲突；on_hand 仅减少一轮 demand；terminal_action_id 唯一 | P1 | | US-02-CONCUR, API-2.4-CAS, S2, PRD-6.4-ATOMIC |
| TC-012 | Error Guessing | DB | CAS 失败路径无半成功脏写 | 同 TC-010 或人为制造 CAS rows=0 | 1. 触发 409 2. 查 picking_task_action / inventory_ledger / balance | 无「已扣库存但任务未终态」或「已写 DEDUCT 但 CAS 失败」；余额与冲突前一致 | P1 | | API-2.4-ORDER, API-2.4-CAS, PRD-6.4-ATOMIC |
| TC-013 | Workflow | API/DB | 终态 COMPLETED 再 complete 非法 | 任务已 COMPLETED（可用 TC-001 后状态） | 1. 再 POST .../complete（新 action_id，task_version 用当前或旧值） | 409；reason_code=INVALID_STATUS 或 TASK_VERSION_CONFLICT；库存不再变化 | P2 | | API-ERR-409, INV-TERM, FR-03 |
| TC-014 | Workflow | API | INSUFFICIENT_STOCK 上误用 complete（非 retry） | 任务已 INSUFFICIENT_STOCK | 1. POST .../complete | 409 INVALID_STATUS（或等价）；状态与库存不变；应走 retry 而非 complete | P2 | | API-ERR-409, FR-04 |
| TC-015 | Workflow | API | FORCE_COMPLETED/CANCELLED 再 complete | 任务分别为 FORCE_COMPLETED 与 CANCELLED（两条断言可同用例分步或参数化） | 1. 对各终态 POST complete | 均 409；不改库存 | P2 | | API-ERR-409 |
| TC-016 | Decision Table | API/DB | reservation_held=0 时 complete 防御 | PENDING_PICK 但 held=0（测试注入/异常数据） | 1. POST .../complete（version 正确） | 409；reason_code=RESERVATION_NOT_HELD；不改库存 | P2 | | API-2.4-HELD0, API-ERR-409 |
| TC-017 | Equivalence | API | 任务不存在 | task_id 无对应行 | 1. POST /picking-tasks/{unknown}/complete | 404；reason_code=NOT_FOUND | P3 | | API-ERR-404 |
| TC-018 | Error Guessing | API/DB | 完成时库存余额行缺失 | PENDING_PICK+held；对应 warehouse+sku 的 inventory_balance 被删除或不存在 | 1. POST .../complete | 422；reason_code=INVENTORY_NOT_FOUND；不静默 upsert；不扣减/不误终态（或整单回滚保持 PENDING，以实现为准但须可观测失败且无脏扣） | P2 | | API-ERR 扩展自 §0 INVENTORY_NOT_FOUND |
| TC-019 | Scenario | API/DB | 无 picking.complete 权限被拒绝（点到） | 用户仅有 picking.view（如 ops）；任务可完成 | 1. POST .../complete | 403；result=DENIED；reason_code=NO_PERMISSION；task 仍 PENDING_PICK；库存不变；可落 DENIED 审计 | P2 | | US-02-PERM |
| TC-020 | Scenario | API/DB | 同 action_id 重放不二次扣减（点到） | 已成功 complete（TC-001）；库存已扣一轮 | 1. 用**相同** action_id 再 POST complete（可带首次 version） | 200；响应与首条语义一致（或 IDEMPOTENT_HIT）；on_hand 不第二次减少；terminal_action_id 不变 | P2 | | US-02-IDEM, S2 |
| TC-021 | Equivalence | API | 请求缺 action_id 或 task_version 类型非法 | PENDING 可完成任务 | 1. Body 缺 action_id 或 task_version="abc" 提交 | 4xx 校验错误；业务状态与库存不变 | P3 | | FR-03（输入校验） |
| TC-022 | Boundary | API | shortages 字段完整性（全不足） | 单 SKU；available=0；demand=D | 1. POST .../complete | 200 INSUFFICIENT_STOCK；shortages[0].shortage_qty=D；available 反映判定所用有效可用（文档为响应计算值） | P2 | | API-2.4-SHORT, S3 |
| TC-023 | Decision Table | API/DB | 不足分支不得 SKIPPED job / 不得写终态键 | 同 TC-002 前置 | 1. complete 进异常后读 job 与 terminal_action_id | job.status 仍为 PENDING；terminal_action_id IS NULL；held=true | P1 | | S3, FR-04, API-2.4-SHORT, INV-TERM（反向） |

---

## Summary Counts

| Priority | Count | IDs |
|----------|------:|-----|
| P1 | 13 | TC-001–008, TC-010–012, TC-023 |
| P2 | 8 | TC-009, TC-013–016, TC-018–020, TC-022 |
| P3 | 2 | TC-017, TC-021 |
| **Total** | **23** | |
| Smoke ✓ | 2 | TC-001, TC-002 |

| Technique | Count |
|-----------|------:|
| Scenario | 5 |
| Boundary | 4 |
| Equivalence | 3 |
| Decision Table | 4 |
| Workflow | 3 |
| Error Guessing | 4 |

---

## Notes for Implementers

1. **TC-003** 是公式回归核心：导入后若仅看 raw 会假不足，必须用 effective。
2. **TC-018**：complete 路径文档以 import/平替为主写明 `INVENTORY_NOT_FOUND`；本用例覆盖「完成瞬间余额行消失」的防御，期望 422 且无静默建行。
3. **TC-019/020** 仅为横切点到，完整权限/幂等矩阵不在本模块展开。
4. **TC-015** 允许参数化一次跑 FORCE_COMPLETED 与 CANCELLED 两个终态。
