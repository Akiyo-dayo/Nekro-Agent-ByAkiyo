# syntax=docker/dockerfile:1
FROM --platform=$BUILDPLATFORM node:20-slim AS frontend-builder

# 设置 npm 镜像
RUN npm config set registry https://registry.npmmirror.com

# 设置工作目录
WORKDIR /app/frontend

# 安装 pnpm
RUN npm install -g pnpm && pnpm config set registry https://registry.npmmirror.com

# 首先复制依赖文件，利用缓存
COPY frontend/package.json frontend/pnpm-lock.yaml ./
# pnpm-workspace.yaml 也是 pnpm 10 的配置文件（allowBuilds/onlyBuiltDependencies 白名单在里面），
# 漏拷它会让 @swc/core、esbuild 的安装期脚本被默认拦截，pnpm install 直接 ERR_PNPM_IGNORED_BUILDS。
COPY frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

# 然后复制源代码并构建
COPY frontend/tsconfig*.json ./
COPY frontend/vite.config.ts ./
COPY frontend/postcss.config.js ./
COPY frontend/tailwind.config.js ./
COPY frontend/tsconfig.app.json ./
COPY frontend/tsconfig.json ./
COPY frontend/tsconfig.node.json ./
COPY frontend/index.html ./
COPY frontend/src ./src
COPY frontend/public ./public
RUN pnpm build

# 使用更小的基础镜像来存储构建产物
FROM busybox:1.36 AS frontend-dist
COPY --from=frontend-builder /app/frontend/dist /frontend-dist

FROM python:3.11-slim-bullseye

# 设置环境变量
ENV DEBIAN_FRONTEND=noninteractive

# 设置时区
RUN ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && \
    echo 'Asia/Shanghai' > /etc/timezone

# Debian 11 (bullseye) 已 EOL：deb.debian.org / debian-security 上它的包已被撤走（pool 里 404），
# 只剩 archive.debian.org。不改源的话下面第一条 `apt install` 就是 exit 100，镜像永远构建不出来。
# archive 的 Release 早已过了 Valid-Until，所以同时关掉有效期检查（签名校验仍然开着）。
# 长期出路是把基础镜像升到 bookworm（要同步处理包名差异），不在这次改动里。
RUN printf '%s\n' \
        'deb http://archive.debian.org/debian bullseye main' \
        'deb http://archive.debian.org/debian bullseye-updates main' \
        'deb http://archive.debian.org/debian-security bullseye-security main' \
        > /etc/apt/sources.list && \
    echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99archive-bullseye

RUN apt update

# 分批安装依赖以避免 QEMU 段错误
RUN apt install -y ca-certificates curl
RUN apt install -y gnupg git
RUN apt install -y gcc libpq-dev libmagic-dev
# 中文字体：python:3.11-slim 基础镜像不带任何 CJK 字形，缺了它
# nekro_agent/services/imagecard 渲染出来的每一个汉字都是豆腐块。
# fonts-noto-cjk 是轮廓字体，任意字号可用；Symbola 作为 emoji/符号的兜底。
RUN apt install -y fonts-noto-cjk fonts-noto-color-emoji fonts-symbola
RUN apt install -y docker.io
# iptables 是 docker.io 的传递依赖，这里显式声明：NA 跑在容器里时，
# 沙盒网络隔离会用本镜像起一个 `--network=host` 的临时容器来下宿主规则（见 services/sandbox/network_guard.py），
# 缺了它整条防线会静默退化成告警
RUN apt install -y iptables
RUN apt install -y git
# 视频分析插件（plugins/builtin/video_analysis）抽帧与抽音轨用的 ffmpeg / ffprobe
RUN apt install -y --no-install-recommends ffmpeg

# 清理缓存
RUN apt clean && rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV STATIC_DIR=/app/static

# 安装 UV（使用官方推荐方式）
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# 首先复制依赖文件和 README（pyproject.toml 需要），利用缓存
COPY pyproject.toml uv.lock README.md ./

# 先安装第三方依赖，避免源码变更时重复下载依赖
RUN uv sync --frozen --no-dev --no-install-project

# 复制入口脚本（force-include 需要）
# nekro_log_hygiene.py 与 run_bot.py 是一体的：run_bot.py 在 nonebot.init() 之前
# `from nekro_log_hygiene import ...`，而这个 import 在 try 块之外——漏拷它不是降级，
# 是容器一启动就 ModuleNotFoundError。pyproject 的 force-include 只管 wheel，
# 管不到这里的 COPY 清单，两处必须同时登记。
COPY run_bot.py nekro_log_hygiene.py ./

# 复制应用代码
COPY nekro_agent ./nekro_agent
COPY plugins ./plugins
COPY migrations ./migrations
COPY .env.prod ./

# 安装当前项目，确保运行期不再触发本地包构建
RUN uv sync --frozen --no-dev

# 从前端构建产物复制静态文件
COPY --from=frontend-dist /frontend-dist ${STATIC_DIR}

# 暴露端口
EXPOSE 8021

# 健康检查
# 端口跟随 `PORT`（NoneBot 的驱动配置项，也就是进程真正监听的那个端口），缺省 8021。
# 写死 8021 时，只要有人在容器里改了 PORT（`docker run -e PORT=…` 或自写编排），
# 容器就会**永久 unhealthy 而进程其实是好的**，依赖 healthcheck 做滚动更新 / 负载摘除的
# 编排会因此判死一个健康实例。
# 注意这里必须是 shell 形态：`${PORT:-8021}` 由容器内的 /bin/sh 在**运行时**展开，
# Dockerfile 的构建期变量替换不作用于 HEALTHCHECK CMD。
# `NEKRO_EXPOSE_PORT` 不是监听端口（compose 里它只改宿主侧映射，容器内恒为 8021），不要用它。
HEALTHCHECK --interval=10s --timeout=3s CMD curl -f "http://127.0.0.1:${PORT:-8021}/api/health" || exit 1

# 启动应用
CMD ["/app/.venv/bin/bot", "--env=prod"]
