from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "affection" DROP CONSTRAINT IF EXISTS "uid_affection_adapter_d26481";
        CREATE TABLE IF NOT EXISTS "identity_alias" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "domain_id" VARCHAR(96) NOT NULL,
    "platform_userid" VARCHAR(256) NOT NULL,
    "person_uid" VARCHAR(256) NOT NULL,
    "source" VARCHAR(16) NOT NULL DEFAULT 'manual',
    "operator" VARCHAR(128) NOT NULL DEFAULT '',
    "note" TEXT NOT NULL,
    "create_time" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "update_time" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "uid_identity_al_domain__b6fe47" UNIQUE ("domain_id", "platform_userid")
);
CREATE INDEX IF NOT EXISTS "idx_identity_al_domain__cee7b4" ON "identity_alias" ("domain_id");
CREATE INDEX IF NOT EXISTS "idx_identity_al_platfor_afdd6d" ON "identity_alias" ("platform_userid");
CREATE INDEX IF NOT EXISTS "idx_identity_al_person__8a88bb" ON "identity_alias" ("person_uid");
CREATE INDEX IF NOT EXISTS "idx_identity_al_source_6650a3" ON "identity_alias" ("source");
COMMENT ON COLUMN "identity_alias"."id" IS 'ID';
COMMENT ON COLUMN "identity_alias"."domain_id" IS '所属域标识（ScopeDomain.domain_id）';
COMMENT ON COLUMN "identity_alias"."platform_userid" IS '别名账号的平台用户 ID';
COMMENT ON COLUMN "identity_alias"."person_uid" IS '指向的人（= 主账号平台用户 ID）';
COMMENT ON COLUMN "identity_alias"."source" IS '别名来源';
COMMENT ON COLUMN "identity_alias"."operator" IS '建立本条别名的操作者用户名';
COMMENT ON COLUMN "identity_alias"."note" IS '备注';
COMMENT ON COLUMN "identity_alias"."create_time" IS '创建时间';
COMMENT ON COLUMN "identity_alias"."update_time" IS '更新时间';
COMMENT ON TABLE "identity_alias" IS '账号别名：`(域, 平台用户 ID) → 人`';
        CREATE TABLE IF NOT EXISTS "identity_person" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "domain_id" VARCHAR(96) NOT NULL,
    "person_uid" VARCHAR(256) NOT NULL,
    "display_name" VARCHAR(128) NOT NULL DEFAULT '',
    "note" TEXT NOT NULL,
    "create_time" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "update_time" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "uid_identity_pe_domain__9ccb1b" UNIQUE ("domain_id", "person_uid")
);
CREATE INDEX IF NOT EXISTS "idx_identity_pe_domain__d0b531" ON "identity_person" ("domain_id");
CREATE INDEX IF NOT EXISTS "idx_identity_pe_person__17b253" ON "identity_person" ("person_uid");
COMMENT ON COLUMN "identity_person"."id" IS 'ID';
COMMENT ON COLUMN "identity_person"."domain_id" IS '所属域标识（ScopeDomain.domain_id）';
COMMENT ON COLUMN "identity_person"."person_uid" IS '人的稳定标识（= 主账号平台用户 ID）';
COMMENT ON COLUMN "identity_person"."display_name" IS '显示名（后台展示用）';
COMMENT ON COLUMN "identity_person"."note" IS '备注';
COMMENT ON COLUMN "identity_person"."create_time" IS '创建时间';
COMMENT ON COLUMN "identity_person"."update_time" IS '更新时间';
COMMENT ON TABLE "identity_person" IS '「人」的档案行（**可选**，缺席不影响解析）';
        CREATE TABLE IF NOT EXISTS "instance_domain_binding" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "adapter_key" VARCHAR(128) NOT NULL,
    "instance_key" VARCHAR(128) NOT NULL DEFAULT '',
    "memory_group_id" INT,
    "affection_group_id" INT,
    "create_time" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "update_time" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "uid_instance_do_adapter_8dceab" UNIQUE ("adapter_key", "instance_key")
);
CREATE INDEX IF NOT EXISTS "idx_instance_do_adapter_3879dc" ON "instance_domain_binding" ("adapter_key");
CREATE INDEX IF NOT EXISTS "idx_instance_do_instanc_ce8f6e" ON "instance_domain_binding" ("instance_key");
CREATE INDEX IF NOT EXISTS "idx_instance_do_memory__8bece8" ON "instance_domain_binding" ("memory_group_id");
CREATE INDEX IF NOT EXISTS "idx_instance_do_affecti_28e083" ON "instance_domain_binding" ("affection_group_id");
COMMENT ON COLUMN "instance_domain_binding"."id" IS 'ID';
COMMENT ON COLUMN "instance_domain_binding"."adapter_key" IS '适配器标识';
COMMENT ON COLUMN "instance_domain_binding"."instance_key" IS '适配器实例标识';
COMMENT ON COLUMN "instance_domain_binding"."memory_group_id" IS '记忆域所属实例组 ID；NULL 表示自成一域';
COMMENT ON COLUMN "instance_domain_binding"."affection_group_id" IS '好感域所属实例组 ID；NULL 表示跟随记忆域';
COMMENT ON COLUMN "instance_domain_binding"."create_time" IS '创建时间';
COMMENT ON COLUMN "instance_domain_binding"."update_time" IS '更新时间';
COMMENT ON TABLE "instance_domain_binding" IS '实例的域归属（只有被显式放进某个组的实例才有一行）';
        CREATE TABLE IF NOT EXISTS "instance_group" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "name" VARCHAR(128) NOT NULL,
    "note" TEXT NOT NULL,
    "tenant_id" INT,
    "create_time" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "update_time" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "idx_instance_gr_name_f37cc9" ON "instance_group" ("name");
