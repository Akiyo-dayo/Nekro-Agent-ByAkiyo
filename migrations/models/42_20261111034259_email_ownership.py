from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "email_account" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "owner_adapter_key" VARCHAR(64) NOT NULL,
    "owner_instance_key" VARCHAR(64) NOT NULL,
    "username" VARCHAR(256) NOT NULL UNIQUE,
    "display_name" VARCHAR(128) NOT NULL DEFAULT '',
    "provider" VARCHAR(32) NOT NULL DEFAULT '自定义',
    "settings" JSONB NOT NULL,
    "password" TEXT NOT NULL,
    "enabled" BOOL NOT NULL DEFAULT True,
    "receive_enabled" BOOL NOT NULL DEFAULT True,
    "send_enabled" BOOL NOT NULL DEFAULT False,
    "is_default_sender" BOOL NOT NULL DEFAULT False,
    "notify_chat_key" VARCHAR(256) NOT NULL DEFAULT '',
    "attachment_dir" VARCHAR(128) NOT NULL DEFAULT '',
    "created_by" VARCHAR(64) NOT NULL DEFAULT '',
    "updated_by" VARCHAR(64) NOT NULL DEFAULT '',
    "create_time" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "update_time" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "idx_email_accou_owner_a_c6a3df" ON "email_account" ("owner_adapter_key", "owner_instance_key");
COMMENT ON COLUMN "email_account"."id" IS 'ID；新建账户的附件目录名取它';
COMMENT ON COLUMN "email_account"."owner_adapter_key" IS '归属的适配器标识';
COMMENT ON COLUMN "email_account"."owner_instance_key" IS '归属的实例标识';
COMMENT ON COLUMN "email_account"."username" IS '邮箱地址（去首尾空白、转小写后存）；与适配器配置里的地址合起来全局唯一';
COMMENT ON COLUMN "email_account"."display_name" IS '显示名；空则显示地址';
COMMENT ON COLUMN "email_account"."provider" IS '服务商预设键（与 EMAIL_PROVIDER_CONFIGS 的键一致），''自定义'' 表示自填服务器';
COMMENT ON COLUMN "email_account"."settings" IS '自定义服务器与端口、SSL、最近一次测试结果；选了预设的不存主机端口';
COMMENT ON COLUMN "email_account"."password" IS '授权码 / 密码（静态加密存储）';
COMMENT ON COLUMN "email_account"."enabled" IS '启用账户；关闭后不参与任何收发';
COMMENT ON COLUMN "email_account"."receive_enabled" IS '启用收信（IMAP）';
COMMENT ON COLUMN "email_account"."send_enabled" IS '启用发信（SMTP）';
COMMENT ON COLUMN "email_account"."is_default_sender" IS '同一归属实例对里最多一个默认发件人；由接口在保存时互斥';
COMMENT ON COLUMN "email_account"."notify_chat_key" IS '新邮件通知推到哪个频道；必须属于归属实例';
COMMENT ON COLUMN "email_account"."attachment_dir" IS '附件根目录名（相对 uploads/email_attachment/）：新建的是 _i<id>，转为实例自有的沿用原地址';
COMMENT ON COLUMN "email_account"."created_by" IS '创建者用户名';
COMMENT ON COLUMN "email_account"."updated_by" IS '最后修改者用户名';
COMMENT ON COLUMN "email_account"."create_time" IS '创建时间';
COMMENT ON COLUMN "email_account"."update_time" IS '更新时间';
COMMENT ON TABLE "email_account" IS '某个实例自己填的邮箱账户';
        CREATE TABLE IF NOT EXISTS "email_outbox" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "batch_key" VARCHAR(64) NOT NULL,
    "adapter_key" VARCHAR(64) NOT NULL,
    "instance_key" VARCHAR(64) NOT NULL,
    "platform_userid" VARCHAR(128) NOT NULL,
    "account_kind" VARCHAR(16) NOT NULL,
    "account_ref" VARCHAR(128) NOT NULL,
    "to_address" VARCHAR(512) NOT NULL,
    "subject" VARCHAR(256) NOT NULL DEFAULT '',
    "body" TEXT NOT NULL,
    "reason" VARCHAR(500) NOT NULL DEFAULT '',
    "status" VARCHAR(16) NOT NULL DEFAULT 'queued',
    "attempts" INT NOT NULL DEFAULT 0,
    "next_at" TIMESTAMPTZ,
    "create_time" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "update_time" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "idx_email_outbo_status_cb9ccc" ON "email_outbox" ("status", "next_at");
