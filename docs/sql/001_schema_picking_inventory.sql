-- =============================================================================
-- Demo: 订单出库拣货扣减（定稿 Schema）
-- MySQL 8.0+ / utf8mb4
--
-- 拍板摘要：
--   状态：PENDING_PICK | INSUFFICIENT_STOCK | COMPLETED | FORCE_COMPLETED | CANCELLED
--   订单：IMPORTING | IMPORT_FAILED | PICKING | COMPLETED | FORCE_COMPLETED | CANCELLED
--   幂等：action_id 全局唯一；同键重放返回首条（含 FAILED/DENIED）
--   双扣防线：picking_task.terminal_action_id UNIQUE + version CAS
--   超时：扫描以 reservation_timeout_job.expire_at 为准；仅 INSUFFICIENT_STOCK+held
--   终态写序：先 CAS picking_task.terminal_action_id → 改 balance → INSERT action → INSERT ledger(task_action_id)
--   充足判定：导入用 raw_available；完成/held=1 用 effective_available（含本任务预留）
--   导入失败：落 IMPORT_FAILED + action.FAILED（禁止静默回滚丢审计）；IMPORT_FAILED 重入全量替换 lines
--   拒绝：action_type=意图 + result=DENIED（DENY 类型仅兼容保留）
--   Demo：强制 FK → ref_* / demo_order / demo_auth_user；生产可摘除 ref/auth FK
-- =============================================================================

CREATE DATABASE IF NOT EXISTS demo_picking
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_0900_ai_ci;

USE demo_picking;

-- -----------------------------------------------------------------------------
-- 0) Demo 主数据镜像
-- -----------------------------------------------------------------------------
CREATE TABLE ref_warehouse (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  code          VARCHAR(64)     NOT NULL COMMENT '仓库编码',
  name          VARCHAR(128)    NOT NULL,
  status        TINYINT         NOT NULL DEFAULT 1 COMMENT '1启用 0停用',
  created_at    DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_ref_warehouse_code (code)
) ENGINE=InnoDB COMMENT='Demo:外部仓库主数据镜像';

CREATE TABLE ref_sku (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  sku_code      VARCHAR(64)     NOT NULL,
  name          VARCHAR(256)    NOT NULL,
  status        TINYINT         NOT NULL DEFAULT 1,
  created_at    DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_ref_sku_code (sku_code)
) ENGINE=InnoDB COMMENT='Demo:外部商品主数据镜像';

-- -----------------------------------------------------------------------------
-- 1) Demo 账号与权限中心 Stub（含 system 用户供 TIMEOUT 等）
-- -----------------------------------------------------------------------------
CREATE TABLE demo_auth_user (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  username      VARCHAR(64)     NOT NULL,
  display_name  VARCHAR(128)    NOT NULL,
  enabled       TINYINT         NOT NULL DEFAULT 1,
  created_at    DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_demo_auth_user_username (username)
) ENGINE=InnoDB COMMENT='Demo stub:账号中心用户';

CREATE TABLE demo_auth_permission (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  code          VARCHAR(64)     NOT NULL,
  name          VARCHAR(128)    NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_demo_auth_permission_code (code)
) ENGINE=InnoDB COMMENT='Demo stub:权限码';

CREATE TABLE demo_auth_role (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  code          VARCHAR(64)     NOT NULL,
  name          VARCHAR(128)    NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_demo_auth_role_code (code)
) ENGINE=InnoDB COMMENT='Demo stub:角色';

CREATE TABLE demo_auth_role_permission (
  role_id       BIGINT UNSIGNED NOT NULL,
  permission_id BIGINT UNSIGNED NOT NULL,
  PRIMARY KEY (role_id, permission_id),
  KEY idx_demo_rp_permission (permission_id),
  CONSTRAINT fk_demo_rp_role FOREIGN KEY (role_id) REFERENCES demo_auth_role (id) ON DELETE RESTRICT,
  CONSTRAINT fk_demo_rp_perm FOREIGN KEY (permission_id) REFERENCES demo_auth_permission (id) ON DELETE RESTRICT
) ENGINE=InnoDB COMMENT='Demo stub:角色-权限';

