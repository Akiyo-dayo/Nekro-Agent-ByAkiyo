#!/bin/bash

# 记下「运行脚本时的当前目录」与「脚本文件所在目录」：后面会 cd 进数据目录，之后就找不回来了。
# 取 compose 文件时先看这两处有没有本 fork 的 compose（见 obtain_compose_file）。
NEKRO_START_DIR=$(pwd)
NEKRO_SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd) || NEKRO_SCRIPT_DIR=""

# 默认不使用 napcat
WITH_NAPCAT=""
# 默认不附带 qbob（QQ 官方机器人 → OneBot v11，见 docs/qbob-integration.md）
WITH_QBOB=""

# 解析命令行参数
while [[ "$#" -gt 0 ]]; do
    case "$1" in
    --with-napcat)
        WITH_NAPCAT=true
        shift
        ;;
    --with-qbob)
        WITH_QBOB=true
        shift
        ;;
    *)
        # 未知选项
        echo "未知选项: $1"
        exit 1
        ;;
    esac
done

# Define base URLs
BASE_URLS=(
    "https://raw.githubusercontent.com/Akiyo-dayo/Nekro-Agent-ByAkiyo/main/docker"
)

# Docker 镜像
DOCKER_IMAGE_MIRRORS=(
    "https://docker.m.daocloud.io"
    "https://docker.1ms.run"
    "https://ccr.ccs.tencentyun.com"
)

# 下载文件
get_remote_file() {
    local filename=$1
    local output=$2
    for base_url in "${BASE_URLS[@]}"; do
        url=${base_url}/${filename}
        if ! curl -fsSL -m 30 -o "$output" "$url"; then
            echo "下载失败，尝试其他源..."
            continue
        fi
        return
    done
    return 1
}

# 一份 compose 文件算不算「本 fork 的」：必须同时含三个环境变量透传。
# compose 的 `environment:` 是显式白名单、服务也没有 env_file：上游那份没有这三行，
# 脚本写进 .env 的随机 JWT 密钥与 NEKRO_SELF_HOSTNAME 就进不了容器（Docker 部署照旧每次重启现场随机）。
# 键名与 docker/docker-compose*.yml 一致，有用例对拍（tests/test_install_compose_source.py）。
compose_has_fork_passthrough() {
    local file=$1 key
    for key in NEKRO_JWT_SECRET_KEY NEKRO_JWT_REFRESH_SECRET_KEY NEKRO_SELF_HOSTNAME; do
        grep -q "$key" "$file" 2>/dev/null || return 1
    done
    return 0
}

# 在「脚本所在目录」「运行时的当前目录」（按此顺序）里找本 fork 的 compose 文件，找到就打印路径。
find_local_compose_file() {
    local filename=$1 dir
    for dir in "$NEKRO_SCRIPT_DIR" "$NEKRO_START_DIR"; do
        [ -n "$dir" ] || continue
        if [ -f "$dir/$filename" ] && compose_has_fork_passthrough "$dir/$filename"; then
            echo "$dir/$filename"
            return 0
        fi
    done
    return 1
}

# 取 compose 文件：本地有 fork 的就用本地的、不下载覆盖；否则保持原行为从上游下载，
# 并在下载到的文件不含透传时明确告警。$1=源文件名 $2=目标路径（当前目录下）
obtain_compose_file() {
    local filename=$1 output=$2 local_file
    if local_file=$(find_local_compose_file "$filename"); then
        # 源与目标是同一个文件（在数据目录里重跑）时 cp 会报错，内容相同就什么都不用做
        if [ -f "$output" ] && cmp -s "$local_file" "$output"; then
            echo "使用本地的 compose 文件（不从上游下载）: $local_file"
            return 0
        fi
        cp -f "$local_file" "$output" || return 1
        echo "使用本地的 compose 文件（不从上游下载）: $local_file"
        return 0
    fi
    get_remote_file "$filename" "$output" || return 1
    if ! compose_has_fork_passthrough "$output"; then
        echo "Warn: 已从上游仓库下载 $filename。上游的 compose 文件不含 NEKRO_JWT_SECRET_KEY / NEKRO_JWT_REFRESH_SECRET_KEY / NEKRO_SELF_HOSTNAME 等环境变量透传：写在 .env 里的这些值进不了容器（JWT 密钥每次重启现场随机，登录态随重启失效）。如需生效，请把本 fork 的 docker/$filename 放在脚本所在目录或运行脚本的当前目录后重新运行。"
    fi
    return 0
}

