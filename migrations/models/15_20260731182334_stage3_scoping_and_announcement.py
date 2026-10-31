from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "announcement" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "content" TEXT NOT NULL,
    "scope_type" VARCHAR(16) NOT NULL,
    "scope_value" TEXT NOT NULL,
    "status" VARCHAR(16) NOT NULL DEFAULT 'pending',
    "total" INT NOT NULL DEFAULT 0,
    "success_count" INT NOT NULL DEFAULT 0,
    "failure_count" INT NOT NULL DEFAULT 0,
    "failure_reason" TEXT NOT NULL,
    "created_by" VARCHAR(128) NOT NULL DEFAULT '',
    "create_time" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "finish_time" TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS "idx_announcemen_scope_t_a35c1a" ON "announcement" ("scope_type");
CREATE INDEX IF NOT EXISTS "idx_announcemen_status_897e30" ON "announcement" ("status");
CREATE INDEX IF NOT EXISTS "idx_announcemen_create__374ca5" ON "announcement" ("create_time");
COMMENT ON COLUMN "announcement"."id" IS 'ID';
COMMENT ON COLUMN "announcement"."content" IS '公告内容';
COMMENT ON COLUMN "announcement"."scope_type" IS '范围类型: all/instances/channels';
COMMENT ON COLUMN "announcement"."scope_value" IS '范围取值(JSON 数组)';
COMMENT ON COLUMN "announcement"."status" IS '任务状态: pending/running/completed/failed/cancelled';
COMMENT ON COLUMN "announcement"."total" IS '目标会话总数';
COMMENT ON COLUMN "announcement"."success_count" IS '成功投递数';
COMMENT ON COLUMN "announcement"."failure_count" IS '失败投递数';
COMMENT ON COLUMN "announcement"."failure_reason" IS '任务级失败原因(熔断/重启中断等)';
COMMENT ON COLUMN "announcement"."created_by" IS '提交人用户名';
COMMENT ON COLUMN "announcement"."create_time" IS '创建时间';
COMMENT ON COLUMN "announcement"."finish_time" IS '结束时间';
COMMENT ON TABLE "announcement" IS '机器人公告任务';
        CREATE TABLE IF NOT EXISTS "announcement_delivery" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "announcement_id" INT NOT NULL,
    "chat_key" VARCHAR(256) NOT NULL,
    "channel_name" VARCHAR(128) NOT NULL DEFAULT '',
    "adapter_key" VARCHAR(64) NOT NULL DEFAULT '',
    "instance_key" VARCHAR(64) NOT NULL DEFAULT '',
    "ok" BOOL NOT NULL DEFAULT False,
    "error" TEXT NOT NULL,
    "send_time" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "idx_announcemen_announc_c5163c" ON "announcement_delivery" ("announcement_id");
