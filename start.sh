#!/usr/bin/env bash
# Docker-only local launcher. Do not source .env: it contains private values.
set -euo pipefail

bundle_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd -- "$bundle_dir"
action="${1:-up}"
if (($#)); then shift; fi
no_open="${ATLAS_NO_OPEN:-0}"

usage() {
  printf '%s\n' '用法：bash start.sh [up|down|status|logs|open] [--no-open]'
  printf '%s\n' '默认 up。down 停止服务并保留数据；--no-open 不自动打开浏览器。'
}
case "$action" in
  -h|--help|help) usage; exit 0 ;;
  up|down|status|logs|open) ;;
  *) usage >&2; exit 2 ;;
esac
for option in "$@"; do
  case "$option" in
    --no-open) no_open=1 ;;
    *) usage >&2; exit 2 ;;
  esac
done

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
[[ -f compose.quickstart.yaml && -f quickstart.py ]] || fail '启动包不完整，请保留同目录的 quickstart.py 和 compose.quickstart.yaml。'

# CSV quoting also supports spaces, commas and quotes in a Unix directory.
mount_source="${bundle_dir//\"/\"\"}"
mount_spec="type=bind,\"source=$mount_source\",target=/bundle"
bootstrap() {
  docker run --rm --network none --user "$(id -u):$(id -g)" \
    --env PYTHONDONTWRITEBYTECODE=1 --env PYTHONUTF8=1 \
    --mount "$mount_spec" --workdir /bundle \
    python:3.12-slim python quickstart.py "$@"
}
compose=(docker compose --env-file .env -f compose.quickstart.yaml)

case "$action" in
  up)
    if bootstrap setup; then :; else
      code=$?
      printf '%s\n' '配置未通过，尚未启动服务。按上方提示填写 llm.json 后，再运行启动脚本。' >&2
      exit "$code"
    fi
    "${compose[@]}" up -d --build --wait --wait-timeout 180 || {
      code=$?
      printf '%s\n' '服务未能就绪。运行 bash start.sh logs 查看记录；无需删除已有数据卷。' >&2
      exit "$code"
    }
    bootstrap info
    open_local
    ;;
  down|status|logs)
    [[ -f .env ]] || fail '尚未生成启动配置，请先运行 bash start.sh。'
    case "$action" in
      down) "${compose[@]}" down ;;
      status) "${compose[@]}" ps --all ;;
      logs) "${compose[@]}" logs --tail 100 --follow ;;
    esac
    ;;
esac