# qbob 附加文件（Dockerfile、入口脚本、compose 附加文件）只随本 fork 的仓库提供，上游没有，不下载。
# 在「脚本所在目录」「运行时的当前目录」（按此顺序）下找 qbob/ 子目录，复制到当前目录（数据目录）的 qbob/ 下。
QBOB_FILES=(Dockerfile qbob_entry.py docker-compose.qbob.yml)
QBOB_COMPOSE_FILE=qbob/docker-compose.qbob.yml
obtain_qbob_files() {
    local dir src name
    for dir in "$NEKRO_SCRIPT_DIR" "$NEKRO_START_DIR"; do
        [ -n "$dir" ] || continue
        src="$dir/qbob"
        [ -f "$src/docker-compose.qbob.yml" ] || continue
        mkdir -p qbob || return 1
        for name in "${QBOB_FILES[@]}"; do
            [ -f "$src/$name" ] || return 1
            # 在数据目录里重跑时源与目标是同一个文件
            if [ -f "qbob/$name" ] && cmp -s "$src/$name" "qbob/$name"; then
                continue
            fi
            cp -f "$src/$name" "qbob/$name" || return 1
        done
        echo "使用本地的 qbob 附加文件: $src"
        return 0
    done
    return 1
}

# .env 里的 COMPOSE_FILE 是否已经带上 qbob 附加文件（之前装过）
env_has_qbob() {
    grep -q "^COMPOSE_FILE=.*${QBOB_COMPOSE_FILE}" .env 2>/dev/null
}

# 保留 .env 里已有的其他附加文件，只在缺 qbob 那一项时追加。整行覆盖会把运维自己加的
# 附加文件（如 extra.yml）丢掉。没有 COMPOSE_FILE 行时由调用方写入基础形态。
append_qbob_compose_file() {
    local current
    current=$(grep -m1 '^COMPOSE_FILE=' .env 2>/dev/null | cut -d '=' -f2-)
    # 空值也按「没有」处理：否则会留下一个没有基础文件的 COMPOSE_FILE
    [ -n "$current" ] || current="docker-compose.yml"
    case ":$current:" in
        *":${QBOB_COMPOSE_FILE}:"*) return 0 ;;
    esac
    sed -i "s|^COMPOSE_FILE=.*|COMPOSE_FILE=${current}:${QBOB_COMPOSE_FILE}|" .env
}

# 镜像拉取失败后调用它给出提示。
ghcr_login_hint() {
    : # 公开版的镜像是公开包，不需要登录；拉取失败时上面的 Error 已提示检查网络
}
# 生成随机字符串的函数
generate_random_string() {
    local length=$1
    tr -dc 'a-zA-Z0-9' < /dev/urandom | fold -w "$length" | head -n 1
}

# 给 .env 里的 $1 填一个 $2 位的随机值：只在「没有这一行」或「值为空」时写，
# **已有值一律不覆盖**（哪怕是仓库的旧示例值或过短的值）——覆盖会让已签发令牌与按 JWT 密钥派生的数据
# 当场作废，该不该换由运维决定，启动审计（core/logger.py）会对示例值 / 过短 / 两把相同点名告警。
# 老部署的 .env 是旧模板铺的，可能根本没有这一行：sed 替换会静默落空，所以没有时追加。
# 随机源给不出值时返回 1、不往 .env 里写任何东西。不打印生成的值。
# 与 install_i18n.sh / wrtinstall.sh 同名同语义，tests/test_jwt_and_admin_secret_hygiene.py 逐份真跑。
ensure_random_env_secret() {
    local key=$1 length=$2 current value
    current=$(grep -m1 "^${key}=" .env | cut -d '=' -f2-)
    if [ -n "$current" ]; then
        return 0
    fi
    value=$(generate_random_string "$length")
    # 空值、或比要求的短（随机源降级时可能给不满）都不能写：宁可失败也不落一把短密钥
    if [ "${#value}" -lt "$length" ]; then
        return 1
    fi
    if grep -q "^${key}=" .env; then
        sed -i "s|^${key}=.*|${key}=${value}|" .env
    else
        # 文件末尾没有换行时先补一个，免得新行拼到上一行的值后面
        if [ -n "$(tail -c1 .env)" ]; then
            echo >>.env
        fi
        echo "${key}=${value}" >>.env
    fi
}