CREATE INDEX IF NOT EXISTS "idx_announcemen_chat_ke_38a8c9" ON "announcement_delivery" ("chat_key");
CREATE INDEX IF NOT EXISTS "idx_announcemen_instanc_f675d0" ON "announcement_delivery" ("instance_key");
COMMENT ON COLUMN "announcement_delivery"."id" IS 'ID';
COMMENT ON COLUMN "announcement_delivery"."announcement_id" IS '所属公告任务 ID';
COMMENT ON COLUMN "announcement_delivery"."chat_key" IS '目标会话唯一标识';
COMMENT ON COLUMN "announcement_delivery"."channel_name" IS '投递时的会话名称快照';
COMMENT ON COLUMN "announcement_delivery"."adapter_key" IS '适配器标识';
COMMENT ON COLUMN "announcement_delivery"."instance_key" IS '适配器实例标识';
COMMENT ON COLUMN "announcement_delivery"."ok" IS '是否投递成功';
COMMENT ON COLUMN "announcement_delivery"."error" IS '失败原因';
COMMENT ON COLUMN "announcement_delivery"."send_time" IS '投递时间';
COMMENT ON TABLE "announcement_delivery" IS '机器人公告单条投递结果';
        ALTER TABLE "presets" ADD "owner_instance_key" VARCHAR(64) NOT NULL DEFAULT '';
        COMMENT ON COLUMN "presets"."owner_instance_key" IS '所属实例标识，空表示全局共享人设';
        CREATE INDEX IF NOT EXISTS "idx_presets_owner_i_badafd" ON "presets" ("owner_instance_key");"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP INDEX IF EXISTS "idx_presets_owner_i_badafd";
        ALTER TABLE "presets" DROP COLUMN "owner_instance_key";
        DROP TABLE IF EXISTS "announcement_delivery";
        DROP TABLE IF EXISTS "announcement";"""


MODELS_STATE = (
    "eJztffmT20aS7r/C6J+8EW0LxA3Hi43QtTvatWSPJL/dN5aDUQAK3RiRAAcEJWuO//1VVu"
    "EoAAU2AB5V3UQ4Qu4mK0F2Zl35ZeaX/7jZpCFe73549eJ5iLY5zt4kuxwlAb75cfGPmwRt"
    "4If+QbeLG7Td1kPghRz5ayqF2OBVzI/2d3mGgpy8H6H1DpOXQrwLsnibx2kCUp/2nqbp5N"
    "+lGX7aW7btkn99D3/am5Hrf9rbSF+SVxz4OYq04NPesXQyxsQuhpEWjNeQC89ZovJdy4Of"
    "+eew5zuRTV4xyCfCtwvTgHy9OLmT/UX2Sfy3PV7l6R3O73FGvs5vv1X6/Iy/wZhSrfT333"
    "+nr4T4D7yD0fDr9vMqivE6bJgyDkGWvr7Kv23pa2+S/D/oQFCBvwrS9X6T1IO33/L7NKlG"
    "x0kOr97hBGcox/D4PNuDJZP9el0YvzQu+0PqIewrcjIhjtB+DfMBpLvT4c2rtmGKcUGawF"
    "Qi32ZH/8A7+JTv9aXpmK5hmy4ZQr9J9YrzL/bn1X87E6Sf8u7jzb/o+yhHbARVY623lvKb"
    "Cnx5jzKxBltiLVWSP6CtylJxh3RZvjBamcIpbekRTFpNI1Pa1ZxPe9cP7IFK36A/Vmuc3O"
    "X35Nel7h5Q8f99/v7ln56//46M+jd4ekq2AbZPvCve0tl7YAduvvKzfITi23Ln0ny9i01Q"
    "PbcLKG6GMN5t1+jbiv4+wgxtucuZ4eZGZITGBm4b5GfHM2BDNjXYhL1Im6Jy3bIHqJyM6l"
    "U5fa+pcjJ/8/1ujLJriYvtMzdbnISgqIeU7ei+TVSuacspCjb0Afo19F71wltN7eIE/mrB"
    "YfgiTdcYJWINc1ItFftE7Ew6rl5oqde2YcuwTN2m/0bsijFMvQfU+eLnn3+Ch2x2u7+t6Q"
    "tvPrb0+uvbF6/JFkLVTQbFOeYPUW7z3q3KP2OcnpuCZ1N1d9fovZTyyvZw6ML+jEx+jquk"
    "+G2WfolDnI3ZPXgZ2du07cB2bOmI3LBtI9RAwx5s2UTdypyJpcJWKAjSfZKvRHfrh/XdEl"
    "dZ9WTSh3S3MSLn2GvKWc7MDc4RvcL/dUf+no4xPuI/ejydjqBsMzTuhkvTAAM4cDc0bPzd"
    "f334+d2/Hb3ffHz9vx8b+02p2u/ePv9f+vjNt+Kdn35+95/lcM4UL3/6+UXLAmu0y1c4y1"
    "LB3tOv/qaUbN2TJUA07UYhWQKetYRp72O4nUcYFoVmRwrrHgV5/AWvkODIfUW0l8cbfMAG"
    "DemWHcJC/IfyhwFWKe4uJ3GgeLPYoeHDbhTQhRHBoWxF5vFmefP29YePz9/+0rDNq+cfX8"
    "M7esMu5avftXeo6iGL/3nz8U8L+HXxl5/fvaYqTXf5XUY/sR738S838J3QPk9XSfp1hUJe"
    "MeXL5UsNkydkSa0yYoOvEyzeEb6IwQdjFcQZhnPHB5jNwTiEGbCMrtvezFpkN0ozvNrEyT"
    "7HAhexF9DrE38Y4jvVjmtrwqVNLxqWTpFQztSWroGpTS9i598lscBa6UGGiRZW5QoYs8Ra"
    "oidYYCfFpCx9CfcLMm78siKTCYU/J+tvxZp+JMus2H4OrrL9Npxq8Jaoaga37cikd3ntig"
    "1OvzxESKLPHNYPL/go+PwVZeGq806qp31ju29t9E37FZSgO2or0C18y76Y2usvOMlvhkTf"
    "2MjbMSG4Fa5kjg7EmZj9C5PocCzM9WG6WZFl9WGC4FICpsKmJzn4Mb3owXXPNYPmZ00K1s"
    "n+soKA3hyxO2/Erprz4xTYlDrXxeiYgF01KReXVTUHnsMuwpTS0Ww/4NWUkh4K5ZepEzg+"
    "2xKmQFm2OQDJss1eIAveEsV+VlGWbsYHgCox2TgKv00Wd3vFokCFwvJ0gpaZkFI6NuEoUk"
    "3HG7zbkbvPODS2EpGt38bFIXRdlfG/Lfq2TlE4Gvxuy6mkczdyyL7hGo6jMuo9IwRP2mFk"
    "CIGqHuMHslsyez3sM5Zjb0d5jTtO6ni/MYIMSNcPwzE5lLyDVThhS4oTGpA3yZwwcv7Bu7"
    "5tNd21aR6jtK85+4qzr/h0fMVi71jBrXmUu9gRlH4t4XYEsua1Y6/ap3cbyV0iJD52TP6g"
    "0ZdAkaxsjfN75+NIf9h9SwI2Y0cbQCAqXf/Ck0pl/eM/tjG55U0IhjclFUt9cKPAmcPfRf"
    "wahxPM25RUzLx8Zsuc6cAlMwX7bCfKJOu/NbTEZO+gvGX53dTGhssyKKfcHKzlEJSOjOq9"
    "O9D3BNljBfK2yvAmzcXX4Qd0L3yCSmYg/4IDhyCjr0b0ht+Pz5/FOqNJV4AmzfkmV2Nwxf"
    "JNkiTdJwHe9OeZ8CNuDyKF7ZED0EHboQV3FJ8wsQ8/L+0AjkcTUdTfZ+UHAsRujOiMol0c"
    "RSMflxcTYTDqUIvIr9DlJ5O1dGkC0NB6o4sDDUG6xaNTMJpS0lMwXEMzYD3DGVGnYPy4QO"
    "v1sxJc3T0L7sk2U+w8owu/hlzPlv23s2W3LJeq8Ata70eFtFtiF7wQ//a78ErM694yQqjq"
    "0oyAYjuLEm1zcGAqCvQ8/uro+rjiwbUfF4XMs2yfJPD/IN1s15h8r2cRitfkfwEsi3VRhi"
    "x/QeRpjtYjDsxq/OWKDoQ1B46NcVnE2Aj2aYYvs9pgtw/Irrdb0aLQEXrtyEnWr60vaU0H"
    "rd3QPYvy4OgyNQvrZ5/h0ZrtyEnWrOUZS1qKa6mmWeKxjYx6dCVlY0WNfRkjp6lvy4D5bN"
    "lY+468vfSod4vCZxB3DcKSiMHEesjeIIN801P0EGVoUbjyR1HrNKVkm6uoUcfILH1Clpth"
    "64bDeF0mHZPnIAx4bLje4Hv8DOuV+mvuinES7+4n2bslqli8ysEh1F071hXGqJTE8V7hdf"
    "wFZ99uHsTzqpG3Q3G9VciLHA3wWQZUVtmOvWxeXso55QWjgb9hj5wBwcuTJvLTaJQSBZLS"
    "0+ts3QSHJrBwH+4sLckuuEf5WIZEXkY6EtjnjJ+OFvE8kVoGTY6mRWzLSb9EN7xIWhFnu2"
    "bLFBVFIpTHYiiJXdqT0hfOcrlWm6B1qCG6KbzHzfrT55iqTsjaBWEHKrpBDaqY0tPPXVUf"
    "JFBkAqoRJza2mQoePNpvOCFx4mjmMmVIy8QglZq40w4n4SS3uCGoXK5L5xC9WhREGSeZnE"
    "f5S3bZuhH6xvyA20MuMb0uB9zIIZ5wVbMA6JhHnFFXow6rp3tkdrgeEAxqlsFXmIm834mP"
    "mT3eyxeS7QoKxZHndUPugsf2EG5pOwo0xrio0kmd+jucfcErWKpjL0ctUSWuSVYAx7cX6N"
    "UqjjSlrkZbciTgkTBOQ2YSgHNK2Johh67vY2lADWwcY66X5Xj5yXGNY6Y6kNS8X6qNBRxR"
    "z6uaXzqDATKqfAvwcFxxTlNK/tyuthOlKm9k4rmnPOr43fq43jbnm79jk5fbckop+TgGud"
    "Ozbj36iJC1pPtwADG3PndX9ehQmnzB2Q7lBY1Flk/L/el/jGoQmOVHXhkvckOH5s4F/tXD"
    "Yfyk+Jpmn3dbNJZrpi0m3Zmylg4FoiyoHwixBbclC2LihoHkxcEfWYbdCHXPKXai1TRXzj"
    "5pg6tVOQuxgrcFNWlvMOFtzV36QDCBYzk9PphQMxkcFUzof8wcTLh4MAGinUDDOMrPbwjJ"
    "v8UbtIkW5Td0Nc1SydkvNDXW12+JKadi4ouy3jkUwrJtw5ru/J8lU6tUYBx8nqp7TlRB/f"
    "ONhRXUfwzc5SK99+/JtcTlSt/6L0Z1aBIIVRcv0nwBCYuRy7wSOa4H0VGGA7Qe31S4JXnB"
    "aOQwFdNpbNJmt3aIT0qDfsLw5BzzmWM+TzfmU7KqjbsLNqWkz23eubAwwEeWEWkTr4Rn4c"
    "B79Kj5Y0XKQYcT4kEKtRPq07xqgaHtGuVRmm1W+x3ORnYs74pKV3u9j/CF3yq5mQXF1SrH"
    "f0yhxqrk5KcANfbvih8LICWXshPrndJNNdKBSk2OTb5qy6lsgRLUWwCBk5pmyNDXVfA3ol"
    "9Rwma/FVpi8o1QVFd4gb94+WfwPN2h/P+X1jj09x476XkZ+bq2ddujNbeW+lmHVXEK8XM2"
    "2xHwSldQeq0zD2rV0Sd6uErCV+bQ7pOO9M2h3SszuFqh3dcbFPdUiLG3bg+Fc3E1ZFB3NI"
    "SHtaLmG4tZYaTDpUMDqlRTqyr0+aeZEV6KgdkLfqwgTvzbDQoodx51HMvADVXaak/8yN/n"
    "QPJ5A8ki9Q8GyAWy0r1+noqFTUXH91n5s80QAGXc/3qaj1B6Q0j2LfzN2+e/LH6dhqicJY"
    "CpMhjeVaoo/FAkCX3/5tWCVu4PPfzPj4Dv9v5fcSAAqg4E5WsR2SQIjWMJA4+v53rupImr"
    "6UOCODCsf+rSN0XJD6PUW0nI1i5zCkvt+kiZSZvhIN7GGD6wo9gDwFJDSrZybcuweeWq3G"
    "UvFLbyPOyTlTKKUVk2rsUWtljrtePVrpDvVSrjoLftp+G30SGKhpDsBeRgFPGxCNpqzWCv"
    "fAeXQh0xSmZFF9U9guaTOQruN+Kd7GASkUBaCVoDnkfC0SA2apsmW3BHm+GU2UOV6mhi56"
    "hzRCQrezHUWi6XBPuZ1YJa+hIyAFzbVfmUiZNVhrdrssOkY+5LLTG5lniTfP8evsz3H1PV"
    "bvoZjnCGocPOuEsTLyVXue+r7zJKt5eexxHOg/tJTWubkqrB2K7hO2XvoKuFsbs3qTlQdW"
    "UGnwNVT9rgigWq/sDBS0iFEceqyndvD4aryKgqn2Za6aGt2/QGaQZws8QGy4IZXXo47DFz"
    "6eHlW3nOidAPXcrPEi0iX+DuDmc02jYyviEQvVyJhSb0Ql0P2yVsXCfnKpSb29DZ2MioUP"
    "iCHtGHb7scb0YoXrF2WEXDwpFAFyelHMClJKU5HPQTks85IdkB6AP3BMUzz9N9vt2Pi0px"
    "IvL17kZwS7OW4PH1N2hSQ9dkG6aMvoJetP17eEPoYoRyPY0nIyeqtuvee7S07ZroZn93Tz"
    "nQ41EtPjuC0on7bG0JjIgYvFvPjPCh1Dk1JvcuT7c9BXLEnXmd7DdU92+KGtvuqcnLy25f"
    "q1ksOKdPqI5jfo+hO3bl8sAvh7ydD2+f//SToNUHeMGAqqw2gg2610lsi0nvslyfjDVEA/"
    "FOn7Zo8kJ9YHjn1An7hQqAtHC8lsXCsjuGW8uovOWppWvaU32CmjtysrcGk/ZQxrStMu3D"
    "Tq55DtO2CnomZ1yGppRUcVLSQ8OuBzr2lrb6JVVzLOVJQ+tzLOXKDK5WLOW/Xzzf7XB+Iw"
    "yllG/eHoqkfPZXqBo1IJDCk1o7DmQbAvpcETGGLGkHAVK3NI1mxMTQNF0QVDnFIx8OsPwG"
    "LFU4gb+brrzdtyQALup8v7shQ3+7CcgyuktZk2jgiEA5K+t5ElEZXo8X5jY+wA6Z7rMAr7"
    "aIuPwdbR5IJ2+KneY2Ml2xBW2bH3kLspjWsZ+h7NuzKF7TpB7bNbyyObAVQRE4Kz+zsac1"
    "E9tc8lEwxlUnwSqBZbCO/45DiqSOtlSfvHT4RGyzH+rve8ByrhfAv4ZGWVvgDso2MGbL46"
    "x4lhARTMXRoaGGkHwYt2as6GaDqqVoYZjiAT2rE6XgVMsTV0xV8+nJnzbg6YthxAOFc7yQ"
    "SlpevIWL6bEUW+cBy+N8PS6YXArIVzB/Z2QpElOL6HTLGrRPWAf2CavDp8XfNIempnAysv"
    "GXxpVcpyX1ZP5OUe/paSZzdCeAD6FGoGfWFuNbOv01IX/tb2Ec5LeLdbzLfz+bhv9PtE8C"
    "0O3C38frPE52P8AH/vuDmmcT2/EdzJdDHI2Clexb/ShYG/C6bbrU8IBO8Ge/2SDRhO+HHD"
    "kRlea7bQK1BNlNFCXMKpyjsSdkS+yCGt9v1ykKhXq3HdsqPSXVSCgLhGDMTa+SkH9AFlkn"
    "Lq0udI1gTK/ec2u2CdOMyKtqCp4ttaoDDAzpQG0tzSVt3Aubh4t8moyypIzNiJHAgyGgS7"
    "UT6h2QTGbyFQ39BHkJlY2Y8F3JC24rW5yEoDyhYXQ/LMt+HN0HX0cbSoB47tnPw5Jj9u+m"
    "mCKKhrkMW8vSUk/RJTXqPdqNwrTacrLhx15khCNXtcwA4C6sDUx8O/clvY0LjrVBn7z0i+"
    "JBaFA1KwT3++TzinJjjQhmtKSkplvQ77IoI0LeMhh6hTlxUgUF8XZkPgpuLPFdryobYrKz"
    "sPitw9NhugaDb4RMnZ6uG4aja4btWqbjWK5W6bX71iEFv3jzn6DjxkTuKn2Ndvmq+DsnVC"
    "YLxBUjdrEdKPFxI1oCwZ2kY2PvjyTWXirmYHYFtRrOslRAQNWPJTSlpEefeMN61pL2G8aK"
    "pu3O+UtPOp1lzl+6MoMrmb/0ImY+5KE0pnLM7ZBsppXPjR6S1dRq/11CMsNTk3g4hzUWd4"
    "LI7895OvsHClmM2z3YmbIEBMYPDX0iSVG1EpVJimrrfaA622Ly+y+0ZviFFcwRo5UTd7gy"
    "eRH5iuS2BGlJfPOF8EruB/OF8MoMruSF8CUAewevg2zEsMtgUI09aYI7j8lYOiRjWI7lnC"
    "DN/aEHixtUcGcWw2jpkhDc7A6OfCIXO+6gpDNFmevdY7+N8H0rVLiNcPN3uEZbUvI7wjeL"
    "XCBw57kOOHbYlRTSuCfHG/m80Sn/bTnZIbk6/ZPuvTpl2T46T/9crZsziOFn48JxvJDs4m"
    "ffoimJOi1ncZwyLm1pJvCCeNE4joQT7hJESTgZs+nyIgpptaSwsSL5Ws3TzzgZHT9uSUnW"
    "rRkZcB3zPWdBv1gRTJajULzxcUh3z4yFgwZnWrUFzxTiGnhL+HOYoSQHRmtzuWRx+YntrM"
    "+Q9DDDF0/Zm53hiyszuJLwRUXrf3MIwqhH3Q6CMbLG+JNCGaw1cpGoSenbhsSwTvx4IaxR"
    "FCbwPjC57JIRqwOxq7YQre8Xij0RuINXrevDhgAVy8qAHgIjDtSuQFI6BMJIaQv/hlN8UR"
    "4uHxRpz/QRl/OupHR1s3IJJRXNf9cR1/WWmGyIpLl50DuFDbkIrK+ybQQRJKtFmsgU/C7v"
    "ulCuZS1Ni/ZhptX5mGY2eD6fjz4JcNG0IYCLpvUDLvBep9IIbhJd0z1UZlRKqUbf7FjGsj"
    "xVHRyC3ZZgAUuHTtisvsgOTZ8nAhxmjcvUGs3O2ZO+q8/O2ZUZXDXn7GBYeVhEeVQsuesV"
    "jYwZj3+A0IkiT93T7o5DwsPtTDJemDpS7QFPM4asVth4zgo89dW9uSQG6rMlJV2dfDS+ru"
    "mfo/FHnt0cO8IcjZ+j8WOm8hyNn6PxUrQ6R+PnaPwcjVfG/58Bn1JdM+BzPQZXDfB5Vfhr"
    "Nz2YT/X+7QOwT8gPnIT8NJ2KgdT4Ux8zqPiT51F/GAbi2NXEKFBJUCp8tyTUF73XYOJ/Mv"
    "iRRId8hpEudklUqYXBMadXgxB/bl5wSF46fUzTVnPTgrlpwdy0YG5acDktz00LLsDJPDct"
    "OHXogXec5qYFF2xaMDcquNgcn5sTnFbLG5Ts0Vqs67k5wUUOwrk5wfmbExiBPjchmJsQzE"
    "0IrrsJwRElJHMTAmVsMTchmJsQzE0I5iYEcxOCuQnBQMPOTQjmNLE5TWxOE7v2NLEHeVu6"
    "A28HJo5NYG/ptAg4nA02jcPlLB9yiMmlVUJVME40igwfzEUTPEqYWdb38CeSYKY468tTST"
    "bji+wEiWeUKMNlU5Jm2bvBgk08+C0KWPmYT/MRgqpYz7dN0eojjxrKj3GeTLZpdZFiYemW"
    "O8zUo0DBpGCDGq50sbB0pffx9Sig7pmvhz/LZ76ema+HvjDz9cx++eyXz365Yn75W7x5ne"
    "Rx/u1G6I3Xb98e8sE3eLPC9bgBjnfpSlEP2PcwvaGDH4z0pTjLZ5DQp4T8R1Pf4Hj2cbnl"
    "mxiAWF7G1V1gRtAgasoOdXZ4k0Mat0YyN13XmfO9pB6gXj2TRf+IQ9J0020H7mNsczvgsj"
    "9cEkZWAUrIr59xT9lXgJI0iQO0Zln5wjHMOGwneEreuYkNH3YRHc8e+YldjF4vnOjb9kyg"
    "YfB9usqgGMYyA5mOdWOVjPA62nLncjs6Wu/JLeXyHcstyPXL7F62ETETMJyDbUEe2V4gKG"
    "5CrpNt001M82sKV5Z+FjnNZ7JNzzJoFQf3uqdhcCcjB7ZHywLjImccanLmdAZ+MxPa+nWy"
    "31B7vynM27F76xEXM3vvYuPOmyNLPoaULy37q5eWneKlsQVi564Nm6RUqFOizDFatYK4dK"
    "qpc/ssxWKt83yE5ruS0m0gSJTqWKJx8TLgUPGWQVhdqjqXLaWshdYx2uFRlSWcyOMoLmF3"
    "X2a4uriEmQ/+zCoxy8Fg7eH2uXTlCfFgMMrogT82G04kerlMrqXQLEtwLhyDutM+uEDyiJ"
    "yK4MR9LFLqsDO59YgLIsAJEk575lxApU/zZmYbIcx1r4gluQl6Foy7/Ta2pSElmv0Fmp3y"
    "zHi3ihPifcdfREmKD4C4vOTl6icG4bhWGEFinAdorh0aAy9Hl8Fot2uUQ3UP8d3ozTIedy"
    "D0iD+Ow8ENMU12dugJTyMiGCIiJo5CakEa19XMynl0wSthngXj+bN931L51JgR+CcNyM4I"
    "/JUZXD0EfhvvyK83vRB88f7tgxg8N/BBEL54Krm8u5qF6P3ebWLsHK4uzm2b9ojzwOBpFt"
    "/FySq4R3n/IFhzBXdw//vAgSt8l78gzSj6CfyHq0TRZ4R8RsjPjJC398IR9haIyqVWrs8Y"
    "3vauZiJav+ZRCh+or9EsQxlU8PFyJHHqPpIf6QxqTVBGtECO39UEDhmhsDrqtozQoyi4rz"
    "7HDHeNGukfNSUvUoc5ep/h+zhccwlmdRmeYmNxIwlFLMz3lLhmC2/JOoyDeIuSvMjfIvfm"
    "cdBl7xMeB3rJ0/LU8WJyS1+oz6NGdI/uMrS9n2C0puBjsZVGGUf16ubOkGPXNMJHYrF7tM"
    "OrDdpuCyKdwRZrC6pkMfhYscU827BLK1m6oXUtVtixihKoaTcftP8Vx3f3ggvPf6xT1HPl"
    "bMm1jBaB4LkMtfxBwP7B3TYdGjt2oerOdqDFAeRcHK39Vz//+uKn14tf3r9++ebDm8IS1Z"
    "FH32zG0t6/fv7THMCUHsB8Kn2YWuHFqhkTX6ClBkQzBxWfdIxpDipemcGVCyq+xWGM3mOW"
    "VXXTF1psjrp9KMC4geFkq+fGjy32QZ5eVdaEZt3oZHDhz4MPYEVATrQ0aIYJxAK8JSpH2L"
    "SEh5dslE47elDmmfJVvkUsgHqILOMuisgzk+UPi089DIzNZFb2AE9zIloOHFZJrp7G/oVT"
    "y9TsT4lOH8nVE3tmhOn3CCG3RkfVt4lsndWrNjQEdJzYjwoqsZtPiQEPBBd3ASasnL9FWb"
    "DkahZdrVPismxCsLoiUci0YmQpsibnsOnNHDZ9RGFTtchjp89Vfr/jtyvY+xZvX1nPPvzp"
    "uW7Zi+YGVkRSKbWpFVCGFkuvyinViXdyu5DQRg8nHzefIDsq1DzlVCsIOsjw0R9/OwXDx+"
    "nIGZ+/WfCMD6JbQH3+KxqFu8KOJo+w6OTpYCuWAS3sPE3zyuXCIyw8c61qaAvLakHrFTAx"
    "jy2rEwpL55Z9PI3vxHzZZyTLPimFr5Auu7oa0QIGxiUhr/o9jDc42ZGvPOo4aEqd4FA46e"
    "y2wed1dNN5RotI7TLNywqg7MryI7eygefqi398jcP8/seFq2m3i3sacSH7iKb9S+WTIdxD"
    "QpLoFnUgpsQLTQwondJQnhMZTQuVEKBTrRLHC0etjzMHmCR1ijyd1k/SG/L0h2wb6xm+5Q"
    "skJdfWClj9JFfYRnG2y1c7jJMJPRA6wqrFEDzPs0sdN+qarzWe0A0g0d4GE+3fllXN/KxR"
    "Arm64tn8dThJvLmu01GpQx3Bx+H+i3m3D7n/EF6xLY1mGtl8743FO1Y8sFT5LjjnAzzp9T"
    "znA1yZwZXLB/iljPve9OUC1CNuH8oD2DaGjkwBqHNAR4T9xUIs1G/rDgXqnbDEKOsOqxVS"
    "SctrWDCfB/kLQm+ap8d/Hk8YXYQGXMOl79ZpBRpyqwLBJSRRnpnpM71LYlrB1B9wJ9ZJs2"
    "+rIkHj4TJmIVvoFwhd0q1jjtffzPH6mSx0LoV+rKXQzf1whLU7gvK5FPnjkDO+ehxkrYNK"
    "qPWHczK6T1HABMgs23fV8CszASNfiYNnO0xuYnl8hDlOn7PxmdxK1zi8O84i3adItwjfSk"
    "21RJkiCayr7f4kGU5EfhISv+Gwy1NBpsslkBURHl9nJwwtYfdRSfDK37pPgr2cJZGml0yg"
    "/3Q4lkLglCE4x3c1uhBCnjlAyK4bYXpoQ9aTFcBhXrFYTutAtNQHrBkyqr8DEbzXypepvY"
    "+OQQ7jIk1JlUrPS2KHIo+Atkwrks8qgKTOKYi8RXmYQJhPdyCvnHLwspzxyrwnWE8KgSiD"
    "StYLirNx/lJT6Ew5JRN6HS7KgkxZ6c2lYmh98dRbQech0ndEy9HdRV3samI9LD2cuiRZjT"
    "vCUyktVrqkmIY/MxwnUUqcuXBqALXzhIscMdOiqMzbLwLqEc2ZYkVNV0x3co/W0WodR3i1"
    "w+TPFJFm9B4hQtnLJak4uiZedAbwoLmuQxfdcnLK1ckxtrmIfy7in3hD+nOYoYQ2dq6yyw"
    "t6FyWzylefYxFH1rC7U+sR8n1+HlhUDU8plDVydjelpN9NG7A9l+TC9m07dOFfzY7evHpG"
    "/UWwgI6WEFBxfNNTrP3NU2IabQNeyCmd9CI4IuQc5QMuypiF7DX3abba7O6EjnK/UTqCsk"
    "3iWUua7++hOpIPwBXL52oUwVRLZ7g7fX40q6HQPs7MgfboJc6U4GH7EQC7fhiyKo0qQYPn"
    "znwEBhHSWw41h5jjUh1jNGguVTYG+Mpkbm+2fQukvy1X7wOkV44NWiIc+mvrxlAC6xM7ax"
    "0lClfFcBuI14U6FhDTv8q0AHF6t1ma4wD00tH8Q/5yQ/RsDnNX770es2WEUDQZgeJtHVX5"
    "2XZgsIQTCGIsMaJXW9iWMDB7Av2NSv400WyUpX/Hgvq8hyxSyylhjqXvl7Pe0X27JEEqcx"
    "+hetXSIHeLAUkW8bhVMsQGJXu0LtDuVYjXORqFlffIXxIz18SYua2bfrkI3ACapNqWY1YZ"
    "pxWKTsa4kHXlncIyJ0PUC72iQFzE2n+NEktLv0f14ee8lWwTrMEyHY/DR4wh9ymj/zpldG"
    "5TXbVOjXSIHvJIgh19xrrmwEdJ2xeNIghoSj2SsrGK0a6CC4R8gY+DL3wuCnvSNUJzUdiV"
    "GVy5orD3ZUbDBif5T+ndTV9tWGfg7UMlYhkvUdYej2aLbWQuYMosBa7lcLbYBx/ASshcrs"
    "TY9oHOqps64bomTXI1KgZWPreS54PVNZqiT0M8Jri5Jvb8Vn0ZpqzkGBzigmzChPo1mqtU"
    "cMDqOl0yKOx/jGXA60WE2rbhu7qRVnC+8pSxDUWQ4Yt3z2GEh6t8REaS60ahW4x4+VIw4q"
    "baEMZVt+UoI6OrvPHi17in0yax5t0dzg6WsfEn3FymdjOXqT0iWtnWapiSOdB6hPzMAcfG"
    "uKJtVCxzoN5uhs/ehoz0qVtrV96kbW7KU+dt5ynypy5/yNX5GYpM3UJfI7NeWmLSsbXGPa"
    "Kb9lJUcWGdr85/DAkwBajsY3LXFayJA6h0R1IBNj/+lmjpUCOsWCZ3oTQU5Tibou1KUDVl"
    "UxxTMWX7KXF2V2gjZvM7VJzQErxspMV4UNlqxVJmlO9Jgz4M5VML9Vmjwkh9aM+64ph9CO"
    "XhRo4FdyhWz4h2hgM6QqECxKEE8W7kN5q5dtu7wiumX2Z9sDBBmRNCEz2jEDZjA1mM0Qee"
    "TXYLpwZ8oM6ZMf4w+jnWiseKGDP0Eu32/l9xkC++//fFNsNhHJAFB7+k9OWpGMrDDEHF59"
    "ateMXD0kGjqm/O3u4+uzFG+NgZlplhmZk9aGYPeqzsQcI9b+AyE++Xstdag8eDb3guZzU1"
    "zo+hS6khJJ2Bhr+RuAE0HSSrZ1ILjjNUqE2fviJR6bPX8pGu0uwtaTDHqbYtJp2QgjsLGg"
    "3a6w5w9RQ3cYDLeGTR2Z2n1jRCRvtjyzyj1Sd9GwGcPinWtx4Y42JMb2P0/kip3mZGkZuZ"
    "UWRmFJkZRQ6b3LXNmVLk5seZUuRQ5c0cmriC0MScgHw1BlcrAfmX9f4uTsisQjfCSBT3/u"
    "2hQNSWjluF5cABcSjWO8g2bPBlsWdQz1UvCTr5dw9GpqY+5uHoz5OJnKgSLCkmyUjUvikl"
    "HWvkZ5dlwR2iuJUfRUGz1IcgAWRUvxeqd9AAaoiR2uZllNI1v5IhBKiWlr+g9V5wXPezjD"
    "el5GMtfaq2NCM43qk8B2V4kRw8hfdKICp9rvO52300V6fbb86SiFmodb/D2UjOq66kUvYo"
    "2s/qhjONuOcs2p4dwyftJ8yO4ZUZXDHHkPwZOL8RO4XsvduDDiEds5vuDHouzWfx/fEOYL"
    "/o7PRd3OnL8CbNxelx/feBhpBs/ks3CqHQGEGWVT21JlL4DckvsfrzS6xufkmy2hE9jiYt"
    "a8ipQJHVAOQdneU8LCkLrN9c01CgrRJEn35N4N46MStRLC07N7FqkHIwNzEo8wVZ3jdrZW"
    "Qt4WcroE/gTMi6UoEJp6yc02dm0f+PMFQ5Xrprwq8FywQmP2j1pYxjksf5epRiKwHpmmXT"
    "m7jck+jtzwIvoS/kZBUU2vVDS7WEfFipMVU9A+o0tCD6DhJdirWqHrD02HsGNnRe9QlUU9"
    "f8lx+h75aYWjq3jSBiyZhq6jxHd4IkmUPg3J0oMUa2lhky5zv4O9pP0anKHij1D1StDNxf"
    "LtFQ8Y+8io021d7P/cfLnID576Q5abrtfSraV9bu99Gz/SwEfmhPVCc4Pw8wzFcS8mc8q8"
    "hyNW1ar7xzXEhm3PlJw5Az7nxlBlcLd36Pg30Giv9I5kP2X6l/I4Sgu8NuD6HRWTmcTsVs"
    "9ddCYAgybWgUwTCtmijEdsu8XNYVh82dmiiH5esGWZqIs3VP+NgTI9q//vrm1QhIe7+Pwx"
    "9AZsrKfRjZ5oiH6SfBP6aYdbjJUgR6gq91ohbNjAecrUh+rdG//DD8TWbaSOy7ljjN9WN6"
    "vT1r3mF5S1ukX9bvvPgtaMF+XKLC5LKcIffwA9fw7i18SqrInCPyqMHBU3o9/EZcA4VVga"
    "aBI1qdfwQ92lnu6/gLsP/CXzMGYWlKyXeEGvyA9Fxkda5m5LHGKt7iOfk+0LOUMSGY2PWr"
    "hLYKkSmtRdYHKpkTGNvdSQ6K8+CR5MBf4T+2o5zYhpBs870kX2bRIgcqiHoowwgGwmOo9l"
    "xYi7KkGTh8NnGyuE/32SJE3xabNMnvF2H6VbH1BTfKv6fJyE2tlpFtnHI9Ae0Ls8ib5++e"
    "Lz7+pTzYWeTP8lx98XwXo2cf7lFyd49ilVhCgFCHTJIV3P3HGKItdzlj3CSF/R/Y6qD4uU"
    "hiqbitQPQZWQ+rKIuffcX4M07CZ0GyKv4a+JHcUHPy46RVcvJ66F2O8v0ozLmWuFwgvC5P"
    "7F68Gi27logNfbZF+x2exDNyeh0ngBdn+yn9hlqiFymAHsGWQ89oWvPcvQQw/riSwVYHN8"
    "QxtM6WdoKTXSEMZ1BdNCtunzQfWqLKzQc0z4cJDajiXRRneLVN13EwygXuSl7wlKSfnJYs"
    "5oLW1JT3Bogh+cng+BY0+LDs4iJZPeXZ7nO8VWPDLtV6lwEP4Hjygl75yxEYGGL6gj6rWL"
    "4fQmTW0koqAxY8LBxolvPHjSmSF6hjTXwHq3yOPAIEeDwO9pQnJ0Lxeg8LdLjJ+sQvZzGh"
    "vRjZqoNxWJIguKFulZss2EiOtuk5hLNMFL7tRy6aUtJRI8bfQlS8bPG3cIq2DI82D8LacW"
    "jSpZEJdv8lR04e0y0oySfcNnofohgRT6NbMtewl1HzMAoRhkmxNNjuhYTvVWUZwKfKGsvy"
    "Utd5OZlzCp50iHnOKbgyg6uVU/DrjgbGBWkE9J3bQ5kD+3LEpCK2unB4dBFbv+gVFbFNKr"
    "0+c0EbzIixpSO8jHy8n59aUD+iTChli3a7r2k2Kl2Cl5GvWotWRjmutlRGqShE2xxnYxMf"
    "WmLyVdsmvp9WgHn60NN2jfIozTaUvWJcqo9AVL6eLQxM05YBwXIliS+2mGhsjb/g9YjDrC"
    "l0OcClHxagZLoMC6M99RyMHEk4SwoEW1Pu4k1J1a7ifKjAsSl/NOsufcV0uT5KVvskjwVr"
    "57CpG4KKQTNWAIFZx3MBrtR1anRbv25DbzOWwlV2IZ1k9N6HKDYBCtNTozeCgvNkoJ+lQP"
    "3ZtPBfnQDu7+N1Hie7H+BjxTngj6ksbUY8nzQANiOeV2ZwtRDP/yn7C94IYc/67dtD2OfX"
    "xrABAGi7i+AIGucxolcEgKoCesrlyjmihKkzrY4jzDk9fPTYGDDEfFGC5as4C4aKOckCbe"
    "/ydLstcoz7s5J/XLCM5H8Ww/8J2UXkf2RLJ1cCovUJU/0MaeAoCf30j1W8IefWKM23BeVO"
    "9yD4vvhGC7jr0DmvBZFiwZRSaV9wthNuLg/rmxO9oMbX5IG7XDjjeWU7ulkk+T57QXzDcP"
    "ERTZrnp9/SgQ2KfBjOVmOPza7kSfLXjikBhobPrM8wOzlZTZDtGgGrSCGy+HOWfk8WxT/u"
    "8R/uv6p6YNq0mDkEReYTOR4Wr9LgM84WZXanE1nLsiWu7ZQHtGp2HBfWactJz0Gsrch1FH"
    "UjyDpzXbOykuXa4LkvTUuxErt74pmttmkmyC3s7+3Gy5ypvesoC0TNaW7bOmXY1KDFJYJs"
    "T8vAhpwAUAa47mZKbUBX8oIHBborOPdEGc3V3K5rAcgtCST+meE1Ig/9J2g5yNW4G9Gs5X"
    "uMstzHEyt2GtKKAfR8FrQVBdChO4yM6wbln1x6e1H34cNmZkYY4i+aHanp/m1wjsZGRHiZ"
    "xxERIWe5McdC5ljIHAuZYyFXHwt5mW42P6V3N4dDIuWo20GRETIzN5vVuhj/YIjk3fNPe3"
    "3pmS9fLihpLeXDR1HJ82BFYScFbKjM9QRFyhelB0XqWTBKg22xcyUjDgd5lpDx6WqW2QXy"
    "F5eNQHFxkTjDgTgqcqB7JS8kO5X23fPVx59XZNn+c/HyJfz47jn58dcPr99Xr3/4fx8+vn"
    "6rhge6S/cZbGgTSPIEorIDUrYD9eI29jSeHm9RfsWqewlfZaObflngydLHPEr4UtJwksWw"
    "Yi0KV1Msdp4mgI+8V4Edui5zE9XvVUA0mqGeNLoDRHoNqYv56+JV4eAQ/EEHCPIK1tmWh7"
    "gAB41BxOX+9Yzbsiqo2LfcxcvglyzdbPO3xC9WuUw93q3Ix2Ogk7vrmu5gl6y2qGqNsmBX"
    "glVkVvxg5BhnQRS3Nrd3fAvfE7bLytHu8+j+sJWIdMSLvy3VlKDD70kzQ//0i+oMp5Rwin"
    "ru9XvMroE3h/3ratjtMAc748dPykF0Q9Msr4KMWN3EUHBVJxV+2huapnc872Mfdj0uuTJ5"
    "iuVsGevBtOVk5y06yDDKCcZPOc/SJ3VPPMs582gbKDYW8VH5oOdhFMYbqAwePYnbctKvSm"
    "xTtB0nmj5zT5/WU631JM0F87fflesIygY5GvPYA0Z6O8ADW1de2gmrlAft5FZ/3YmAvf7Q"
    "q1hapSAsfKA4CMtbqe5aR2+zDiNFP9piZwnLVjrfUi9/0kqpRVVaKzXhmpprZRfc4w0avU"
    "haYo9jdVi+5ZSc/w14qrpcq7k6tnt/HQerLfq2TpHgtt5vpq6kSpbqTybxHOgGblsmxB6X"
    "rE1VbTtLM45Hmc5iqR0OMpyX+l7hJMi+bXMssFn/hnboGbJ3NktHWsk8xMgoWQeUs1rqPA"
    "h7AtoRGOYgRstJXRCerZyNA+gs+TdiMSaV8NfHBg/O6VZzutWcbvXo0q1KoPcFMSyzwxBY"
    "uBx9Ow4dXvmc3CSU2MRwcvJeArmNLst7KAv6OEE0Bise/0gBYvxbJ1+o+pvJr7+3AOXu6F"
    "2a5as0C8nTyNju++Xp+fuMRd/O6WFD1oq0lDB+4g/XZ0tKujrr/UCaIud79iXu2dzOO3y2"
    "NoUkNwaxDQ/aHGB3YBHPqeepGKA/EGBSBJcfi8VfIlFwdvqesg8wO31XZnCVnL7nOIuD+x"
    "uBg1e8c3vImUP1mIcct/7Kj+tJtVGm+mUCr4wUPpkjtdg6mq1BR7N14Gi2OgT92+0YJRbD"
    "H6cCl5o2JF9G0/rzZeC9gUUQ/TG4/iKIiwXfjlPrJYJoUo+Xf/1/V4b80A=="
)
