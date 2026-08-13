#!/usr/bin/env bash
set -euo pipefail

archive=${1:-/tmp/ai-bot-sample-review.tar.gz}
target=/srv/ai-bot-sample-review
staging=/srv/ai-bot-sample-review.new
nginx_conf=/etc/nginx/conf.d/public-admin.conf
snippet=/etc/nginx/snippets/ai-bot-sample-review.conf
stamp=$(date +%Y%m%d-%H%M%S)

rm -rf "$staging"
mkdir -p "$staging"
tar -xzf "$archive" -C "$staging"

if [[ -f "$target/data/review.sqlite3" ]]; then
  cp -a "$target/data/review.sqlite3" "$staging/data/review.sqlite3"
fi

if [[ -d "$target" ]]; then
  mv "$target" "${target}.backup-${stamp}"
fi
mv "$staging" "$target"

install -m 0644 "$target/ai-bot-sample-review.service" /etc/systemd/system/ai-bot-sample-review.service
install -d -m 0755 "$(dirname "$snippet")"
install -m 0644 "$target/nginx-location.conf" "$snippet"
cp -a "$nginx_conf" "${nginx_conf}.backup-${stamp}"

python3 - "$nginx_conf" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
include = "    include /etc/nginx/snippets/ai-bot-sample-review.conf;\n"
if include not in text:
    server_name = text.find("server_name ad.goods-editor.com;")
    if server_name < 0:
        raise SystemExit("HTTPS server block for ad.goods-editor.com not found")
    location = text.find("    location / {", server_name)
    if location < 0:
        raise SystemExit("root location in HTTPS server block not found")
    text = text[:location] + include + "\n" + text[location:]
    path.write_text(text, encoding="utf-8")
PY

systemctl daemon-reload
systemctl enable --now ai-bot-sample-review.service
nginx -t
systemctl reload nginx

curl --fail --silent http://127.0.0.1:8792/healthz
