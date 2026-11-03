# qbob（QQ 官方机器人 → OneBot v11）本地构建文件
#
# qbob 是 AGPL-3.0 的独立项目（https://github.com/Loping151/qbob）。本仓库**不收录它的源码**：
# 构建时从上游 git 拉 QBOB_REF 指定的提交，镜像只在部署机本地生成，不发布。
# 不要在这里打补丁改 qbob 的代码——改了就要按 AGPL 公开补丁并向其用户提供源码。
# 设计与许可边界见 docs/qbob-integration.md §4、§5。

# 用完整的提交 SHA：qbob 没有发布标签，分支名会漂移。默认 0.1.5
ARG QBOB_REF=9c36d577bae36f205c0ad04b88afe11588fe56aa

# ---- 构建阶段：拉源码、装依赖 ----
# 锁文件里的 pilk（silk 编解码，C 扩展）在 PyPI 上只有 Windows 轮子和源码包，Linux 上必须现场编译，
# 而 slim 镜像不带编译器。编译器只留在这一阶段，不进运行镜像。
FROM python:3.12-slim AS build

ARG QBOB_REPO=https://github.com/Loping151/qbob.git
ARG QBOB_REF
# 国内构建可指向 PyPI 镜像；留空用官方源
ARG PIP_INDEX_URL=

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src

# 按提交拉取；记下来源后删掉 .git：qbob 管理台的「更新」(= git pull) 在非 git 目录里会直接拒绝，
# 与配置里的 update_check_hours=0 互为兜底
RUN git init -q . \
    && git remote add origin "$QBOB_REPO" \
    && git fetch -q --depth 1 origin "$QBOB_REF" \
    && git checkout -q FETCH_HEAD \
    && printf '%s@%s\n' "$QBOB_REPO" "$(git rev-parse HEAD)" > QBOB_SOURCE \
    && rm -rf .git

# 上游的运行时锁文件带哈希，按它装，不解析新版本；装到独立前缀，运行阶段整体拷过去
RUN if [ -z "$PIP_INDEX_URL" ]; then unset PIP_INDEX_URL; fi \
    && pip install --no-cache-dir --require-hashes --prefix=/install -r requirements-runtime-lock.txt

# ---- 运行阶段 ----
FROM python:3.12-slim

ARG QBOB_REF
# 长文本转图片要 chromium（另带 CJK 字体，镜像大约多 400MB）；不装时 qbob 照常发文字
ARG WITH_CHROMIUM=false

LABEL org.opencontainers.image.title="qbob (local build for NekroAgent)" \
      org.opencontainers.image.source="https://github.com/Loping151/qbob" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.revision="${QBOB_REF}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && if [ "$WITH_CHROMIUM" = "true" ]; then \
           apt-get install -y --no-install-recommends chromium fonts-noto-cjk; \
       fi \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /install /usr/local
COPY --from=build /src /app/qbob
WORKDIR /app/qbob

RUN groupadd --system --gid 10001 qbob \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app/qbob --shell /usr/sbin/nologin qbob

COPY qbob_entry.py /usr/local/bin/qbob_entry.py

# 入口以 root 起：先把挂进来的数据目录交给 qbob 用户并收紧权限，再降权运行（见 qbob_entry.py）
ENTRYPOINT ["python", "/usr/local/bin/qbob_entry.py"]