CREATE INDEX IF NOT EXISTS "idx_email_outbo_batch_k_96962f" ON "email_outbox" ("batch_key");
COMMENT ON COLUMN "email_outbox"."id" IS 'ID';
COMMENT ON COLUMN "email_outbox"."batch_key" IS '批次键：<节日名>:<日期>:<实例对哈希>';
COMMENT ON COLUMN "email_outbox"."adapter_key" IS '收件人名单属于哪个适配器';
COMMENT ON COLUMN "email_outbox"."instance_key" IS '收件人名单属于哪个实例';
COMMENT ON COLUMN "email_outbox"."platform_userid" IS '收件人的平台账号';
COMMENT ON COLUMN "email_outbox"."account_kind" IS '发件账户类型：platform / instance';
COMMENT ON COLUMN "email_outbox"."account_ref" IS '发件账户引用（口径同发信记录）';
COMMENT ON COLUMN "email_outbox"."to_address" IS '收件地址';
COMMENT ON COLUMN "email_outbox"."subject" IS '主题';
COMMENT ON COLUMN "email_outbox"."body" IS '正文；发出或被作废后清空';
COMMENT ON COLUMN "email_outbox"."reason" IS '发信理由（宿主写）';
COMMENT ON COLUMN "email_outbox"."status" IS '状态：queued / paused / sent / failed / skipped';
COMMENT ON COLUMN "email_outbox"."attempts" IS '已尝试发送次数；失败按 1 分钟、5 分钟重试两次';
COMMENT ON COLUMN "email_outbox"."next_at" IS '下一次可以发的时刻；空表示立即可发';
COMMENT ON COLUMN "email_outbox"."create_time" IS '创建时间';
COMMENT ON COLUMN "email_outbox"."update_time" IS '更新时间';
COMMENT ON TABLE "email_outbox" IS '节日邮件队列里的一封';
        CREATE TABLE IF NOT EXISTS "email_send_log" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "account_kind" VARCHAR(16) NOT NULL,
    "account_ref" VARCHAR(128) NOT NULL,
    "adapter_key" VARCHAR(64) NOT NULL DEFAULT '',
    "instance_key" VARCHAR(64) NOT NULL DEFAULT '',
    "chat_key" VARCHAR(256) NOT NULL DEFAULT '',
    "tenant_id" INT,
    "purpose" VARCHAR(16) NOT NULL,
    "to_address" VARCHAR(512) NOT NULL,
    "subject" VARCHAR(256) NOT NULL DEFAULT '',
    "reason" VARCHAR(500) NOT NULL DEFAULT '',
    "ok" BOOL NOT NULL DEFAULT False,
    "error" VARCHAR(256) NOT NULL DEFAULT '',
    "create_time" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "idx_email_send__adapter_812b30" ON "email_send_log" ("adapter_key", "instance_key", "create_time");