CREATE TABLE demo_auth_user_role (
  user_id       BIGINT UNSIGNED NOT NULL,
  role_id       BIGINT UNSIGNED NOT NULL,
  PRIMARY KEY (user_id, role_id),
  KEY idx_demo_ur_role (role_id),
  CONSTRAINT fk_demo_ur_user FOREIGN KEY (user_id) REFERENCES demo_auth_user (id) ON DELETE RESTRICT,
  CONSTRAINT fk_demo_ur_role FOREIGN KEY (role_id) REFERENCES demo_auth_role (id) ON DELETE RESTRICT
) ENGINE=InnoDB COMMENT='Demo stub:用户-角色';

-- -----------------------------------------------------------------------------
-- 2) 本地订单
-- status: IMPORTING|IMPORT_FAILED|PICKING|COMPLETED|FORCE_COMPLETED|CANCELLED
-- -----------------------------------------------------------------------------
CREATE TABLE demo_order (
  id                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  external_order_no  VARCHAR(64)     NOT NULL COMMENT '业务订单号',
  warehouse_id       BIGINT UNSIGNED NOT NULL,
  status             VARCHAR(32)     NOT NULL COMMENT 'IMPORTING|IMPORT_FAILED|PICKING|COMPLETED|FORCE_COMPLETED|CANCELLED',
  source             VARCHAR(32)     NOT NULL DEFAULT 'IMPORT',
  created_by         BIGINT UNSIGNED NULL,
  created_at         DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at         DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  cancelled_at       DATETIME(3)     NULL,
  cancel_reason      VARCHAR(256)    NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_demo_order_no_wh (external_order_no, warehouse_id),
  KEY idx_demo_order_status_created (status, created_at),
  KEY idx_demo_order_warehouse (warehouse_id),
  CONSTRAINT fk_demo_order_warehouse FOREIGN KEY (warehouse_id) REFERENCES ref_warehouse (id) ON DELETE RESTRICT,
  CONSTRAINT fk_demo_order_created_by FOREIGN KEY (created_by) REFERENCES demo_auth_user (id) ON DELETE RESTRICT,
  CONSTRAINT chk_demo_order_status CHECK (
    status IN (
      'IMPORTING',
      'IMPORT_FAILED',
      'PICKING',
      'COMPLETED',
      'FORCE_COMPLETED',
      'CANCELLED'
    )
  )
) ENGINE=InnoDB COMMENT='Demo:本地订单头；导入须同事务落到 PICKING+task，或失败到 IMPORT_FAILED';

CREATE TABLE demo_order_line (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_id      BIGINT UNSIGNED NOT NULL,
  line_no       INT             NOT NULL,
  sku_id        BIGINT UNSIGNED NOT NULL,
  qty           DECIMAL(18, 4)  NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_demo_order_line (order_id, line_no),
  KEY idx_demo_order_line_sku (sku_id),
  CONSTRAINT fk_demo_order_line_order FOREIGN KEY (order_id) REFERENCES demo_order (id) ON DELETE RESTRICT,
  CONSTRAINT fk_demo_order_line_sku FOREIGN KEY (sku_id) REFERENCES ref_sku (id) ON DELETE RESTRICT,
  CONSTRAINT chk_order_line_qty CHECK (qty > 0)
) ENGINE=InnoDB COMMENT='Demo:本地订单行';

