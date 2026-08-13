#!/usr/bin/env bash
set -euo pipefail

stamp="${1:-$(date +%Y%m%d-%H%M%S)}"
backup_root="/root/retired-backups"
stage="${backup_root}/.old8-legacy-${stamp}"
archive="${backup_root}/old8-legacy-${stamp}.tar.gz"

umask 077
mkdir -p "${backup_root}"
rm -rf -- "${stage}"
mkdir -p "${stage}/configs" "${stage}/data" "${stage}/sources" "${stage}/reports"

copy_if_present() {
  local source="$1"
  local destination="$2"
  if [[ -e "${source}" ]]; then
    mkdir -p "$(dirname "${destination}")"
    cp -a -- "${source}" "${destination}"
  fi
}

copy_if_present /etc/systemd/system/aigolf-uat-api.service \
  "${stage}/configs/systemd/aigolf-uat-api.service"
copy_if_present /etc/systemd/system/aigolf-uat-worker.service \
  "${stage}/configs/systemd/aigolf-uat-worker.service"
copy_if_present /etc/systemd/system/souleye-test-platform@.service \
  "${stage}/configs/systemd/souleye-test-platform@.service"
copy_if_present /etc/systemd/system/souleye-test-platform@.timer \
  "${stage}/configs/systemd/souleye-test-platform@.timer"
copy_if_present /etc/systemd/system/timers.target.wants/souleye-test-platform@aigolf-pc-120-smoke.timer \
  "${stage}/configs/systemd/souleye-test-platform@aigolf-pc-120-smoke.timer.link"
copy_if_present /etc/nginx/conf.d/souleye-test-platform.conf \
  "${stage}/configs/nginx/souleye-test-platform.conf"
copy_if_present /etc/nginx/sites-available/aigolf-uat.conf \
  "${stage}/configs/nginx/aigolf-uat.conf"
copy_if_present /etc/nginx/snippets/aigolf-uat-public-locations.conf \
  "${stage}/configs/nginx/aigolf-uat-public-locations.conf"
copy_if_present /etc/souleye-test-platform \
  "${stage}/configs/souleye-test-platform"
copy_if_present /opt/aigolf-ops-platform-uat/.env \
  "${stage}/configs/aigolf-uat.env"

if docker ps --format '{{.Names}}' | grep -qx aigolf-uat-postgres; then
  docker exec aigolf-uat-postgres sh -lc \
    'pg_dumpall -U "$POSTGRES_USER"' | gzip -9 > "${stage}/data/aigolf-postgres.sql.gz"
fi
if docker ps --format '{{.Names}}' | grep -qx aigolf-uat-minio; then
  docker cp aigolf-uat-minio:/data "${stage}/data/minio"
fi
if docker ps --format '{{.Names}}' | grep -qx aigolf-uat-redis; then
  docker cp aigolf-uat-redis:/data "${stage}/data/redis"
fi

if [[ -d /opt/aigolf-ops-platform-uat ]]; then
  tar -czf "${stage}/sources/aigolf-ops-platform-uat.tar.gz" \
    --exclude='aigolf-ops-platform-uat/node_modules' \
    --exclude='aigolf-ops-platform-uat/.git' \
    --exclude='aigolf-ops-platform-uat/.env' \
    -C /opt aigolf-ops-platform-uat
fi

if [[ -d /opt/souleye-test-platform ]]; then
  tar -czf "${stage}/sources/souleye-test-platform.tar.gz" \
    --exclude='soueleye-test-platform/**/node_modules' \
    --exclude='souleye-test-platform/**/node_modules' \
    --exclude='souleye-test-platform/**/.git' \
    -C /opt souleye-test-platform
fi

if [[ -d /srv/souleye-test-platform/reports/aigolf/120 ]]; then
  latest_report="$(
    find /srv/souleye-test-platform/reports/aigolf/120 \
      -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort | tail -n 1
  )"
  if [[ -n "${latest_report}" ]]; then
    cp -a -- \
      "/srv/souleye-test-platform/reports/aigolf/120/${latest_report}" \
      "${stage}/reports/"
  fi
fi

{
  echo "created_at=$(date --iso-8601=seconds)"
  echo "host=$(hostname)"
  echo "retired_scope=souleye-test-platform,aigolf-uat"
  echo "excluded=/restore,node_modules,git-history,bulk-test-reports,caches"
  echo
  find "${stage}" -type f -printf '%s %P\n' | sort -k2
} > "${stage}/MANIFEST.txt"

find "${stage}" -type f ! -name SHA256SUMS -print0 \
  | sort -z \
  | xargs -0 sha256sum \
  | sed "s#${stage}/##" > "${stage}/SHA256SUMS"

tar -czf "${archive}" -C "${backup_root}" "$(basename "${stage}")"
sha256sum "${archive}" > "${archive}.sha256"
rm -rf -- "${stage}"

chmod 600 "${archive}" "${archive}.sha256"
printf 'ARCHIVE=%s\n' "${archive}"
printf 'SIZE_BYTES=%s\n' "$(stat -c %s "${archive}")"
printf 'SHA256=%s\n' "$(cut -d' ' -f1 "${archive}.sha256")"
