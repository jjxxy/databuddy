#!/bin/sh
# Operator-only local ONLYOFFICE instance. Never bind the editor to the LAN.
set -eu
command -v docker >/dev/null || { echo '请先在服务主机安装 Docker。' >&2; exit 1; }
command -v socat >/dev/null || { echo '需要 socat 提供本机环回转发。' >&2; exit 1; }
command -v systemd-run >/dev/null || { echo '需要 systemd 管理本机转发服务。' >&2; exit 1; }
systemctl show --property=Version >/dev/null 2>&1 || { echo 'systemd 不可用，请由管理员启动。' >&2; exit 1; }
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/atlas-onlyoffice"
umask 077
mkdir -p "$config_dir"
secret_file="$config_dir/docker.env"
if [ ! -f "$secret_file" ]; then
  command -v openssl >/dev/null || { echo '需要 openssl 生成密钥。' >&2; exit 1; }
  printf 'JWT_SECRET=%s\nJWT_ENABLED=true\n' "$(openssl rand -hex 32)" > "$secret_file"
fi
chmod 600 "$secret_file"
network=atlas-onlyoffice-private
if docker network inspect "$network" >/dev/null 2>&1; then
  [ "$(docker network inspect --format '{{.Internal}}' "$network")" = true ] || { echo '同名 Docker 网络不是内部网络，请先核对。' >&2; exit 1; }
else
  docker network create --internal "$network" >/dev/null
fi
if docker container inspect atlas-onlyoffice >/dev/null 2>&1; then
  owner=$(docker inspect --format '{{ index .Config.Labels "com.atlas.onlyoffice" }}' atlas-onlyoffice)
  [ "$owner" = local ] || { echo '同名容器不属于此部署脚本，请先核对。' >&2; exit 1; }
  docker start atlas-onlyoffice >/dev/null
  networks=$(docker inspect --format '{{range $name, $value := .NetworkSettings.Networks}}{{$name}} {{end}}' atlas-onlyoffice)
  for item in $networks; do
    case "$item" in "$network"|bridge) ;; *) echo '容器连接了其他网络，请先核对。' >&2; exit 1;; esac
  done
  case " $networks " in *" $network "*) ;; *) docker network connect "$network" atlas-onlyoffice;; esac
  case " $networks " in *" bridge "*) docker network disconnect bridge atlas-onlyoffice;; esac
else
  docker run -d --name atlas-onlyoffice --label com.atlas.onlyoffice=local --restart unless-stopped --network "$network" \
    --env-file "$secret_file" \
    -v atlas-onlyoffice-data:/var/www/onlyoffice/Data \
    -v atlas-onlyoffice-logs:/var/log/onlyoffice \
    "${ATLAS_ONLYOFFICE_IMAGE:-onlyoffice/documentserver@sha256:ca17889f10b11141cc6441836aebdd2835cf3e1b49681b2fd6e155c94b7850b3}" >/dev/null
fi
address=$(docker inspect --format '{{with index .NetworkSettings.Networks "atlas-onlyoffice-private"}}{{.IPAddress}}{{end}}' atlas-onlyoffice)
case "$address" in ''|*[!0-9.]*) echo '无法确定容器内网地址。' >&2; exit 1;; esac
unit=atlas-onlyoffice-local-proxy
if systemctl is-active --quiet "$unit.service"; then systemctl stop "$unit.service"; fi
systemctl reset-failed "$unit.service" >/dev/null 2>&1 || :
systemd-run --quiet --collect --unit="$unit" --property=Restart=on-failure \
  "$(command -v socat)" TCP-LISTEN:18080,bind=127.0.0.1,reuseaddr,fork "TCP:$address:80"
echo '服务已启动，初始化可能需要数分钟。请检查 http://127.0.0.1:18080/healthcheck。'
echo '此服务尚未自动接入 Atlas；请勿向它上传真实财务文件，直到权限和回调接入完成。'