-- -----------------------------------------------------------------------------
-- 3) 库存余额
-- available = on_hand - reserved - locked（应用层计算）
-- -----------------------------------------------------------------------------
CREATE TABLE inventory_balance (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  warehouse_id  BIGINT UNSIGNED NOT NULL,
  sku_id        BIGINT UNSIGNED NOT NULL,
  on_hand       DECIMAL(18, 4)  NOT NULL DEFAULT 0,
  reserved      DECIMAL(18, 4)  NOT NULL DEFAULT 0,
  locked        DECIMAL(18, 4)  NOT NULL DEFAULT 0,
  version       INT UNSIGNED    NOT NULL DEFAULT 0 COMMENT '乐观锁',
  updated_at    DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_inv_wh_sku (warehouse_id, sku_id),
  KEY idx_inv_sku (sku_id),
  CONSTRAINT fk_inv_warehouse FOREIGN KEY (warehouse_id) REFERENCES ref_warehouse (id) ON DELETE RESTRICT,
  CONSTRAINT fk_inv_sku FOREIGN KEY (sku_id) REFERENCES ref_sku (id) ON DELETE RESTRICT,
  CONSTRAINT chk_inv_nonneg CHECK (on_hand >= 0 AND reserved >= 0 AND locked >= 0),
  CONSTRAINT chk_inv_available CHECK (on_hand >= reserved + locked)
) ENGINE=InnoDB COMMENT='库存余额';

-- -----------------------------------------------------------------------------
-- 4) 拣货任务
-- status: PENDING_PICK|INSUFFICIENT_STOCK|COMPLETED|FORCE_COMPLETED|CANCELLED
-- terminal_action_id: 成功进入终态时写入，防双扣/双终态
-- reservation_expire_at: 与 timeout_job.expire_at 双写同步；扫描以 job 为准
-- -----------------------------------------------------------------------------
CREATE TABLE picking_task (
  id                          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_id                    BIGINT UNSIGNED NOT NULL COMMENT '关联 demo_order.id，1:1',
  external_order_no           VARCHAR(64)     NOT NULL,
  warehouse_id                BIGINT UNSIGNED NOT NULL,
  status                      VARCHAR(32)     NOT NULL,
  version                     INT UNSIGNED    NOT NULL DEFAULT 0 COMMENT '状态 CAS',
  terminal_action_id          VARCHAR(64)     NULL COMMENT '成功终态对应 action_id，防重复完成',
  reservation_held            TINYINT(1)      NOT NULL DEFAULT 1,
  reservation_expire_at       DATETIME(3)     NOT NULL COMMENT '冗余缓存；与 timeout_job 同步；扫描以 job 为准',
  reservation_released_at     DATETIME(3)     NULL,
  reservation_release_reason  VARCHAR(32)     NULL COMMENT 'TIMEOUT|CANCEL|COMPLETE|FORCE_COMPLETE',
  exception_code              VARCHAR(64)     NULL,
  exception_message           VARCHAR(512)    NULL,
  created_by                  BIGINT UNSIGNED NULL,
  created_at                  DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at                  DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  completed_at                DATETIME(3)     NULL,
  cancelled_at                DATETIME(3)     NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_picking_task_order_wh (external_order_no, warehouse_id),
  UNIQUE KEY uk_picking_task_order_id (order_id),
  UNIQUE KEY uk_picking_task_terminal_action (terminal_action_id),
  KEY idx_picking_task_status_created (status, created_at),
  KEY idx_picking_task_wh_status_created (warehouse_id, status, created_at),
  KEY idx_picking_task_expire (status, reservation_held, reservation_expire_at),
  CONSTRAINT fk_picking_task_order FOREIGN KEY (order_id) REFERENCES demo_order (id) ON DELETE RESTRICT,
  CONSTRAINT fk_picking_task_warehouse FOREIGN KEY (warehouse_id) REFERENCES ref_warehouse (id) ON DELETE RESTRICT,
  CONSTRAINT fk_picking_task_created_by FOREIGN KEY (created_by) REFERENCES demo_auth_user (id) ON DELETE RESTRICT,
  CONSTRAINT chk_picking_task_status CHECK (
    status IN (
      'PENDING_PICK',
      'INSUFFICIENT_STOCK',
      'COMPLETED',
      'FORCE_COMPLETED',
      'CANCELLED'
    )
  ),
  CONSTRAINT chk_picking_release_reason CHECK (
    reservation_release_reason IS NULL
    OR reservation_release_reason IN ('TIMEOUT', 'CANCEL', 'COMPLETE', 'FORCE_COMPLETE')
  )
) ENGINE=InnoDB COMMENT='拣货任务';

