from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "wb_book" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "name" VARCHAR(128) NOT NULL,
    "description" TEXT NOT NULL,
    "scope_kind" VARCHAR(16) NOT NULL,
    "adapter_key" VARCHAR(64) NOT NULL DEFAULT '',
    "instance_key" VARCHAR(64) NOT NULL DEFAULT '',
    "preset_id" INT NOT NULL DEFAULT 0,
    "chat_key" VARCHAR(256) NOT NULL DEFAULT '',
    "person_account" VARCHAR(128) NOT NULL DEFAULT '',
    "enabled" BOOL NOT NULL DEFAULT True,
    "scan_depth" INT NOT NULL DEFAULT 4,
    "budget_chars" INT NOT NULL DEFAULT 0,
    "case_sensitive" BOOL NOT NULL DEFAULT False,
    "match_whole_words" BOOL NOT NULL DEFAULT False,
    "ext" JSONB NOT NULL,
    "created_by" VARCHAR(64) NOT NULL DEFAULT '',
    "updated_by" VARCHAR(64) NOT NULL DEFAULT '',
    "create_time" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "update_time" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "idx_wb_book_chat_ke_e4d394" ON "wb_book" ("chat_key");
CREATE INDEX IF NOT EXISTS "idx_wb_book_adapter_3e9624" ON "wb_book" ("adapter_key", "instance_key");
CREATE INDEX IF NOT EXISTS "idx_wb_book_scope_k_5c9bb1" ON "wb_book" ("scope_kind", "preset_id");
CREATE INDEX IF NOT EXISTS "idx_wb_book_adapter_abf006" ON "wb_book" ("adapter_key", "instance_key", "person_account");
COMMENT ON COLUMN "wb_book"."id" IS '主键 ID';
COMMENT ON COLUMN "wb_book"."name" IS '书名，只给人看';
COMMENT ON COLUMN "wb_book"."description" IS '书的说明，只给人看，不进提示词';
COMMENT ON COLUMN "wb_book"."scope_kind" IS '作用域类型 domain / preset / channel / person';
COMMENT ON COLUMN "wb_book"."adapter_key" IS '归属实例的适配器标识';
COMMENT ON COLUMN "wb_book"."instance_key" IS '归属实例标识';
COMMENT ON COLUMN "wb_book"."preset_id" IS 'preset 书绑定的人设；其余为 0';
COMMENT ON COLUMN "wb_book"."chat_key" IS 'channel 书绑定的频道';
COMMENT ON COLUMN "wb_book"."person_account" IS 'person 书绑定的人的平台账号';
COMMENT ON COLUMN "wb_book"."enabled" IS '整本书是否生效';
COMMENT ON COLUMN "wb_book"."scan_depth" IS '扫描最近几条消息，1 到 50';
COMMENT ON COLUMN "wb_book"."budget_chars" IS '本书每轮最多注入多少字；0 表示不单独限制';
COMMENT ON COLUMN "wb_book"."case_sensitive" IS '条目没单独设时的大小写默认';
COMMENT ON COLUMN "wb_book"."match_whole_words" IS '条目没单独设时的整词默认';
COMMENT ON COLUMN "wb_book"."ext" IS '导入时酒馆书级的其余字段，导出时还原';
COMMENT ON COLUMN "wb_book"."created_by" IS '创建者用户名';
COMMENT ON COLUMN "wb_book"."updated_by" IS '最后修改者用户名';
COMMENT ON COLUMN "wb_book"."create_time" IS '创建时间';
COMMENT ON COLUMN "wb_book"."update_time" IS '更新时间';
COMMENT ON TABLE "wb_book" IS '一本世界书';
        CREATE TABLE IF NOT EXISTS "wb_entry" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "uid" INT NOT NULL,
    "comment" VARCHAR(256) NOT NULL DEFAULT '',
    "keys" JSONB NOT NULL,
    "secondary_keys" JSONB NOT NULL,
    "selective_logic" VARCHAR(8) NOT NULL DEFAULT 'and_any',
    "use_regex" BOOL NOT NULL DEFAULT False,
    "constant" BOOL NOT NULL DEFAULT False,
    "content" TEXT NOT NULL,
    "order" INT NOT NULL DEFAULT 100,
    "position" VARCHAR(16) NOT NULL DEFAULT 'before_history',
    "depth" INT NOT NULL DEFAULT 4,
    "scan_depth" INT,
    "case_sensitive" BOOL,
    "match_whole_words" BOOL,
    "sticky" INT,
    "cooldown" INT,
    "delay" INT,
    "enabled" BOOL NOT NULL DEFAULT True,
    "trusted" BOOL NOT NULL DEFAULT False,
    "status" VARCHAR(16) NOT NULL DEFAULT 'active',
    "quarantined_at" TIMESTAMPTZ,
    "quarantine_reason" VARCHAR(16) NOT NULL DEFAULT '',
    "blocked_round_hits" INT NOT NULL DEFAULT 0,
    "ext" JSONB NOT NULL,
    "created_by" VARCHAR(64) NOT NULL DEFAULT '',
    "updated_by" VARCHAR(64) NOT NULL DEFAULT '',
    "create_time" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "update_time" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "book_id" INT NOT NULL REFERENCES "wb_book" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_wb_entry_book_id_8fa072" UNIQUE ("book_id", "uid")
);
CREATE INDEX IF NOT EXISTS "idx_wb_entry_book_id_4dd12a" ON "wb_entry" ("book_id");
COMMENT ON COLUMN "wb_entry"."id" IS '主键 ID';
COMMENT ON COLUMN "wb_entry"."uid" IS '书内编号，沿用酒馆的 uid';
COMMENT ON COLUMN "wb_entry"."comment" IS '标题，只给人看';
COMMENT ON COLUMN "wb_entry"."keys" IS '主关键词数组';
COMMENT ON COLUMN "wb_entry"."secondary_keys" IS '次关键词数组';
COMMENT ON COLUMN "wb_entry"."selective_logic" IS '次关键词逻辑 and_any / and_all / not_any / not_all';
COMMENT ON COLUMN "wb_entry"."use_regex" IS '为真时每个关键词都按正则解释';
COMMENT ON COLUMN "wb_entry"."constant" IS '常驻：不看关键词，书生效就注入';
COMMENT ON COLUMN "wb_entry"."content" IS '注入的正文';
COMMENT ON COLUMN "wb_entry"."order" IS '入选排序用的权重，大的先入选';
COMMENT ON COLUMN "wb_entry"."position" IS '注入位置 after_persona / before_history / at_depth';
COMMENT ON COLUMN "wb_entry"."depth" IS 'at_depth 用的深度，0 到 100';
COMMENT ON COLUMN "wb_entry"."scan_depth" IS '扫描深度；空表示用书的';
COMMENT ON COLUMN "wb_entry"."case_sensitive" IS '大小写；空表示用书的';
COMMENT ON COLUMN "wb_entry"."match_whole_words" IS '整词匹配；空表示用书的';
COMMENT ON COLUMN "wb_entry"."sticky" IS '命中后保持多少条消息；空或 0 表示不用';
COMMENT ON COLUMN "wb_entry"."cooldown" IS '命中后多少条消息内不再命中；空或 0 表示不用';
COMMENT ON COLUMN "wb_entry"."delay" IS '频道消息少于这个数时不命中；空或 0 表示不用';
COMMENT ON COLUMN "wb_entry"."enabled" IS '条目是否启用';
COMMENT ON COLUMN "wb_entry"."trusted" IS '按宿主内容注入';
COMMENT ON COLUMN "wb_entry"."status" IS '状态 active / quarantined';
COMMENT ON COLUMN "wb_entry"."quarantined_at" IS '被隔离的时间';
COMMENT ON COLUMN "wb_entry"."quarantine_reason" IS '隔离原因 manual / auto_blocked';
COMMENT ON COLUMN "wb_entry"."blocked_round_hits" IS '注入后被拦的轮次数，用于自动隔离';
COMMENT ON COLUMN "wb_entry"."ext" IS '酒馆条目的原始内容与插件自有的 na_ 附加信息';
COMMENT ON COLUMN "wb_entry"."created_by" IS '创建者用户名';
COMMENT ON COLUMN "wb_entry"."updated_by" IS '最后修改者用户名';
COMMENT ON COLUMN "wb_entry"."create_time" IS '创建时间';
COMMENT ON COLUMN "wb_entry"."update_time" IS '更新时间';
COMMENT ON COLUMN "wb_entry"."book_id" IS '所属的书；删书连带删条目';
COMMENT ON TABLE "wb_entry" IS '世界书里的一条条目';
        ALTER TABLE "mem_prompt_round" ADD "worldbook_state" VARCHAR(16) NOT NULL DEFAULT 'disabled';
        ALTER TABLE "mem_prompt_round" ADD "worldbook_chars" INT NOT NULL DEFAULT 0;
        COMMENT ON COLUMN "mem_prompt_round"."worldbook_items" IS '世界书条目引用与字数';
        COMMENT ON COLUMN "mem_prompt_round"."worldbook_state" IS '世界书状态 injected / none / disabled / suppressed / error';
COMMENT ON COLUMN "mem_prompt_round"."worldbook_chars" IS '世界书正文总字数';"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "mem_prompt_round" DROP COLUMN "worldbook_state";
        ALTER TABLE "mem_prompt_round" DROP COLUMN "worldbook_chars";
        COMMENT ON COLUMN "mem_prompt_round"."worldbook_items" IS '世界书条目引用；当前恒为空';
        DROP TABLE IF EXISTS "wb_entry";
        DROP TABLE IF EXISTS "wb_book";"""


MODELS_STATE = (
    "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"
    "rr8J4mrJt9PYeo0kmtTr5R//P+wHin8="
)