CREATE INDEX IF NOT EXISTS "idx_email_send__tenant__cd9286" ON "email_send_log" ("tenant_id", "create_time");
CREATE INDEX IF NOT EXISTS "idx_email_send__adapter_4954b0" ON "email_send_log" ("adapter_key", "instance_key", "purpose", "to_address", "create_time");
COMMENT ON COLUMN "email_send_log"."id" IS 'ID';
COMMENT ON COLUMN "email_send_log"."account_kind" IS '发件账户类型：platform / instance';
COMMENT ON COLUMN "email_send_log"."account_ref" IS '发件账户引用：平台方存地址 sha256 前 16 位，实例自有存 email_account.id';
COMMENT ON COLUMN "email_send_log"."adapter_key" IS '发信实例所属适配器';
COMMENT ON COLUMN "email_send_log"."instance_key" IS '发信实例标识';
COMMENT ON COLUMN "email_send_log"."chat_key" IS '发信的会话；节日邮件为空';
COMMENT ON COLUMN "email_send_log"."tenant_id" IS '发信时的租户快照；平台方为 NULL';
COMMENT ON COLUMN "email_send_log"."purpose" IS '用途：send_email / festival';
COMMENT ON COLUMN "email_send_log"."to_address" IS '收件地址';
COMMENT ON COLUMN "email_send_log"."subject" IS '主题（截断 256 字）';
COMMENT ON COLUMN "email_send_log"."reason" IS '发信理由（截断 500 字）';
COMMENT ON COLUMN "email_send_log"."ok" IS '是否发送成功';
COMMENT ON COLUMN "email_send_log"."error" IS '失败原因（截断 256 字），成功时为空';
COMMENT ON COLUMN "email_send_log"."create_time" IS '创建时间';
COMMENT ON TABLE "email_send_log" IS '一次发信尝试';
        CREATE TABLE IF NOT EXISTS "email_user_pref" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "adapter_key" VARCHAR(64) NOT NULL,
    "instance_key" VARCHAR(64) NOT NULL,
    "platform_userid" VARCHAR(128) NOT NULL,
    "email" VARCHAR(320) NOT NULL DEFAULT '',
    "festival_subscribed" BOOL NOT NULL DEFAULT False,
    "unsubscribed" BOOL NOT NULL DEFAULT False,
    "unsubscribed_at" TIMESTAMPTZ,
    "set_in_chat_key" VARCHAR(256) NOT NULL DEFAULT '',
    "create_time" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "update_time" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "uid_email_user__adapter_633f69" UNIQUE ("adapter_key", "instance_key", "platform_userid")
);
COMMENT ON COLUMN "email_user_pref"."id" IS 'ID';
COMMENT ON COLUMN "email_user_pref"."adapter_key" IS '记下这条偏好的实例所属适配器';
COMMENT ON COLUMN "email_user_pref"."instance_key" IS '记下这条偏好的实例标识';
COMMENT ON COLUMN "email_user_pref"."platform_userid" IS '宿主记下的发送者账号';
COMMENT ON COLUMN "email_user_pref"."email" IS '用户留的收件地址；空 = 用默认地址';
COMMENT ON COLUMN "email_user_pref"."festival_subscribed" IS '在群里说过要节日祝福邮件';
COMMENT ON COLUMN "email_user_pref"."unsubscribed" IS '说过别再发邮件；两类收件人都不再发';
COMMENT ON COLUMN "email_user_pref"."unsubscribed_at" IS '退订时间';
COMMENT ON COLUMN "email_user_pref"."set_in_chat_key" IS '最后一次设置发生在哪个频道';
COMMENT ON COLUMN "email_user_pref"."create_time" IS '创建时间';
COMMENT ON COLUMN "email_user_pref"."update_time" IS '更新时间';
COMMENT ON TABLE "email_user_pref" IS '某个用户在一个实例下的邮件偏好';
        ALTER TABLE "email" ADD "owner_adapter_key" VARCHAR(64) NOT NULL DEFAULT '';
        ALTER TABLE "email" ADD "owner_instance_key" VARCHAR(64) NOT NULL DEFAULT '';
        COMMENT ON COLUMN "email"."owner_adapter_key" IS '入库时这封信的账户归属的适配器标识（快照）；空 = 平台方';
COMMENT ON COLUMN "email"."owner_instance_key" IS '入库时这封信的账户归属的实例标识（快照）；空 = 平台方';"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "email" DROP COLUMN "owner_adapter_key";
        ALTER TABLE "email" DROP COLUMN "owner_instance_key";
        DROP TABLE IF EXISTS "email_send_log";
        DROP TABLE IF EXISTS "email_user_pref";
        DROP TABLE IF EXISTS "email_account";
        DROP TABLE IF EXISTS "email_outbox";"""


MODELS_STATE = (
    "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"
    "U02U+tpyhfKO1e+f5/ZbsD2Q=="
)