# 选择 Docker 安装镜像
select_docker_install_mirror() {
    echo "请选择使用的 Docker 安装源："
    echo "    1) Docker 官方"
    echo "    2) 阿里"
    echo "    3) Azure 中国云"

    read -r -p "请输入选项数字 (默认为 1): " num
    echo ""
    [ -z "$num" ] && num=1
    case "$num" in
        1)
            ;;
        2)
            DOCKER_PKG_MIRROR="Aliyun"
            ;;
        3)
            DOCKER_PKG_MIRROR="AzureChinaCloud"
            ;;
        *)
            >&2 echo "未知选项，退出..."
            exit 1
            ;;
    esac
}

# 通过脚本安装 docker
install_docker_via_official_script() {
    mirror="${1:-Aliyun}"
    max_retries=3
    attempt_num=0

    if command -v apt-get &>/dev/null; then
        echo "Warn: 为避免冲突，将卸载可能已从系统安装的 docker.io docker-doc docker-compose podman-docker containerd runc"
        for pkg in docker.io docker-doc docker-compose podman-docker containerd runc; do sudo apt-get remove $pkg; done
    fi
    echo "尝试获取 Docker 安装脚本..."
    while [ "$attempt_num" -le "$max_retries" ]; do
        if content=$(curl -fsSL -m 30 https://get.docker.com); then
            echo "Docker 安装脚本下载完成."
            # 使用 sed 命令修改 sleep 以取消等待
            if printf '%s\n' "$content" | sed 's#sleep#test#g' | sh -s -- --mirror "$mirror"; then
                DOCKER_COMPOSE_CMD="docker compose"
                return 0
            else
                echo "Docker 安装失败..." >&2
                return 1
            fi
        else
            if [ "$attempt_num" -eq "$max_retries" ]; then
                echo "Docker 安装脚本下载失败..." >&2
                return 1
            fi
            echo "Docker 安装脚本下载失败，正在重试($((attempt_num + 1))/$max_retries)"
            sleep 1
        fi
        attempt_num=$((attempt_num + 1))
    done
    return 1
}

# Docker 备用安装方式
install_docker_fallback() {
    if ! command -v apt-get &>/dev/null; then
        echo "包管理器非 apt，暂不支持..."
        return 1
    fi
    echo "正在更新软件源..."
    if ! sudo apt-get update; then
        echo "Error: 更新软件源失败，请检查您的网络连接。"
        return 1
    fi
    echo "正在安装 Docker..."
    # 必须是 Compose v2：apt 里的 `docker-compose` 是 V1（Python 版），不支持 compose 文件里
    # `${NEKRO_SELF_HOSTNAME:-${CONTAINER_NAME_PREFIX}...}` 这种嵌套默认值，会把它解析成错的
    # 字面量。v2 在不同发行版里包名不同，按顺序试。
    if ! sudo apt-get install -y docker.io; then
        echo "Error: Docker 安装失败，请检查您的网络连接或软件源配置。" >&2
        return 1
    fi
    if ! docker compose version >/dev/null 2>&1; then
        for pkg in docker-compose-v2 docker-compose-plugin; do
            sudo apt-get install -y "$pkg" && docker compose version >/dev/null 2>&1 && break
        done
    fi
    if ! docker compose version >/dev/null 2>&1; then
        echo "Error: 未能安装 Docker Compose v2（docker compose）。V1 的 docker-compose 不被支持，" >&2
        echo "请参考 https://docs.docker.com/compose/install/linux/ 手动安装 v2 后重试。" >&2
        return 1
    fi
    DOCKER_COMPOSE_CMD="docker compose"
}

# 添加 Docker 镜像源
add_docker_mirrors_prepend() {
    if [[ $# -eq 0 ]]; then
        return 1
    fi

    if ! command -v jq &> /dev/null; then
        echo "Error: jq 未安装" >&2
        return 1
    fi

    local daemon_file="/etc/docker/daemon.json"
    local current_json_input="{}"
    local mirrors_array_string="[]"
    if (($# > 0)); then
        mirrors_array_string=$(printf '%s\n' "$@" | jq -R . | jq -s .)
    fi

    if sudo test -f "$daemon_file" && sudo test -s "$daemon_file"; then
        if sudo jq -e 'type == "object"' "$daemon_file" >/dev/null 2>&1; then
            current_json_input=$(cat "$daemon_file")
        else
            echo "Error: $daemon_file 文件内容有误。" >&2
            echo "请修复该文件或将其删除后重试。" >&2
            return 1
        fi
        sudo cp "$daemon_file" "$daemon_file.bak"
    else
        sudo mkdir -p "/etc/docker/"
    fi

    local updated_json
    updated_json=$(echo "$current_json_input" | jq \
        --argjson new_mirrors_jq "$mirrors_array_string" \
        'if .["registry-mirrors"] != null and (.["registry-mirrors"] | type) != "array" then
                error("Error: daemon.json 中的 registry-mirrors 已存在但不是一个数组！请检查文件内容。")
        else
            .["registry-mirrors"] = ($new_mirrors_jq) + (.["registry-mirrors"] // [])
        end | .["registry-mirrors"] = ((.["registry-mirrors"] // []) | unique)'
    )

    # shellcheck disable=SC2181
    if [[ $? -ne 0 ]] || [[ -z "$updated_json" ]]; then
        echo "Error: jq 处理 JSON 失败。" >&2
        return 1
    fi

    if echo "$updated_json" | sudo tee "$daemon_file" > /dev/null; then
        return 0
    fi
    echo "Error: 写入 $daemon_file 文件失败。" >&2
    return 1
}

# 只认 Compose v2。不再优先用 V1 的 `docker-compose`：compose 文件里的嵌套默认值
# （`${NEKRO_SELF_HOSTNAME:-${CONTAINER_NAME_PREFIX}...}`）在 V1 下不成立，容器内主机名会被
# 插值成错的字面量。
# v2 有两种形态：插件 `docker compose`，以及独立二进制 `docker-compose`（v2 起也发布独立版，
# 主版本 >= 2 时同样可用）。只认前者会把后者误判成 V1、默认进入安装，apt 机器还会走卸载分支。
# 成功时在 stdout 打印可用的命令名，优先插件。
# 只用 shell 内建做版本比较，不依赖 sed / awk。
compose_v2_command() {
    if docker compose version >/dev/null 2>&1; then
        echo "docker compose"
        return 0
    fi
    if command -v docker-compose &>/dev/null; then
        local ver major
        ver=$(docker-compose version --short 2>/dev/null) || ver=""
        ver=${ver#v}
        major=${ver%%.*}
        if [[ "$major" =~ ^[0-9]+$ ]] && [ "$major" -ge 2 ]; then
            echo "docker-compose"
            return 0
        fi
    fi
    return 1
}

# 把数据目录对容器开放（容器内进程的 uid 不固定，沿用原来的 777）。
# 但**排除**应用写下的手填 API Key 备份（`nekro-agent.model-groups-handwritten-keys.*`）：它含明文 Key，
# 应用创建时是仅属主可读写，递归放开 777 会把它改成全员可读。
# 文件名前缀必须与 `services/model_group_service.HANDWRITTEN_KEYS_BACKUP_PREFIX` 一致（有用例对拍）。
# 用 find 而不是递归 chmod，是为了真的「不碰」它，而不是先放开再收回（中间有一个全员可读的窗口）。
#
# 同样排除凭据加密的**主密钥文件** `credential_secret.key`（档 B，应用创建时 0600；
# 文件名必须与 `tools/credential_cipher.KEY_FILE_NAME` 一致，有用例对拍）：它全员可读 = 同机任何账户
# 都能解开库里全部模型组密文，违反「租户拿不到平台方 Key」的硬约束。
# 排除之后再对**已经存在**的那份纠正回 0600——老版本脚本可能早已把它放开了，光「这次不放开」纠不回来。
#
# 还排除数据目录里的 `.env`（ADMIN 口令、JWT 两把、OneBot 令牌都在里面；重跑脚本时它已存在），
# 同样收紧到 0600。compose 由脚本以 root（sudo）读 `--env-file .env`，服务本身没有 env_file，
# 容器内进程不读它，所以收紧不影响运行。
#
# `find "$1/"` 的结尾斜杠是有意的：`NEKRO_DATA_DIR` 本身可能是符号链接，`find 链接` 不会进入它
# （`! -type l` 把它自己也排掉了，结果是一个文件都没 chmod），带斜杠才会先解析链接再遍历。
#
# 附带 qbob 时整棵 `qbob_data/`（qbob 的库里是明文 AppSecret、首启 admin 口令）与 `qbob_state/`（NA 的 qbob
# 服务账号）也排除在外（-prune，不进入），已存在的收回组与其他人的全部权限。`qbob/` 是镜像的构建上下文
# （Dockerfile 与入口脚本会以 root 在容器里执行），同样不能放开。qbob 容器入口本身就把数据目录
# 收紧到 0700，这里只是不让重跑脚本把它重新放开。
open_data_dir_permissions() {
    # 邮箱账户引用的密钥 `email_account_ref.key`（应用创建时 0600，不随 JWT 轮换）与上面的主密钥同样排除、同样收紧
    sudo find "$1/" -type d \( -name 'qbob_data' -o -name 'qbob_state' -o -name 'qbob' \) -prune -o ! -type l ! -name 'nekro-agent.model-groups-handwritten-keys.*' ! -name 'credential_secret.key' ! -name 'email_account_ref.key' ! -name '.env' -exec chmod 777 {} +
    sudo find "$1/" -maxdepth 1 -type f \( -name 'credential_secret.key' -o -name 'email_account_ref.key' -o -name '.env' \) -exec chmod 600 {} +
    sudo find "$1/" -maxdepth 1 -type d \( -name 'qbob_data' -o -name 'qbob_state' \) -exec chmod -R go-rwx {} +
}

DOCKER_COMPOSE_CMD=$(compose_v2_command) || DOCKER_COMPOSE_CMD=""

# 安装 Docker / Compose v2
if [[ -z $DOCKER_COMPOSE_CMD ]]; then
    if command -v docker-compose &>/dev/null; then
        echo "检测到 docker-compose，但主版本低于 2（或无法识别版本）；NekroAgent 的 compose 文件需要 Docker Compose v2"
    fi
    echo "NerkoAgent 依赖于 Docker Compose v2，当前环境缺失"
    echo "优先使用 Docker 官方脚本进行安装"
    if ! command -v apt-get &>/dev/null; then
        echo "Warn: 您可能需要手动卸载已安装的 docker"
    fi

    read -r -p "是否安装？[Y/n] " yn
    echo ""
    [ -z "$yn" ] && yn=y
    if ! [[ "$yn" =~ ^[Yy]$ ]]; then
        echo "已取消..." >&2
        exit 1
    fi
    echo "正在通过 Docker 官方安装脚本进行安装"
    select_docker_install_mirror
    if ! install_docker_via_official_script "$DOCKER_PKG_MIRROR"; then
        echo "Docker 安装失败..." >&2
        echo "正在尝试备用安装方式..."
        if ! install_docker_fallback; then
            echo "安装失败，退出..." >&2
            exit 1
        fi
    fi
fi

# 兜底：不论走的是哪条安装路径，最终必须有可用的 Compose v2（插件或独立 v2 二进制，同一个判定函数）
if ! DOCKER_COMPOSE_CMD=$(compose_v2_command); then
    echo "Error: 未检测到可用的 Docker Compose v2（docker compose，或主版本 >= 2 的 docker-compose）。" >&2
    echo "V1 的 docker-compose 不被支持，请参考 https://docs.docker.com/compose/install/linux/ 安装 v2 后重试。" >&2
    exit 1
fi

# 设置应用目录 优先使用环境变量
if [ -z "$NEKRO_DATA_DIR" ]; then
    NEKRO_DATA_DIR=${NEKRO_DATA_DIR:-"${HOME}/srv/nekro_agent"}
fi

echo "NEKRO_DATA_DIR: $NEKRO_DATA_DIR"

export NEKRO_DATA_DIR=$NEKRO_DATA_DIR

# 创建应用目录
mkdir -p "$NEKRO_DATA_DIR" || {
    echo "Error: 无法创建应用目录 $NEKRO_DATA_DIR，请检查您的权限配置。"
    exit 1
}

# 设置开放目录权限
open_data_dir_permissions "$NEKRO_DATA_DIR"

# 进入应用目录
cd "$NEKRO_DATA_DIR" || {
    echo "Error: 无法进入应用目录 $NEKRO_DATA_DIR。"
    exit 1
}

# 如果当前目录没有 .env 文件，从仓库获取.env.example 并修改 .env 文件
if [ ! -f .env ]; then
    echo "未找到.env文件，正在从仓库获取.env.example..."
    if ! get_remote_file .env.example .env.example; then
        echo "Error: 无法获取.env.example文件，请检查网络连接或手动创建.env文件。"
        exit 1
    fi
    if ! cp .env.example .env; then
        echo "Error: 无法将文件 .env.example 复制为 .env"
        exit 1
    fi
fi

# 替换或添加 NEKRO_DATA_DIR
if grep -q "^NEKRO_DATA_DIR=" .env; then
    # 如果存在，就替换
    sed -i "s|^NEKRO_DATA_DIR=.*|NEKRO_DATA_DIR=${NEKRO_DATA_DIR}|" .env
else
    # 如果不存在，就添加
    echo "NEKRO_DATA_DIR=${NEKRO_DATA_DIR}" >>.env
fi

# 生成随机的 ONEBOT_ACCESS_TOKEN 和 NEKRO_ADMIN_PASSWORD（如果它们为空）
ONEBOT_ACCESS_TOKEN=$(grep -m1 '^ONEBOT_ACCESS_TOKEN' .env | cut -d '=' -f2)
if [ -z "$ONEBOT_ACCESS_TOKEN" ]; then
    ONEBOT_ACCESS_TOKEN=$(generate_random_string 32)
    sed -i "s|^ONEBOT_ACCESS_TOKEN=.*|ONEBOT_ACCESS_TOKEN=${ONEBOT_ACCESS_TOKEN}|" .env
fi

NEKRO_ADMIN_PASSWORD=$(grep -m1 '^NEKRO_ADMIN_PASSWORD=' .env | cut -d '=' -f2)
if [ -z "$NEKRO_ADMIN_PASSWORD" ]; then
    NEKRO_ADMIN_PASSWORD=$(generate_random_string 16)
    sed -i "s|^NEKRO_ADMIN_PASSWORD=.*|NEKRO_ADMIN_PASSWORD=${NEKRO_ADMIN_PASSWORD}|" .env
fi

QDRANT_API_KEY=$(grep -m1 '^QDRANT_API_KEY=' .env | cut -d '=' -f2)
if [ -z "$QDRANT_API_KEY" ]; then
    QDRANT_API_KEY=$(generate_random_string 32)
    sed -i "s|^QDRANT_API_KEY=.*|QDRANT_API_KEY=${QDRANT_API_KEY}|" .env
fi

# JWT 两把签名密钥：拿到它的人可以直接自签 admin 的令牌，不能留在示例值 / 空值上。
# 已有值不覆盖；两把各自随机，互不相同。
ensure_random_env_secret NEKRO_JWT_SECRET_KEY 64 || {
    echo "Error: 无法生成随机的 NEKRO_JWT_SECRET_KEY" >&2
    exit 1
}
ensure_random_env_secret NEKRO_JWT_REFRESH_SECRET_KEY 64 || {
    echo "Error: 无法生成随机的 NEKRO_JWT_REFRESH_SECRET_KEY" >&2
    exit 1
}

# 从.env文件加载环境变量
# 设置 INSTANCE_NAME 默认值为空字符串
INSTANCE_NAME=$(grep -m1 '^INSTANCE_NAME=' .env | cut -d '=' -f2)
export INSTANCE_NAME=$INSTANCE_NAME

# 确保 NEKRO_EXPOSE_PORT 有值
NEKRO_EXPOSE_PORT=$(grep -m1 '^NEKRO_EXPOSE_PORT=' .env | cut -d '=' -f2)
if [ -z "$NEKRO_EXPOSE_PORT" ]; then
    echo "Error: NEKRO_EXPOSE_PORT 未在 .env 文件中设置"
    exit 1
fi
export NEKRO_EXPOSE_PORT=$NEKRO_EXPOSE_PORT

if [ "$WITH_NAPCAT" ]; then
    NAPCAT_EXPOSE_PORT=$(grep -m1 '^NAPCAT_EXPOSE_PORT=' .env | cut -d '=' -f2)
    if [ -z "$NAPCAT_EXPOSE_PORT" ]; then
        echo "Error: NAPCAT_EXPOSE_PORT 未在 .env 文件中设置"
        exit 1
    fi
    export NAPCAT_EXPOSE_PORT=$NAPCAT_EXPOSE_PORT
fi

read -r -p "请检查并按需修改.env文件中的配置，未修改则按照默认配置安装，确认是否继续安装？[Y/n] " yn
echo ""
[ -z "$yn" ] && yn=y
if ! [[ "$yn" =~ ^[Yy]$ ]]; then
    echo -e "安装已取消..."
    exit 0
fi

# 添加 Docker 镜像到 daemon.json
echo -n "是否添加 Docker 镜像源（若无 jq 将安装）？[Y/n] "
read -r yn
echo ""
[ -z "$yn" ] && yn=y
if [[ "$yn" =~ ^[Yy]$ ]]; then
    if ! command -v jq &>/dev.null; then
        if command -v apt-get &>/dev/null; then
            sudo apt-get update && sudo apt-get install -y jq
        else
            echo "包管理器非 apt，暂不支持..."
        fi
    fi
    if add_docker_mirrors_prepend "${DOCKER_IMAGE_MIRRORS[@]}"; then
        sudo systemctl daemon-reload
        sudo systemctl restart docker
    else
        echo "Error: 添加失败" >&2
    fi
fi

# 拉取 docker-compose.yml 文件
if [ -z "$WITH_NAPCAT" ]; then
    read -r -p "是否同时使用 napcat 服务？[Y/n] " yn
    echo ""
    [ -z "$yn" ] && yn=y
    if [[ "$yn" =~ ^[Yy]$ ]]; then
        WITH_NAPCAT=true
    else
        echo "不使用 napcat 服务"
    fi
fi

if [ "$WITH_NAPCAT" ]; then
    echo "将同时运行 napcat 服务"
    compose_file=docker-compose-x-napcat.yml
else
    compose_file=docker-compose.yml
fi

echo "正在获取 docker-compose.yml 文件..."
if ! obtain_compose_file "$compose_file" docker-compose.yml; then
    echo "Error: 无法拉取 docker-compose.yml 文件，请检查您的网络连接。"
    exit 1
fi

# qbob：之前装过（.env 的 COMPOSE_FILE 已带上它）就沿用，不再询问
if [ -z "$WITH_QBOB" ] && env_has_qbob; then
    echo "检测到已附带 qbob，继续附带"
    WITH_QBOB=true
fi
if [ -z "$WITH_QBOB" ]; then
    read -r -p "是否同时附带 qbob（QQ 官方机器人接入，镜像在本机构建）？[y/N] " yn
    echo ""
    if [[ "$yn" =~ ^[Yy]$ ]]; then
        WITH_QBOB=true
    fi
fi

COMPOSE_FILE_ARGS=""
PULL_EXTRA_ARGS=""
if [ "$WITH_QBOB" ]; then
    if ! obtain_qbob_files; then
        echo "Error: 找不到 qbob 附加文件。它只随本仓库提供：请在仓库的 docker/ 目录里运行本脚本（该目录下应有 qbob/docker-compose.qbob.yml）。" >&2
        exit 1
    fi
    QBOB_ADMIN_EXPOSE_PORT=$(grep -m1 '^QBOB_ADMIN_EXPOSE_PORT=' .env | cut -d '=' -f2)
    COMPOSE_FILE_ARGS="-f docker-compose.yml -f ${QBOB_COMPOSE_FILE}"
    # qbob 镜像只在本机构建，pull 时跳过；up 时本机没有这个提交的镜像才构建
    PULL_EXTRA_ARGS="--ignore-buildable"
fi

# 拉取服务镜像
echo "拉取服务镜像..."
if ! sudo bash -c "$DOCKER_COMPOSE_CMD --env-file .env $COMPOSE_FILE_ARGS pull $PULL_EXTRA_ARGS"; then
    echo "Error: 无法拉取服务镜像，请检查您的网络连接。"
    ghcr_login_hint
    exit 1
fi

# 从.env文件加载环境变量
if [ -f .env ]; then
    # 使用 --env-file 参数而不是 export
    echo "使用实例名称: ${INSTANCE_NAME}"
    echo "启动主服务中..."
    if ! sudo bash -c "$DOCKER_COMPOSE_CMD --env-file .env $COMPOSE_FILE_ARGS up -d"; then
        echo "Error: 无法启动主服务，请检查 Docker Compose 配置。"
        if [ "$WITH_QBOB" ]; then
            echo "提示: 附带了 qbob 时，也可能是 qbob 镜像构建失败（要从 GitHub 与 PyPI 拉源码和依赖）。不附带 qbob 重跑本脚本即可先把 NekroAgent 启动起来。" >&2
        fi
        exit 1
    fi
    # 启动成功后才记住「附带 qbob」：让之后手敲的 docker compose 命令也带上它（Compose 会读 .env 里的
    # COMPOSE_FILE），重跑本脚本时也据此沿用。启动失败时不写，免得之后每次 up 都去构建一个起不来的镜像
    if [ "$WITH_QBOB" ]; then
        if grep -q "^COMPOSE_FILE=" .env; then
            # 保留 .env 里已有的其他附加文件；已经带 qbob 时不重复追加
            append_qbob_compose_file
        else
            echo "COMPOSE_FILE=docker-compose.yml:${QBOB_COMPOSE_FILE}" >>.env
        fi
    fi
else
    echo "Error: .env 文件不存在"
    exit 1
fi

# 拉取沙盒镜像
echo "拉取沙盒镜像..."
if ! sudo docker pull ghcr.io/akiyo-dayo/nekro-agent-byakiyo-sandbox; then
    echo "Error: 无法拉取沙盒镜像，请检查您的网络连接。"
    ghcr_login_hint
    exit 1
fi

# 放行防火墙端口
echo "NekroAgent 主服务需放行端口 ${NEKRO_EXPOSE_PORT:-8021}/tcp..."
if [ "$WITH_NAPCAT" ]; then
    echo "NapCat 服务需放行端口 ${NAPCAT_EXPOSE_PORT:-6099}/tcp..."
fi
if command -v ufw &>/dev/null; then
    echo -e "\n正在配置防火墙..."
    if ! sudo ufw allow "${NEKRO_EXPOSE_PORT:-8021}/tcp"; then
        echo "Warning: 无法放行防火墙端口 ${NEKRO_EXPOSE_PORT:-8021}，如服务访问受限，请检查防火墙设置。"
    fi

    if [ "$WITH_NAPCAT" ]; then
        if ! sudo ufw allow "${NAPCAT_EXPOSE_PORT:-6099}/tcp"; then
            echo "Warning: 无法放行防火墙端口 ${NAPCAT_EXPOSE_PORT:-6099}，如服务访问受限，请检查防火墙设置。"
        fi
    fi
fi

echo -e "\n=== 部署完成！==="
echo "你可以通过以下命令查看服务日志："
echo "  NekroAgent: 'sudo docker logs -f ${INSTANCE_NAME}nekro_agent'"
echo "  NapCat: 'sudo docker logs -f ${INSTANCE_NAME}napcat'"

# 显示重要的配置信息
echo -e "\n=== 重要配置信息 ==="
ONEBOT_ACCESS_TOKEN=$(grep -m1 '^ONEBOT_ACCESS_TOKEN=' .env | cut -d '=' -f2)
NEKRO_ADMIN_PASSWORD=$(grep -m1 '^NEKRO_ADMIN_PASSWORD=' .env | cut -d '=' -f2)
QDRANT_API_KEY=$(grep -m1 '^QDRANT_API_KEY=' .env | cut -d '=' -f2)
echo "OneBot 访问令牌: ${ONEBOT_ACCESS_TOKEN}"
echo "管理员账号: admin | 密码: ${NEKRO_ADMIN_PASSWORD}"

echo -e "\n=== 服务访问信息 ==="
echo "NekroAgent 主服务端口: ${NEKRO_EXPOSE_PORT:-8021}"
echo "NekroAgent Web 访问地址: http://127.0.0.1:${NEKRO_EXPOSE_PORT:-8021}"
if [ "$WITH_NAPCAT" ]; then
    echo "NapCat 服务端口: ${NAPCAT_EXPOSE_PORT:-6099}"
else
    echo "OneBot WebSocket 连接地址: ws://127.0.0.1:${NEKRO_EXPOSE_PORT:-8021}/onebot/v11/ws"
fi

echo -e "\n=== 注意事项 ==="
echo "1. 如果您使用的是云服务器，请在云服务商控制台的安全组中放行以下端口："
echo "   - ${NEKRO_EXPOSE_PORT:-8021}/tcp (NekroAgent 主服务)"
if [ "$WITH_NAPCAT" ]; then
    echo "   - ${NAPCAT_EXPOSE_PORT:-6099}/tcp (NapCat 服务)"
fi
echo "2. 如果需要从外部访问，请将上述地址中的 127.0.0.1 替换为您的服务器公网IP"
echo "3. 请使用 'sudo docker logs ${INSTANCE_NAME}napcat' 查看机器人 QQ 账号二维码进行登录"
if [ "$WITH_QBOB" ]; then
    echo "4. qbob 已附带：在 NekroAgent 总后台 → 实例 → 新建，选「内置 qbob（QQ 官方机器人）」扫码接入。"
    echo "   qbob 自己的管理台只绑本机 127.0.0.1:${QBOB_ADMIN_EXPOSE_PORT:-17810}（日常用不到），首启口令在 ${NEKRO_DATA_DIR}/qbob_data/initial_admin_password.txt。"
    echo "   改 qbob 的 admin 口令之前，先在 NekroAgent 总后台打开一次「新建实例」页：NA 会用这个初始口令建好自己的服务账号；否则要在 NA 里手工提供 qbob 账号。"
    echo "   qbob 是 AGPL-3.0 的独立项目（https://github.com/Loping151/qbob），镜像由本机从上游源码构建。"
    echo "   手工升级用 'sudo docker compose pull --ignore-buildable'（qbob 镜像只在本机构建），再 'sudo docker compose up -d'。"
fi

echo -e "\n安装完成！祝您使用愉快！"
