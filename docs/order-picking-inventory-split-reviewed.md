# 评审后需求拆分：订单出库拣货扣减

> 基于九维评审修订 + Schema 自检拍板落地；2026-07-21 与 PRD v1.1 / api 定稿对齐（effective_available、写序、仅异常超时、IMPORT_FAILED 重入）。  
> 约束：垂直切片；权限与日志并入业务 Story；非前后端拆分。  
> Schema：`docs/sql/001_schema_picking_inventory.sql` · 设计：`docs/db-design-picking-inventory.md` · PRD：`docs/PRD-订单出库拣货扣减.md`

---

## 1. 范围与边界

### In Scope（本期）
1. 订单导入 → 生成「待拣货」任务（可列表查看）
2. 拣货完成 → 按规则原子扣减库存
3. 库存不足 → 任务置 `INSUFFICIENT_STOCK` 且不扣减
4. 异常人工决策：到货重试 / 平替强制完成 / 取消任务与订单；24h 预留超时释放（**仅** `INSUFFICIENT_STOCK`+held）
5. 权限控制与操作日志

### Out of Scope（显式排除）
- 收货「入库加库存」、上架、盘点、调拨
- 波次拣货、路径优化、PDA 硬件对接
- **波次级/全局替代料策略**、拆单部分发运（正常完成路径整单全有或全无；**异常平替强制完成在范围内**）
- 完整订单中心改造（Demo 用本地 `demo_order` + outbox 回写）

### 业务语义（必须统一）
本需求是**出库履约**：导入销售/出库订单 → 拣货 → **扣减**可用库存。  
不是采购/退货「入库加库存」。

### 正式状态表

**拣货任务 `picking_task.status`**

| 状态码 | 含义 |
|--------|------|
| `PENDING_PICK` | 待拣货 |
| `INSUFFICIENT_STOCK` | 库存不足异常（可人工决策） |
| `COMPLETED` | 已完成（原 SKU 扣减） |
| `FORCE_COMPLETED` | 强制完成（平替发出） |
| `CANCELLED` | 已取消 |

**本地订单 `demo_order.status`**

| 状态码 | 含义 |
|--------|------|
| `IMPORTING` | 导入进行中 |
| `IMPORT_FAILED` | 导入失败（可恢复重试） |
| `PICKING` | 已生成待拣货任务 |
| `COMPLETED` | 履约完成 |
| `FORCE_COMPLETED` | 平替强制完成 |
| `CANCELLED` | 已取消 |

---

## 2. 共享可量化规则（各 Story 共用）

### 2.1 库存公式
```
raw_available       = on_hand - reserved - locked
own_reserved        = 本任务对该 SKU 仍占用的预留合计
effective_available = raw_available + own_reserved
库约束：on_hand >= reserved + locked
```
- **导入 / held=0 再预留：** 按 `sku_id` 聚合后 `raw_available ≥ demand`。  
- **完成 / held=1 重试：** `effective_available ≥ demand`（必须计入本任务预留）。  
- **预留时机：** 导入成功时按 SKU 聚合 `reserved+=qty`；`timeout_job.expire_at = now+24h`，并同步 `task.reservation_expire_at`。  
- **扣减时机：** 完成/重试成功：`on_hand -= qty` 且释放 `reserved`；写入 `terminal_action_id`。  
- **INSUFFICIENT_STOCK：** 不扣 `on_hand`，继续占 `reserved`。  
- **取消 / 平替：** held 时释放原 SKU `reserved`；平替另扣 substitute `on_hand`（本期 `substitute_qty=demand_qty`）。  
- **超时：** 扫描以 **`reservation_timeout_job.expire_at`** 为准；**仅** `INSUFFICIENT_STOCK`+held 释预留，状态仍为 `INSUFFICIENT_STOCK`；之后重试需 **RE_RESERVE**。`PENDING_PICK` 本期不超时释预留。

### 2.2 部分缺货策略
**全有或全无（ALL-OR-NOTHING）：** 任一 SKU 不足 → 整单不扣减、不部分完成。

### 2.3 任务状态机
```
PENDING_PICK ──COMPLETE(足)──► COMPLETED
PENDING_PICK ──COMPLETE(不足)──► INSUFFICIENT_STOCK
INSUFFICIENT_STOCK ──RETRY──► COMPLETED
INSUFFICIENT_STOCK ──FORCE_COMPLETE──► FORCE_COMPLETED
INSUFFICIENT_STOCK ──CANCEL──► CANCELLED
INSUFFICIENT_STOCK ──TIMEOUT_RELEASE──► 仍为 INSUFFICIENT_STOCK（仅释预留）
COMPLETED / FORCE_COMPLETED / CANCELLED 为终态
```

