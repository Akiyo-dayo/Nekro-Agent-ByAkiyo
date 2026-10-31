from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "affection" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "adapter_key" VARCHAR(64) NOT NULL,
    "instance_key" VARCHAR(64) NOT NULL DEFAULT '',
    "platform_userid" VARCHAR(256) NOT NULL,
    "nickname" VARCHAR(128) NOT NULL DEFAULT '',
    "score" INT NOT NULL DEFAULT 0,
    "stage" VARCHAR(32) NOT NULL DEFAULT 'stranger',
    "state_text" TEXT NOT NULL,
    "last_event_text" TEXT NOT NULL,
    "state_generated_at" TIMESTAMPTZ,
    "interaction_count" INT NOT NULL DEFAULT 0,
    "last_interact_at" TIMESTAMPTZ,
    "last_chat_key" VARCHAR(256) NOT NULL DEFAULT '',
    "create_time" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "update_time" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "uid_affection_adapter_d26481" UNIQUE ("adapter_key", "instance_key", "platform_userid")
);
CREATE INDEX IF NOT EXISTS "idx_affection_adapter_5fa332" ON "affection" ("adapter_key");
CREATE INDEX IF NOT EXISTS "idx_affection_instanc_aaec7d" ON "affection" ("instance_key");
CREATE INDEX IF NOT EXISTS "idx_affection_platfor_b69a67" ON "affection" ("platform_userid");
CREATE INDEX IF NOT EXISTS "idx_affection_score_0d3918" ON "affection" ("score");
CREATE INDEX IF NOT EXISTS "idx_affection_stage_aa457b" ON "affection" ("stage");
COMMENT ON COLUMN "affection"."id" IS 'ID';
COMMENT ON COLUMN "affection"."adapter_key" IS '适配器标识';
COMMENT ON COLUMN "affection"."instance_key" IS '适配器实例标识';
COMMENT ON COLUMN "affection"."platform_userid" IS '平台用户 ID';
COMMENT ON COLUMN "affection"."nickname" IS '最近一次见到的昵称';
COMMENT ON COLUMN "affection"."score" IS '好感度数值';
COMMENT ON COLUMN "affection"."stage" IS '好感度阶段';
COMMENT ON COLUMN "affection"."state_text" IS 'LLM 生成的状态描述（不可信内容）';
COMMENT ON COLUMN "affection"."last_event_text" IS 'LLM 生成的最近一次变化叙述（不可信内容）';
COMMENT ON COLUMN "affection"."state_generated_at" IS '状态描述生成时间';
COMMENT ON COLUMN "affection"."interaction_count" IS '累计互动轮次';
COMMENT ON COLUMN "affection"."last_interact_at" IS '最近互动时间';
COMMENT ON COLUMN "affection"."last_chat_key" IS '最近互动的会话标识';
COMMENT ON COLUMN "affection"."create_time" IS '创建时间';
COMMENT ON COLUMN "affection"."update_time" IS '更新时间';
COMMENT ON TABLE "affection" IS '好感度记录（每个实例下的每个平台用户一行）';
        CREATE TABLE IF NOT EXISTS "affection_event" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "adapter_key" VARCHAR(64) NOT NULL,
    "instance_key" VARCHAR(64) NOT NULL DEFAULT '',
    "platform_userid" VARCHAR(256) NOT NULL,
    "chat_key" VARCHAR(256) NOT NULL DEFAULT '',
    "delta" INT NOT NULL DEFAULT 0,
    "score_after" INT NOT NULL DEFAULT 0,
    "source" VARCHAR(16) NOT NULL DEFAULT 'llm',
    "reason" TEXT NOT NULL,
    "operator" VARCHAR(128) NOT NULL DEFAULT '',
    "create_time" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "idx_affection_e_adapter_ed0de2" ON "affection_event" ("adapter_key");
CREATE INDEX IF NOT EXISTS "idx_affection_e_instanc_a33d9c" ON "affection_event" ("instance_key");
CREATE INDEX IF NOT EXISTS "idx_affection_e_platfor_b5513f" ON "affection_event" ("platform_userid");
CREATE INDEX IF NOT EXISTS "idx_affection_e_chat_ke_28fbe1" ON "affection_event" ("chat_key");
CREATE INDEX IF NOT EXISTS "idx_affection_e_source_3e67cf" ON "affection_event" ("source");
CREATE INDEX IF NOT EXISTS "idx_affection_e_create__88ca17" ON "affection_event" ("create_time");
COMMENT ON COLUMN "affection_event"."id" IS 'ID';
COMMENT ON COLUMN "affection_event"."adapter_key" IS '适配器标识';
COMMENT ON COLUMN "affection_event"."instance_key" IS '适配器实例标识';
COMMENT ON COLUMN "affection_event"."platform_userid" IS '平台用户 ID';
COMMENT ON COLUMN "affection_event"."chat_key" IS '触发变化的会话标识';
COMMENT ON COLUMN "affection_event"."delta" IS '本次变化量';
COMMENT ON COLUMN "affection_event"."score_after" IS '变化后的好感度数值';
COMMENT ON COLUMN "affection_event"."source" IS '变化来源';
COMMENT ON COLUMN "affection_event"."reason" IS '变化原因叙述（不可信内容）';
COMMENT ON COLUMN "affection_event"."operator" IS '人工调整的操作者用户名';
COMMENT ON COLUMN "affection_event"."create_time" IS '创建时间';
COMMENT ON TABLE "affection_event" IS '好感度变化事件';"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "affection_event";
        DROP TABLE IF EXISTS "affection";"""


MODELS_STATE = (
    "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"
)