CREATE TABLE picking_task_line (
  id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  task_id             BIGINT UNSIGNED NOT NULL,
  line_no             INT             NOT NULL,
  sku_id              BIGINT UNSIGNED NOT NULL COMMENT '原需求 SKU',
  demand_qty          DECIMAL(18, 4)  NOT NULL,
  shortage_qty        DECIMAL(18, 4)  NULL COMMENT '异常时缺口',
  substitute_sku_id   BIGINT UNSIGNED NULL COMMENT '平替 SKU',
  substitute_qty      DECIMAL(18, 4)  NULL,
  line_fulfill_type   VARCHAR(32)     NOT NULL DEFAULT 'ORIGINAL' COMMENT 'ORIGINAL|SUBSTITUTE|NONE',
  PRIMARY KEY (id),
  UNIQUE KEY uk_picking_task_line (task_id, line_no),
  KEY idx_picking_task_line_sku (sku_id),
  KEY idx_picking_task_line_sub_sku (substitute_sku_id),
  CONSTRAINT fk_picking_task_line_task FOREIGN KEY (task_id) REFERENCES picking_task (id) ON DELETE RESTRICT,
  CONSTRAINT fk_picking_task_line_sku FOREIGN KEY (sku_id) REFERENCES ref_sku (id) ON DELETE RESTRICT,
  CONSTRAINT fk_picking_task_line_sub_sku FOREIGN KEY (substitute_sku_id) REFERENCES ref_sku (id) ON DELETE RESTRICT,
  CONSTRAINT chk_demand_positive CHECK (demand_qty > 0),
  CONSTRAINT chk_shortage_nonneg CHECK (shortage_qty IS NULL OR shortage_qty >= 0),
  CONSTRAINT chk_substitute_pair CHECK (
    (substitute_sku_id IS NULL AND substitute_qty IS NULL)
    OR (substitute_sku_id IS NOT NULL AND substitute_qty IS NOT NULL AND substitute_qty > 0)
  ),
  CONSTRAINT chk_line_fulfill_type CHECK (
    line_fulfill_type IN ('ORIGINAL', 'SUBSTITUTE', 'NONE')
  )
) ENGINE=InnoDB COMMENT='拣货任务行';

-- -----------------------------------------------------------------------------
-- 5) 任务操作全量流水（幂等 + 审计）
-- action_id：全局唯一；同键重放返回首条（含 FAILED/DENIED），客户端无需因失败换键
-- -----------------------------------------------------------------------------
CREATE TABLE picking_task_action (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  action_id         VARCHAR(64)     NOT NULL COMMENT '幂等键：UK；冲突则返回首条记录',
  task_id           BIGINT UNSIGNED NULL,
  order_id          BIGINT UNSIGNED NULL,
  warehouse_id      BIGINT UNSIGNED NULL COMMENT '审计按仓；业务动作建议必填',
  action_type       VARCHAR(32)     NOT NULL,
  from_status       VARCHAR(32)     NULL,
  to_status         VARCHAR(32)     NULL,
  result            VARCHAR(16)     NOT NULL COMMENT 'SUCCESS|FAILED|DENIED',
  reason_code       VARCHAR(64)     NULL,
  actor_id          BIGINT UNSIGNED NULL COMMENT '业务建议非空；TIMEOUT 用 system 用户',
  actor_name        VARCHAR(128)    NULL COMMENT '快照',
  request_id        VARCHAR(64)     NULL,
  request_payload   JSON            NULL,
  response_payload  JSON            NULL,
  occurred_at       DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_picking_task_action_id (action_id),
  KEY idx_pta_task_time (task_id, occurred_at),
  KEY idx_pta_order_time (order_id, occurred_at),
  KEY idx_pta_wh_time (warehouse_id, occurred_at),
  KEY idx_pta_type_time (action_type, occurred_at),
  CONSTRAINT fk_pta_task FOREIGN KEY (task_id) REFERENCES picking_task (id) ON DELETE RESTRICT,
  CONSTRAINT fk_pta_order FOREIGN KEY (order_id) REFERENCES demo_order (id) ON DELETE RESTRICT,
  CONSTRAINT fk_pta_warehouse FOREIGN KEY (warehouse_id) REFERENCES ref_warehouse (id) ON DELETE RESTRICT,
  CONSTRAINT fk_pta_actor FOREIGN KEY (actor_id) REFERENCES demo_auth_user (id) ON DELETE RESTRICT,
  CONSTRAINT chk_pta_action_type CHECK (
    action_type IN (
      'IMPORT',
      'COMPLETE',
      'RETRY',
      'FORCE_COMPLETE',
      'CANCEL',
      'TIMEOUT_RELEASE',
      'DENY'              -- deprecated：仅兼容保留；新代码禁止写入，拒绝用 result=DENIED
    )
  ),
  CONSTRAINT chk_pta_result CHECK (result IN ('SUCCESS', 'FAILED', 'DENIED'))
) ENGINE=InnoDB COMMENT='拣货任务操作全量记录；幂等同键返回首条；写操作应用层必填 actor_id+warehouse_id；ledger 须在本表行之后插入';