### 2.4 幂等与并发
| 动作 | 幂等键 | 并发规则 |
|------|--------|----------|
| 导入 | `external_order_no` + `warehouse_id`；另有 `action_id` | 相同业务键且已成功：返回已有；失败必落 `IMPORT_FAILED`+FAILED 日志；`IMPORT_FAILED` 可同键重入（全量替换 lines） |
| 完成/重试/强制/取消 | `action_id`（全局唯一） | **同键重放返回首条**（含 FAILED/DENIED）；状态 CAS + `terminal_action_id` |
| 超时 | 系统 `action_id` 规则 + job 行状态 | 仅 `job.status=PENDING`；终态任务→`SKIPPED` |

### 2.5 原子性
「状态变更 + 库存 + 操作日志 + 库存流水（若有）+（导入时）订单/超时 job」同一事务成功或全部回滚。

**写序：** 终态先 CAS task → 改 balance → INSERT `picking_task_action` → INSERT `inventory_ledger(task_action_id)` → order/job/outbox。

### 2.6 权限矩阵

| 权限码 | 动作 | 角色示例 |
|--------|------|----------|
| `order.import` | 导入订单生成任务 | 仓库运营 |
| `picking.view` | 查看任务列表 | 仓库运营、拣货员 |
| `picking.complete` | 完成拣货、到货重试 | 拣货员 |
| `picking.force_complete` | 平替强制完成 | 仓库主管 |
| `picking.cancel` | 取消任务与订单 | 仓库主管 |

### 2.7 操作日志（`picking_task_action`）
| 字段 | 说明 |
|------|------|
| `actor_id` / `actor_name` | 操作人；TIMEOUT 用 `system` |
| `occurred_at` | 服务端时间 |
| `action_type` | 意图动作：IMPORT/COMPLETE/RETRY/FORCE_COMPLETE/CANCEL/TIMEOUT_RELEASE（**禁止**写 DENY；拒绝用 `result=DENIED`） |
| `task_id` / `order_id` / `warehouse_id` | 对象与仓；写操作必填 `actor_id`+`warehouse_id`，能关联则写 order/task |
| `request_id` / `action_id` | 追踪与幂等 |
| `result` | SUCCESS/FAILED/DENIED |
| `reason_code` | INSUFFICIENT_STOCK/NO_PERMISSION/IDEMPOTENT_HIT/INVENTORY_NOT_FOUND/QTY_MISMATCH 等 |
| `request_payload` / `response_payload` | 明细摘要 |

日志追加只写；失败与拒绝也记；同 `action_id` 不插第二条。

---

## 3. Original Story（修订后总述）

- **As a** 仓库履约相关角色（运营 / 拣货员 / 主管）  
- **I want to** 导入出库订单生成待拣货任务，完成拣货时按规则扣减库存，并在缺货时进入可处置的异常流程  
- **so that** 出库账实一致、缺货可收口、操作可审计  

---

## 4. Suggested Splits

### Split 1 · US-01：导入 → PENDING_PICK（含列表可见）

- **Summary:** 导入出库订单生成待拣货任务并完成库存预留

**Use Case:**
- **As a** 拥有 `order.import` 的仓库运营人员  
- **I want to** 导入出库订单并自动生成待拣货任务  
- **so that** 拣货侧能立刻看到可执行任务，且库存已被预留  

**Acceptance Criteria:**

- **Scenario:** 首次导入成功  
- **Given:** 拥有 `order.import`；订单合法且各 SKU（聚合后）`raw_available ≥ demand`  
- **When:** 提交导入（带 `action_id`）  
- **Then:** 同事务创建 `demo_order(PICKING)`、`picking_task(PENDING_PICK)`、预留、`timeout_job`；列表可见；写 SUCCESS 日志  

- **Scenario:** 重复导入幂等  
- **Given:** 相同 `external_order_no`+`warehouse_id` 已存在且为成功态（`PICKING`+task）  
- **When:** 再次导入  
- **Then:** 不新建、不重复预留；返回已有；若同 `action_id` 则返回首条动作记录  

- **Scenario:** 导入库存不足  
- **Given:** 至少一行不足  
- **When:** 提交导入  
- **Then:** 无成功任务/无预留；订单必落 `IMPORT_FAILED`；日志 FAILED+INSUFFICIENT_STOCK（禁止静默全回滚丢审计）  

- **Scenario:** IMPORT_FAILED 同键重入  
- **Given:** 同业务键订单为 `IMPORT_FAILED`  
- **When:** 再次导入（可换明细）  
- **Then:** 以本次 lines 全量替换订单行后重新校验；成功则预留+建任务，失败则仍为 `IMPORT_FAILED`+FAILED 日志  

- **Scenario:** 无权限  
- **Given:** 无 `order.import`  
- **When:** 尝试导入  
- **Then:** action_type=IMPORT + result=DENIED；无订单任务变更  

---

### Split 2 · US-02：完成 → 扣减（成功路径）

