from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "model_group" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "name" VARCHAR(128) NOT NULL UNIQUE,
    "ownership" VARCHAR(32) NOT NULL DEFAULT 'platform',
    "owner_tenant_id" INT,
    "visibility" VARCHAR(32) NOT NULL DEFAULT 'shared',
    "group_name" VARCHAR(128) NOT NULL DEFAULT '',
    "chat_model" VARCHAR(256) NOT NULL DEFAULT '',
    "chat_proxy" VARCHAR(512) NOT NULL DEFAULT '',
    "base_url" VARCHAR(512) NOT NULL DEFAULT '',
    "api_key" TEXT NOT NULL,
    "model_type" VARCHAR(32) NOT NULL DEFAULT 'chat',
    "enable_vision" BOOL NOT NULL DEFAULT False,
    "enable_cot" BOOL NOT NULL DEFAULT False,
    "token_input_rate" DOUBLE PRECISION NOT NULL DEFAULT 1,
    "token_completion_rate" DOUBLE PRECISION NOT NULL DEFAULT 1,
    "model_price_rate" DOUBLE PRECISION NOT NULL DEFAULT 1,
    "temperature" DOUBLE PRECISION,
    "top_p" DOUBLE PRECISION,
    "top_k" INT,
    "presence_penalty" DOUBLE PRECISION,
    "frequency_penalty" DOUBLE PRECISION,
    "extra_body" TEXT,
    "create_time" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "update_time" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "idx_model_group_ownersh_e92b52" ON "model_group" ("ownership");
CREATE INDEX IF NOT EXISTS "idx_model_group_owner_t_f20718" ON "model_group" ("owner_tenant_id");
COMMENT ON COLUMN "model_group"."id" IS 'ID';
COMMENT ON COLUMN "model_group"."name" IS '模型组名（全局唯一，等同运行时视图的字典键）';
COMMENT ON COLUMN "model_group"."ownership" IS '归属：platform=平台方 / tenant=租户自建';
COMMENT ON COLUMN "model_group"."owner_tenant_id" IS '归属租户ID；平台组为 NULL';
COMMENT ON COLUMN "model_group"."visibility" IS '平台组对租户的开放程度：shared=租户可选用 / private=仅平台方';
COMMENT ON COLUMN "model_group"."group_name" IS '模型组显示名称';
COMMENT ON COLUMN "model_group"."chat_model" IS '聊天模型名称';
COMMENT ON COLUMN "model_group"."chat_proxy" IS '聊天模型访问代理';
COMMENT ON COLUMN "model_group"."base_url" IS '接口地址';
COMMENT ON COLUMN "model_group"."api_key" IS '接口凭据';
COMMENT ON COLUMN "model_group"."model_type" IS '模型类型 chat/embedding/draw';
COMMENT ON COLUMN "model_group"."enable_vision" IS '启用视觉功能';
COMMENT ON COLUMN "model_group"."enable_cot" IS '启用外置思维链';
COMMENT ON COLUMN "model_group"."token_input_rate" IS '输入 Token 倍率';
COMMENT ON COLUMN "model_group"."token_completion_rate" IS '补全 Token 倍率';
COMMENT ON COLUMN "model_group"."model_price_rate" IS '模型价格倍率';
COMMENT ON COLUMN "model_group"."temperature" IS '温度值';
COMMENT ON COLUMN "model_group"."top_p" IS 'Top P';
COMMENT ON COLUMN "model_group"."top_k" IS 'Top K';
COMMENT ON COLUMN "model_group"."presence_penalty" IS '提示重复惩罚';
COMMENT ON COLUMN "model_group"."frequency_penalty" IS '补全重复惩罚';
COMMENT ON COLUMN "model_group"."extra_body" IS '额外参数 (JSON)';
COMMENT ON COLUMN "model_group"."create_time" IS '创建时间';
COMMENT ON COLUMN "model_group"."update_time" IS '更新时间';
COMMENT ON TABLE "model_group" IS '模型组';
        CREATE TABLE IF NOT EXISTS "tenant" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "name" VARCHAR(64) NOT NULL UNIQUE,
    "display_name" VARCHAR(128) NOT NULL DEFAULT '',
    "status" VARCHAR(32) NOT NULL DEFAULT 'active',
    "contact" VARCHAR(256) NOT NULL DEFAULT '',
    "note" TEXT NOT NULL,
    "create_time" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "update_time" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "idx_tenant_status_b1da0a" ON "tenant" ("status");
COMMENT ON COLUMN "tenant"."id" IS '租户ID';
COMMENT ON COLUMN "tenant"."name" IS '租户标识（唯一，创建后不可改）';
COMMENT ON COLUMN "tenant"."display_name" IS '显示名称';
COMMENT ON COLUMN "tenant"."status" IS '租户状态';
COMMENT ON COLUMN "tenant"."contact" IS '联系方式';
COMMENT ON COLUMN "tenant"."note" IS '备注';
COMMENT ON COLUMN "tenant"."create_time" IS '创建时间';
COMMENT ON COLUMN "tenant"."update_time" IS '更新时间';
COMMENT ON TABLE "tenant" IS '租户（定制版客户）';
        ALTER TABLE "adapter_instance" ADD "tenant_id" INT;
        ALTER TABLE "user" ADD "tenant_id" INT;
        ALTER TABLE "user" ADD "tenant_role" VARCHAR(32) NOT NULL DEFAULT 'platform_owner';
        COMMENT ON COLUMN "adapter_instance"."tenant_id" IS '所属租户ID；NULL 表示归平台方';
COMMENT ON COLUMN "user"."tenant_id" IS '所属租户ID；NULL 表示归平台方';
COMMENT ON COLUMN "user"."tenant_role" IS '租户体系下的角色';
        CREATE INDEX IF NOT EXISTS "idx_adapter_ins_tenant__3cad21" ON "adapter_instance" ("tenant_id");
        CREATE INDEX IF NOT EXISTS "idx_user_tenant__30ef43" ON "user" ("tenant_id");
        CREATE INDEX IF NOT EXISTS "idx_user_tenant__da5dda" ON "user" ("tenant_role");"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP INDEX IF EXISTS "idx_adapter_ins_tenant__3cad21";
        DROP INDEX IF EXISTS "idx_user_tenant__da5dda";
        DROP INDEX IF EXISTS "idx_user_tenant__30ef43";
        ALTER TABLE "user" DROP COLUMN "tenant_id";
        ALTER TABLE "user" DROP COLUMN "tenant_role";
        ALTER TABLE "adapter_instance" DROP COLUMN "tenant_id";
        DROP TABLE IF EXISTS "tenant";
        DROP TABLE IF EXISTS "model_group";"""


MODELS_STATE = (
    "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"
    "ghjOwQ03QVws+XYcWy+RRJN6vfz7/wPgA6w6"
)