-- -----------------------------------------------------------------------------
-- 6) 库存流水
-- -----------------------------------------------------------------------------
CREATE TABLE inventory_ledger (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  warehouse_id      BIGINT UNSIGNED NOT NULL,
  sku_id            BIGINT UNSIGNED NOT NULL,
  task_id           BIGINT UNSIGNED NULL,
  task_action_id    BIGINT UNSIGNED NULL,
  biz_type          VARCHAR(32)     NOT NULL,
  delta_on_hand     DECIMAL(18, 4)  NOT NULL DEFAULT 0,
  delta_reserved    DECIMAL(18, 4)  NOT NULL DEFAULT 0,
  delta_locked      DECIMAL(18, 4)  NOT NULL DEFAULT 0,
  on_hand_after     DECIMAL(18, 4)  NOT NULL,
  reserved_after    DECIMAL(18, 4)  NOT NULL,
  locked_after      DECIMAL(18, 4)  NOT NULL,
  created_at        DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  created_by        BIGINT UNSIGNED NULL,
  PRIMARY KEY (id),
  KEY idx_ledger_task (task_id),
  KEY idx_ledger_action (task_action_id),
  KEY idx_ledger_wh_sku_time (warehouse_id, sku_id, created_at),
  CONSTRAINT fk_ledger_warehouse FOREIGN KEY (warehouse_id) REFERENCES ref_warehouse (id) ON DELETE RESTRICT,
  CONSTRAINT fk_ledger_sku FOREIGN KEY (sku_id) REFERENCES ref_sku (id) ON DELETE RESTRICT,
  CONSTRAINT fk_ledger_task FOREIGN KEY (task_id) REFERENCES picking_task (id) ON DELETE RESTRICT,
  CONSTRAINT fk_ledger_action FOREIGN KEY (task_action_id) REFERENCES picking_task_action (id) ON DELETE RESTRICT,
  CONSTRAINT fk_ledger_created_by FOREIGN KEY (created_by) REFERENCES demo_auth_user (id) ON DELETE RESTRICT,
  CONSTRAINT chk_ledger_biz_type CHECK (
    biz_type IN ('RESERVE', 'DEDUCT', 'RELEASE', 'DEDUCT_SUBSTITUTE', 'RE_RESERVE')
  )
) ENGINE=InnoDB COMMENT='库存变动流水；须先有 picking_task_action 再插入（FK task_action_id）；预留/释放可多次，双扣靠 task.terminal_action_id+CAS';

