#!/usr/bin/env bash
set -euo pipefail

stamp="${1:-$(date +%Y%m%d-%H%M%S)}"
config_stage="/root/retired-backups/disabled-configs-${stamp}"
mkdir -p "${config_stage}/nginx" "${config_stage}/systemd"

move_if_present() {
  local source="$1"
  local destination="$2"
  if [[ -e "${source}" || -L "${source}" ]]; then
    mkdir -p "$(dirname "${destination}")"
    mv -- "${source}" "${destination}"
  fi
}

assert_exact_path() {
  local expected="$1"
  local resolved
  resolved="$(realpath -m -- "${expected}")"
  if [[ "${resolved}" != "${expected}" ]]; then
    printf 'Refusing unexpected path: %s -> %s\n' "${expected}" "${resolved}" >&2
    exit 1
  fi
}

remove_tree_if_present() {
  local target="$1"
  assert_exact_path "${target}"
  if [[ -e "${target}" || -L "${target}" ]]; then
    rm -rf -- "${target}"
  fi
}

systemctl disable --now \
  souleye-test-platform@aigolf-pc-120-smoke.timer 2>/dev/null || true
systemctl stop souleye-test-platform@aigolf-pc-120-smoke.service 2>/dev/null || true
systemctl disable --now aigolf-uat-api.service aigolf-uat-worker.service 2>/dev/null || true
systemctl reset-failed souleye-test-platform@aigolf-pc-120-smoke.service 2>/dev/null || true

docker rm -f \
  aigolf-uat-minio \
  aigolf-uat-redis \
  aigolf-uat-postgres 2>/dev/null || true
docker volume rm \
  aigolf-uat_postgres_data \
  aigolf-uat_redis_data \
  aigolf-uat_minio_data 2>/dev/null || true

move_if_present /etc/nginx/conf.d/souleye-test-platform.conf \
  "${config_stage}/nginx/souleye-test-platform.conf"
move_if_present /etc/nginx/sites-available/aigolf-uat.conf \
  "${config_stage}/nginx/aigolf-uat.conf"
move_if_present /etc/nginx/sites-enabled/aigolf-uat.conf \
  "${config_stage}/nginx/aigolf-uat.enabled.conf"
move_if_present /etc/nginx/snippets/aigolf-uat-public-locations.conf \
  "${config_stage}/nginx/aigolf-uat-public-locations.conf"

if ! nginx -t; then
  move_if_present "${config_stage}/nginx/souleye-test-platform.conf" \
    /etc/nginx/conf.d/souleye-test-platform.conf
  move_if_present "${config_stage}/nginx/aigolf-uat.conf" \
    /etc/nginx/sites-available/aigolf-uat.conf
  move_if_present "${config_stage}/nginx/aigolf-uat.enabled.conf" \
    /etc/nginx/sites-enabled/aigolf-uat.conf
  move_if_present "${config_stage}/nginx/aigolf-uat-public-locations.conf" \
    /etc/nginx/snippets/aigolf-uat-public-locations.conf
  nginx -t
  printf 'Nginx retirement failed; configuration restored.\n' >&2
  exit 1
fi
systemctl reload nginx

move_if_present /etc/systemd/system/aigolf-uat-api.service \
  "${config_stage}/systemd/aigolf-uat-api.service"
move_if_present /etc/systemd/system/aigolf-uat-worker.service \
  "${config_stage}/systemd/aigolf-uat-worker.service"
move_if_present /etc/systemd/system/souleye-test-platform@.service \
  "${config_stage}/systemd/souleye-test-platform@.service"
move_if_present /etc/systemd/system/souleye-test-platform@.timer \
  "${config_stage}/systemd/souleye-test-platform@.timer"
move_if_present \
  /etc/systemd/system/timers.target.wants/souleye-test-platform@aigolf-pc-120-smoke.timer \
  "${config_stage}/systemd/souleye-test-platform@aigolf-pc-120-smoke.timer.link"
systemctl daemon-reload

for target in \
  /opt/aigolf-ops-platform-uat \
  /var/www/aigolf-uat \
  /opt/souleye-test-platform \
  /srv/souleye-test-platform \
  /etc/souleye-test-platform \
  /var/log/souleye-test-platform \
  /restore \
  /tmp/security-audit-trivy-cache \
  /tmp/security-audit-trivy-results-20260710 \
  /tmp/security-audit-trivy-0.70.0 \
  /tmp/playwright-transform-cache-0 \
  /tmp/node-compile-cache \
  /tmp/tsx-0 \
  /tmp/verify-platform-head \
  /root/.cache/ms-playwright \
  /root/.cache/pnpm \
  /root/.cache/prisma \
  /root/.cache/node \
  /root/.local/share/pnpm
do
  remove_tree_if_present "${target}"
done

for file in \
  /tmp/items.out \
  /tmp/index.out \
  /tmp/manifest-check.err \
  /tmp/manifest-check.json \
  /tmp/aigolf-tournament-report-noauth.html \
  /tmp/aigolf-tournament-report-md-noauth.md \
  /tmp/aigolf-tournament-report-md-check.md \
  /tmp/report-noauth-check.html \
  /tmp/aigolf-tournament-report-check.html \
  /tmp/deploy-platform-old8.sh \
  /tmp/bootstrap-old8.sh
do
  assert_exact_path "${file}"
  rm -f -- "${file}"
done

docker image rm \
  docker.m.daocloud.io/library/postgres:16-alpine \
  docker.m.daocloud.io/library/redis:7-alpine \
  docker.m.daocloud.io/minio/minio:latest 2>/dev/null || true
docker image prune -f >/dev/null

apt-get clean
journalctl --vacuum-size=100M >/dev/null

printf 'CONFIG_STAGE=%s\n' "${config_stage}"
printf 'RETIREMENT_COMPLETE=1\n'
