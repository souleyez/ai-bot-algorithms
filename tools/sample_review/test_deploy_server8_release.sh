#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
test_root="$(mktemp -d)"
cleanup() {
  rm -rf -- "$test_root"
}
trap cleanup EXIT

commit=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
archive="$test_root/sample-review.tar.gz"
PYTHONPATH="$repo_root" python3 - "$archive" "$commit" <<'PY'
import sys
from pathlib import Path
from tools.sample_review.package_server8_release import build_archive_bytes

archive = Path(sys.argv[1])
commit = sys.argv[2]
archive.write_bytes(
    build_archive_bytes(
        {
            "tools/sample_review/server.py": b"print('fixture')\n",
            "tools/sample_review/static/app.js": b"console.log('fixture')\n",
            "tools/sample_review/ai-bot-sample-review-server8.service": b"[Unit]\nDescription=fixture\n",
        },
        commit,
        1_700_000_000,
    )
)
PY
sha256="$(sha256sum "$archive" | awk '{print $1}')"

EXPECTED_ARCHIVE_SHA256="$sha256" \
EXPECTED_SOURCE_COMMIT="$commit" \
AI_BOT_SAMPLE_REVIEW_VALIDATE_ONLY=1 \
bash "$script_dir/deploy_server8_release.sh" "$archive" |
  grep -Fxq AI_BOT_SAMPLE_REVIEW_ARCHIVE_VALIDATION_OK

bin="$test_root/bin"
mkdir -p "$bin"
cat >"$bin/systemctl" <<'SH'
#!/usr/bin/env bash
exit 0
SH
cat >"$bin/curl" <<'SH'
#!/usr/bin/env bash
exit 0
SH
chmod 0755 "$bin/systemctl" "$bin/curl"

base="$test_root/opt/ai-bot-sample-review"
data="$test_root/srv/ai-bot-sample-review"
unit="$test_root/etc/ai-bot-sample-review.service"
mkdir -p "$base/releases/old" "$data/data" "$(dirname "$unit")"
printf old >"$base/releases/old/marker"
ln -s "$base/releases/old" "$base/current"
printf '[Unit]\nDescription=old\n' >"$unit"
python3 - "$data/data/review.sqlite3" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as connection:
    connection.execute("CREATE TABLE items (id TEXT PRIMARY KEY)")
    connection.execute("INSERT INTO items VALUES ('before-release')")
PY

PATH="$bin:$PATH" \
EXPECTED_ARCHIVE_SHA256="$sha256" \
EXPECTED_SOURCE_COMMIT="$commit" \
AI_BOT_SAMPLE_REVIEW_TEST_MODE=1 \
AI_BOT_SAMPLE_REVIEW_BASE="$base" \
AI_BOT_SAMPLE_REVIEW_DATA_ROOT="$data" \
AI_BOT_SAMPLE_REVIEW_UNIT_PATH="$unit" \
AI_BOT_SAMPLE_REVIEW_LOCK_FILE="$test_root/deploy.lock" \
AI_BOT_SAMPLE_REVIEW_HEALTH_ATTEMPTS=1 \
AI_BOT_SAMPLE_REVIEW_HEALTH_SLEEP_SECONDS=0 \
bash "$script_dir/deploy_server8_release.sh" "$archive" >/dev/null

[[ "$(readlink "$base/current")" == "$base/releases/$commit" ]]
grep -Fq 'Description=fixture' "$unit"
find "$data/backups" -name review.sqlite3 -type f | grep -q .

# A failed health check restores both the previous current link and unit.
rm -f "$bin/curl"
cat >"$bin/curl" <<'SH'
#!/usr/bin/env bash
exit 1
SH
chmod 0755 "$bin/curl"
second_commit=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
second_archive="$test_root/sample-review-second.tar.gz"
PYTHONPATH="$repo_root" python3 - "$second_archive" "$second_commit" <<'PY'
import sys
from pathlib import Path
from tools.sample_review.package_server8_release import build_archive_bytes

Path(sys.argv[1]).write_bytes(
    build_archive_bytes(
        {
            "tools/sample_review/server.py": b"print('second')\n",
            "tools/sample_review/static/app.js": b"console.log('second')\n",
            "tools/sample_review/ai-bot-sample-review-server8.service": b"[Unit]\nDescription=second\n",
        },
        sys.argv[2],
        1_700_000_001,
    )
)
PY
second_sha="$(sha256sum "$second_archive" | awk '{print $1}')"
set +e
PATH="$bin:$PATH" \
EXPECTED_ARCHIVE_SHA256="$second_sha" \
EXPECTED_SOURCE_COMMIT="$second_commit" \
AI_BOT_SAMPLE_REVIEW_TEST_MODE=1 \
AI_BOT_SAMPLE_REVIEW_BASE="$base" \
AI_BOT_SAMPLE_REVIEW_DATA_ROOT="$data" \
AI_BOT_SAMPLE_REVIEW_UNIT_PATH="$unit" \
AI_BOT_SAMPLE_REVIEW_LOCK_FILE="$test_root/deploy.lock" \
AI_BOT_SAMPLE_REVIEW_HEALTH_ATTEMPTS=1 \
AI_BOT_SAMPLE_REVIEW_HEALTH_SLEEP_SECONDS=0 \
bash "$script_dir/deploy_server8_release.sh" "$second_archive" >/dev/null 2>&1
status=$?
set -e
[[ "$status" -ne 0 ]]
[[ "$(readlink "$base/current")" == "$base/releases/$commit" ]]
grep -Fq 'Description=fixture' "$unit"

echo DEPLOY_SAMPLE_REVIEW_RELEASE_TESTS_OK