- **Summary:** `PENDING_PICK` 完成时原子扣减库存

**Use Case:**
- **As a** 拥有 `picking.complete` 的拣货员  
- **I want to** 标记拣货完成并扣减库存  
- **so that** 出库账实一致且可追溯  

**Acceptance Criteria:**

- **Scenario:** 库存充足完成  
- **Given:** 任务 `PENDING_PICK` 且 held；完成瞬间各 SKU `effective_available ≥ demand`；唯一 `action_id`  
- **When:** 提交完成  
- **Then:** 任务→`COMPLETED`，订单→`COMPLETED`；扣减并释预留；写 `terminal_action_id`；job→SKIPPED；先 action 后 ledger；同 `action_id` 重放不重复扣减  

- **Scenario:** 并发冲突  
- **Given:** 任务已被他人迁出 `PENDING_PICK`  
- **When:** 提交完成  
- **Then:** 失败「任务已变更」；本方不改库存  

- **Scenario:** 无权限 → DENIED，不变  

---

### Split 3 · US-03：完成时不足 → INSUFFICIENT_STOCK

- **Summary:** 完成瞬间不足则整单进异常且不扣减

**Use Case:**
- **As a** 拥有 `picking.complete` 的拣货员  
- **I want to** 在不足时看到明确异常与缺口  
- **so that** 可交给运营决策  

**Acceptance Criteria:**

- **Scenario:** 部分或全部缺货  
- **Given:** `PENDING_PICK` 且至少一行不足  
- **When:** 提交完成  
- **Then:** 不扣 `on_hand`；状态→`INSUFFICIENT_STOCK`；继续占预留；返回缺口；日志 SUCCESS+INSUFFICIENT_STOCK（业务上成功进入异常）  

- **Scenario:** 异常列表可见（`picking.view`）  

---

### Split 4 · US-04：人工决策 + 24h 超时

- **Summary:** 重试 / 平替强制完成 / 取消；超时释预留

**Use Case:**
- **As a** 运营/主管  
- **I want to** 对缺货任务做决策并防止预留长期占用  
- **so that** 订单可收口、库存不被锁死  

**Acceptance Criteria:**

- **Scenario:** held=true 时 RETRY 成功 → `COMPLETED` + 扣减释预留  

- **Scenario:** held=false（曾 TIMEOUT）时 RETRY  
- **Given:** `INSUFFICIENT_STOCK` 且 `reservation_held=false`  
- **When:** 重试  
- **Then:** 若 `raw_available` 足：同事务 RE_RESERVE+DEDUCT→`COMPLETED`，job=`SKIPPED`；若仍不足：保持异常、更新缺口、**不改库存**  

- **Scenario:** FORCE_COMPLETE  
- **Given:** `INSUFFICIENT_STOCK` + `picking.force_complete` + 平替行全覆盖且 `substitute_qty=demand_qty` + 平替 SKU 充足  
- **When:** 强制完成  
- **Then:** 若 held 则释原预留；扣平替 `on_hand`；任务/订单→`FORCE_COMPLETED`；写 terminal_action_id  

- **Scenario:** CANCEL  
- **Given:** `INSUFFICIENT_STOCK` + `picking.cancel`  
- **When:** 取消  
- **Then:** 若 held 则释预留；任务/订单→`CANCELLED`；outbox ORDER_CANCEL  

- **Scenario:** 超时（扫 `reservation_timeout_job.expire_at`）  
- **Given:** job PENDING 且 expire_at<=now；任务为 `INSUFFICIENT_STOCK` 且 held=true  
- **When:** 超时任务执行（actor=system）  
- **Then:** 仅释预留；held=false；reason=TIMEOUT；状态仍 `INSUFFICIENT_STOCK`；job=DONE  

- **Scenario:** 终态不可再操作；超时遇到终态或非异常态 → job SKIPPED（本期不释 `PENDING_PICK` 预留）  

---

## 5. 拆分校验

| 检查项 | 结果 |
|--------|------|
| 垂直切片 | 是 |
| 权限/日志并入 | 是 |
| 闭环 | 是（RETRY / FORCE_COMPLETE / CANCEL + 超时） |
| 与定稿 Schema 对齐 | 是 |

---

## 6. 接口（语义级）

- `POST /order-imports`
- `GET /picking-tasks?status=`
- `POST /picking-tasks/{id}/complete`
- `POST /picking-tasks/{id}/retry`
- `POST /picking-tasks/{id}/force-complete`
- `POST /picking-tasks/{id}/cancel`
- 调度：扫描 `reservation_timeout_job`（`status=PENDING AND expire_at<=now`）

---

## 7. 实现顺序

1. US-01 导入（同事务 + IMPORT_FAILED）  
2. US-02 完成扣减  
3. US-03 不足进异常  
4. US-04 重试 / 平替 / 取消 / 超时  

US-04 为演示闭环必含。