CREATE INDEX IF NOT EXISTS "idx_instance_gr_tenant__c70338" ON "instance_group" ("tenant_id");
COMMENT ON COLUMN "instance_group"."id" IS 'ID';
COMMENT ON COLUMN "instance_group"."name" IS '组名称（仅用于展示，不要求唯一）';
COMMENT ON COLUMN "instance_group"."note" IS '备注';
COMMENT ON COLUMN "instance_group"."tenant_id" IS '所属租户ID；NULL 表示归平台方';
COMMENT ON COLUMN "instance_group"."create_time" IS '创建时间';
COMMENT ON COLUMN "instance_group"."update_time" IS '更新时间';
COMMENT ON TABLE "instance_group" IS '实例组（记忆域）';
        ALTER TABLE "affection" ADD "domain_group_id" INT NOT NULL DEFAULT 0;
        ALTER TABLE "affection" ADD "preset_id" INT NOT NULL DEFAULT 0;
        COMMENT ON COLUMN "affection"."adapter_key" IS '自成一域时的适配器标识；属于实例组时为空串';
        COMMENT ON COLUMN "affection"."platform_userid" IS '人的标识（= 主账号平台用户 ID）';
        COMMENT ON COLUMN "affection"."instance_key" IS '自成一域时的实例标识；属于实例组时为空串';
        ALTER TABLE "affection_event" ADD "source_platform_userid" VARCHAR(256) NOT NULL DEFAULT '';
        ALTER TABLE "affection_event" ADD "domain_group_id" INT NOT NULL DEFAULT 0;
        ALTER TABLE "affection_event" ADD "preset_id" INT NOT NULL DEFAULT 0;
        COMMENT ON COLUMN "affection_event"."adapter_key" IS '自成一域时的适配器标识；属于实例组时为空串';
        COMMENT ON COLUMN "affection_event"."platform_userid" IS '人的标识（= 主账号平台用户 ID）';
        COMMENT ON COLUMN "affection_event"."instance_key" IS '自成一域时的实例标识；属于实例组时为空串';
        COMMENT ON COLUMN "affection"."domain_group_id" IS '所属实例组 ID；0 表示自成一域';
COMMENT ON COLUMN "affection"."preset_id" IS '人设标识；0 表示无人设档';
COMMENT ON COLUMN "affection_event"."source_platform_userid" IS '触发本条事件的原始账号；空串表示与 platform_userid 相同';
COMMENT ON COLUMN "affection_event"."domain_group_id" IS '所属实例组 ID；0 表示自成一域';
COMMENT ON COLUMN "affection_event"."preset_id" IS '人设标识；0 表示无人设档';
        CREATE UNIQUE INDEX IF NOT EXISTS "uid_affection_adapter_ef7bdd" ON "affection" ("adapter_key", "instance_key", "domain_group_id", "platform_userid", "preset_id");
        CREATE INDEX IF NOT EXISTS "idx_affection_domain__0e8ecd" ON "affection" ("domain_group_id");
        CREATE INDEX IF NOT EXISTS "idx_affection_preset__83e9c3" ON "affection" ("preset_id");
        CREATE INDEX IF NOT EXISTS "idx_affection_e_source__9d2794" ON "affection_event" ("source_platform_userid");
        CREATE INDEX IF NOT EXISTS "idx_affection_e_domain__82a24e" ON "affection_event" ("domain_group_id");
        CREATE INDEX IF NOT EXISTS "idx_affection_e_preset__f6eab1" ON "affection_event" ("preset_id");"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP INDEX IF EXISTS "idx_affection_e_preset__f6eab1";
        DROP INDEX IF EXISTS "idx_affection_e_domain__82a24e";
        DROP INDEX IF EXISTS "idx_affection_e_source__9d2794";
        DROP INDEX IF EXISTS "idx_affection_preset__83e9c3";
        DROP INDEX IF EXISTS "idx_affection_domain__0e8ecd";
        DROP INDEX IF EXISTS "uid_affection_adapter_ef7bdd";
        ALTER TABLE "affection" DROP COLUMN "domain_group_id";
        ALTER TABLE "affection" DROP COLUMN "preset_id";
        COMMENT ON COLUMN "affection"."adapter_key" IS '适配器标识';
        COMMENT ON COLUMN "affection"."platform_userid" IS '平台用户 ID';
        COMMENT ON COLUMN "affection"."instance_key" IS '适配器实例标识';
        ALTER TABLE "affection_event" DROP COLUMN "source_platform_userid";
        ALTER TABLE "affection_event" DROP COLUMN "domain_group_id";
        ALTER TABLE "affection_event" DROP COLUMN "preset_id";
        COMMENT ON COLUMN "affection_event"."adapter_key" IS '适配器标识';
        COMMENT ON COLUMN "affection_event"."platform_userid" IS '平台用户 ID';
        COMMENT ON COLUMN "affection_event"."instance_key" IS '适配器实例标识';
        DROP TABLE IF EXISTS "identity_person";
        DROP TABLE IF EXISTS "instance_domain_binding";
        DROP TABLE IF EXISTS "identity_alias";
        DROP TABLE IF EXISTS "instance_group";
        CREATE UNIQUE INDEX IF NOT EXISTS "uid_affection_adapter_d26481" ON "affection" ("adapter_key", "instance_key", "platform_userid");"""


MODELS_STATE = (
    "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"
    "cMrrsI4mLBt+PYeokgmtDr5d//H9CSdpE="
)
