#!/usr/bin/env bash
# Docker-only local launcher. Do not source .env: it contains private values.
set -euo pipefail

bundle_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd -- "$bundle_dir"
action="${1:-up}"
if (($#)); then shift; fi
no_open="${ATLAS_NO_OPEN:-0}"

usage() {
  printf '%s\n' '用法：bash start-local.sh [up|down|status|logs|open] [--folder PATH|--choose-folder] [--offline] [--no-open]'
  printf '%s\n' '默认 up。down 停止服务并保留数据；--no-open 不自动打开浏览器。'
}
case "$action" in
  -h|--help|help) usage; exit 0 ;;
  up|down|status|logs|open) ;;
  *) usage >&2; exit 2 ;;
esac
local_folder="${ATLAS_LOCAL_WORKSPACE_HOST:-}"
choose_folder=0
network_mode="${ATLAS_NETWORK_MODE:-connected}"
while (($#)); do
  case "$1" in
    --no-open) no_open=1; shift ;;
    --folder) (($# >= 2)) || { printf '%s\n' '--folder 需要目录路径' >&2; exit 2; }; local_folder="$2"; shift 2 ;;
    --choose-folder) choose_folder=1; shift ;;
    --offline) network_mode=local_only; shift ;;
    *) usage >&2; exit 2 ;;
  esac
done
if [[ -z "$local_folder" && "$choose_folder" == 0 && -f .atlas-local-folder ]]; then
  IFS= read -r local_folder < .atlas-local-folder || true
fi
if [[ "$action" == up && ( -z "$local_folder" || "$choose_folder" == 1 ) ]]; then
  if command -v osascript >/dev/null 2>&1; then
    local_folder="$(osascript -e 'POSIX path of (choose folder with prompt "选择 Atlas 业务工作文件夹")')" || exit 0
  elif command -v zenity >/dev/null 2>&1; then
    local_folder="$(zenity --file-selection --directory --title='选择 Atlas 业务工作文件夹')" || exit 0
  else
    printf '%s\n' '当前环境没有文件夹选择器，请运行 bash start-local.sh up --folder "/你的业务文件夹"。' >&2
    exit 2
  fi
fi
if [[ -n "$local_folder" ]]; then
  [[ -d "$local_folder" && ! -L "$local_folder" && "$local_folder" != / && "$local_folder" != *$'\n'* && "$local_folder" != *$'\r'* ]] || { printf '%s\n' '请选择实际存在的普通业务文件夹，不能是根目录或链接。' >&2; exit 2; }
  local_folder="$(cd -- "$local_folder" && pwd -P)"
  printf '%s\n' "$local_folder" > .atlas-local-folder
elif [[ "$action" != open ]]; then
  printf '%s\n' '尚未选择本地工作文件夹，请先运行 up --folder。' >&2; exit 2
fi
export ATLAS_LOCAL_WORKSPACE_HOST="$local_folder"
export ATLAS_LOCAL_LABEL="$(basename -- "$local_folder")"
export ATLAS_LOCAL_UID="${ATLAS_LOCAL_UID:-$(id -u)}"
export ATLAS_LOCAL_GID="${ATLAS_LOCAL_GID:-$(id -g)}"
if [[ "$ATLAS_LOCAL_UID" == 0 ]]; then
  printf '%s\n' '本地模式建议用拥有该文件夹的普通用户启动；root 启动需自行保证 UID 10001 可读写该文件夹，系统不会修改原文件权限。' >&2
  export ATLAS_LOCAL_UID=10001 ATLAS_LOCAL_GID=10001
fi
export ATLAS_NETWORK_MODE="$network_mode"

fail() { printf '%s\n' "$1" >&2; exit 1; }

local_url() {
  local port=8000 line value
  if [[ -f .env ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      line="${line%$'\r'}"
      if [[ "$line" =~ ^[[:space:]]*(export[[:space:]]+)?ATLAS_HTTP_PORT[[:space:]]*= ]]; then
        value="${line#*=}"
        value="${value#"${value%%[![:space:]]*}"}"
        value="${value%"${value##*[![:space:]]}"}"
        if [[ "$value" == \"*\" || "$value" == \'*\' ]]; then
          value="${value:1:${#value}-2}"
        else
          value="${value%% \#*}"
          value="${value%"${value##*[![:space:]]}"}"
        fi
        if [[ "$value" =~ ^0*([1-9][0-9]{0,4}|0)$ ]]; then
          port="${BASH_REMATCH[1]}"
        else
          fail '.env 中 ATLAS_HTTP_PORT 需为 1–65535 的数字端口。'
        fi
      fi
    done < .env
  fi
  port=$((10#$port))
  ((port >= 1 && port <= 65535)) || fail '.env 中 ATLAS_HTTP_PORT 需为 1–65535 的数字端口。'
  printf 'http://localhost:%s\n' "$port"
}

open_local() {
  local url
  url="$(local_url)"
  printf '工作空间：%s\n' "$url"
  if [[ "$no_open" == 1 ]]; then return; fi
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1 &
  elif command -v open >/dev/null 2>&1; then
    open "$url" >/dev/null 2>&1 || true
  else
    printf '%s\n' '请在浏览器中打开上方地址。'
  fi
}

if [[ "$action" == open ]]; then open_local; exit 0; fi
command -v docker >/dev/null 2>&1 || fail '请先安装并启动 Docker（含 Compose v2），再重新运行此脚本。'
docker_os="$(docker info --format '{{.OSType}}')" || fail 'Docker 尚未就绪，请启动 Docker Desktop 或 Docker Engine 后重试。'
[[ "$docker_os" == linux ]] || fail '请将 Docker Desktop 切换为 Linux 容器模式。'
docker compose version >/dev/null || fail '需要 Docker Compose v2，请更新 Docker Desktop 或安装 Compose 插件。'
[[ -f compose.local.yaml && -f quickstart.py ]] || fail '启动包不完整，请保留同目录的 quickstart.py 和 compose.local.yaml。'

# CSV quoting also supports spaces, commas and quotes in a Unix directory.
mount_source="${bundle_dir//\"/\"\"}"
mount_spec="type=bind,\"source=$mount_source\",target=/bundle"
bootstrap() {
  docker run --rm --network none --user "$(id -u):$(id -g)" \
    --env PYTHONDONTWRITEBYTECODE=1 --env PYTHONUTF8=1 \
    --mount "$mount_spec" --workdir /bundle \
    python:3.12-slim python quickstart.py "$@"
}
compose=(docker compose --env-file .env -f compose.local.yaml)

case "$action" in
  up)
    if bootstrap setup; then :; else
      code=$?
      printf '%s\n' '配置未通过，尚未启动服务。按上方提示填写 llm.json 后，再运行启动脚本。' >&2
      exit "$code"
    fi
    "${compose[@]}" up -d --build --wait --wait-timeout 180 || {
      code=$?
      printf '%s\n' '服务未能就绪。运行 bash start-local.sh logs 查看记录；无需删除已有数据卷。' >&2
      exit "$code"
    }
    bootstrap info
    open_local
    ;;
  down|status|logs)
    [[ -f .env ]] || fail '尚未生成启动配置，请先运行 bash start-local.sh。'
    case "$action" in
      down) "${compose[@]}" down ;;
      status) "${compose[@]}" ps --all ;;
      logs) "${compose[@]}" logs --tail 100 --follow ;;
    esac
    ;;
esac