-- -----------------------------------------------------------------------------
-- 7) 预留超时任务（扫描真相源）
-- -----------------------------------------------------------------------------
CREATE TABLE reservation_timeout_job (
  id                   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  task_id              BIGINT UNSIGNED NOT NULL,
  expire_at            DATETIME(3)     NOT NULL COMMENT '超时扫描以此为准',
  status               VARCHAR(16)     NOT NULL DEFAULT 'PENDING' COMMENT 'PENDING|DONE|SKIPPED',
  processed_action_id  BIGINT UNSIGNED NULL,
  processed_at         DATETIME(3)     NULL,
  created_at           DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_timeout_task (task_id),
  KEY idx_timeout_scan (status, expire_at),
  CONSTRAINT fk_timeout_task FOREIGN KEY (task_id) REFERENCES picking_task (id) ON DELETE RESTRICT,
  CONSTRAINT fk_timeout_action FOREIGN KEY (processed_action_id) REFERENCES picking_task_action (id) ON DELETE RESTRICT,
  CONSTRAINT chk_timeout_status CHECK (status IN ('PENDING', 'DONE', 'SKIPPED'))
) ENGINE=InnoDB COMMENT='预留超时释放调度；expire_at 为扫描真相源；仅处理 INSUFFICIENT_STOCK+held';

-- -----------------------------------------------------------------------------
-- 8) 集成 Outbox
-- -----------------------------------------------------------------------------
CREATE TABLE integration_outbox (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  event_type        VARCHAR(64)     NOT NULL COMMENT 'ORDER_CANCEL|TASK_COMPLETED|TASK_FORCE_COMPLETED|ORDER_STATUS_SYNC',
  aggregate_type    VARCHAR(32)     NOT NULL DEFAULT 'PickingTask',
  aggregate_id      BIGINT UNSIGNED NOT NULL,
  idempotency_key   VARCHAR(128)    NOT NULL,
  payload           JSON            NOT NULL,
  status            VARCHAR(16)     NOT NULL DEFAULT 'PENDING' COMMENT 'PENDING|SENT|FAILED',
  retry_count       INT UNSIGNED    NOT NULL DEFAULT 0,
  next_retry_at     DATETIME(3)     NULL,
  last_error        VARCHAR(512)    NULL,
  created_at        DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  sent_at           DATETIME(3)     NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_outbox_idem (idempotency_key),
  KEY idx_outbox_poll (status, next_retry_at, id),
  CONSTRAINT chk_outbox_status CHECK (status IN ('PENDING', 'SENT', 'FAILED'))
) ENGINE=InnoDB COMMENT='集成出站消息';

-- -----------------------------------------------------------------------------
-- 9) 种子数据
-- -----------------------------------------------------------------------------
INSERT INTO demo_auth_user (id, username, display_name, enabled) VALUES
  (1, 'system', 'System', 1),
  (2, 'ops', '仓库运营', 1),
  (3, 'picker', '拣货员', 1),
  (4, 'supervisor', '仓库主管', 1);

INSERT INTO demo_auth_permission (code, name) VALUES
  ('order.import', '导入订单生成拣货任务'),
  ('picking.view', '查看拣货任务'),
  ('picking.complete', '完成拣货/到货重试'),
  ('picking.force_complete', '平替强制完成'),
  ('picking.cancel', '取消任务与订单');

INSERT INTO demo_auth_role (code, name) VALUES
  ('warehouse_ops', '仓库运营'),
  ('picker', '拣货员'),
  ('warehouse_supervisor', '仓库主管');

INSERT INTO demo_auth_role_permission (role_id, permission_id)
SELECT r.id, p.id FROM demo_auth_role r
JOIN demo_auth_permission p
WHERE (r.code = 'warehouse_ops' AND p.code IN ('order.import', 'picking.view'))
   OR (r.code = 'picker' AND p.code IN ('picking.view', 'picking.complete'))
   OR (r.code = 'warehouse_supervisor' AND p.code IN (
        'picking.view', 'picking.complete', 'picking.force_complete', 'picking.cancel'
      ));

INSERT INTO demo_auth_user_role (user_id, role_id)
SELECT u.id, r.id FROM demo_auth_user u
JOIN demo_auth_role r
WHERE (u.username = 'ops' AND r.code = 'warehouse_ops')
   OR (u.username = 'picker' AND r.code = 'picker')
   OR (u.username = 'supervisor' AND r.code = 'warehouse_supervisor');
