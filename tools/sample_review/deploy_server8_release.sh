#!/usr/bin/env bash
set -Eeuo pipefail

archive="${1:?sample-review archive path is required}"
expected_sha256="${EXPECTED_ARCHIVE_SHA256:?EXPECTED_ARCHIVE_SHA256 is required}"
expected_commit="${EXPECTED_SOURCE_COMMIT:?EXPECTED_SOURCE_COMMIT is required}"
validate_only="${AI_BOT_SAMPLE_REVIEW_VALIDATE_ONLY:-0}"
test_mode="${AI_BOT_SAMPLE_REVIEW_TEST_MODE:-0}"
base="${AI_BOT_SAMPLE_REVIEW_BASE:-/opt/ai-bot-sample-review}"
data_root="${AI_BOT_SAMPLE_REVIEW_DATA_ROOT:-/srv/ai-bot-sample-review}"
unit_path="${AI_BOT_SAMPLE_REVIEW_UNIT_PATH:-/etc/systemd/system/ai-bot-sample-review.service}"
lock_file="${AI_BOT_SAMPLE_REVIEW_LOCK_FILE:-/run/lock/ai-bot-sample-review-deploy.lock}"
health_url="${AI_BOT_SAMPLE_REVIEW_HEALTH_URL:-http://127.0.0.1:8793/healthz}"
health_attempts="${AI_BOT_SAMPLE_REVIEW_HEALTH_ATTEMPTS:-30}"
health_sleep="${AI_BOT_SAMPLE_REVIEW_HEALTH_SLEEP_SECONDS:-2}"

case "$expected_sha256" in
  *[!0-9A-Fa-f]*|'') echo 'EXPECTED_ARCHIVE_SHA256 must be hexadecimal' >&2; exit 2 ;;
esac
[[ ${#expected_sha256} -eq 64 ]] || {
  echo 'EXPECTED_ARCHIVE_SHA256 must be exactly 64 hexadecimal characters' >&2
  exit 2
}
expected_sha256="${expected_sha256,,}"
case "$expected_commit" in
  *[!0-9A-Fa-f]*|'') echo 'EXPECTED_SOURCE_COMMIT must be hexadecimal' >&2; exit 2 ;;
esac
[[ ${#expected_commit} -eq 40 ]] || {
  echo 'EXPECTED_SOURCE_COMMIT must be exactly 40 hexadecimal characters' >&2
  exit 2
}
expected_commit="${expected_commit,,}"
[[ "$health_attempts" =~ ^[1-9][0-9]{0,2}$ ]] || {
  echo 'AI_BOT_SAMPLE_REVIEW_HEALTH_ATTEMPTS must be between 1 and 999' >&2
  exit 2
}
[[ "$health_sleep" =~ ^[0-9]+([.][0-9]+)?$ ]] || {
  echo 'AI_BOT_SAMPLE_REVIEW_HEALTH_SLEEP_SECONDS must be non-negative' >&2
  exit 2
}

for command_name in basename chmod curl date dirname flock grep id install ln mkdir mv python3 readlink realpath rm seq sha256sum sleep systemctl; do
  command -v "$command_name" >/dev/null || {
    printf 'missing required command: %s\n' "$command_name" >&2
    exit 2
  }
done
[[ -f "$archive" && ! -L "$archive" ]] || {
  echo 'sample-review archive must be a regular non-symlink file' >&2
  exit 2
}
printf '%s  %s\n' "$expected_sha256" "$archive" | sha256sum --check --status || {
  echo 'sample-review archive SHA-256 mismatch' >&2
  exit 3
}

verify_or_extract() {
  local source="$1"
  local destination="${2:-}"
  python3 - "$source" "$expected_commit" "$destination" <<'PY'
import hashlib
import json
import os
import pathlib
import sys
import tarfile

source, expected_commit, destination = sys.argv[1:]
maximum_archive = 64 * 1024 * 1024
maximum_expanded = 128 * 1024 * 1024
maximum_file = 16 * 1024 * 1024
maximum_entries = 1000
if os.path.getsize(source) > maximum_archive:
    raise SystemExit("release archive exceeds compressed-size limit")

files = {}
names = []
total = 0
try:
    bundle = tarfile.open(source, mode="r:gz")
except (OSError, tarfile.TarError) as exc:
    raise SystemExit(f"invalid release archive: {exc}")
with bundle:
    for member in bundle:
        if len(names) >= maximum_entries:
            raise SystemExit("release archive entry count exceeds limit")
        raw = member.name
        path = pathlib.PurePosixPath(raw)
        if (not member.isfile() or not raw or raw.startswith("/") or "\\" in raw or
                any(part in {"", ".", ".."} for part in path.parts) or raw in files):
            raise SystemExit(f"unsafe release archive member: {raw!r}")
        if member.size < 0 or member.size > maximum_file:
            raise SystemExit(f"release archive member exceeds limit: {raw}")
        total += member.size
        if total > maximum_expanded:
            raise SystemExit("release archive expanded size exceeds limit")
        stream = bundle.extractfile(member)
        if stream is None:
            raise SystemExit(f"cannot read release archive member: {raw}")
        payload = stream.read(maximum_file + 1)
        if len(payload) != member.size:
            raise SystemExit(f"release archive member size mismatch: {raw}")
        names.append(raw)
        files[raw] = payload
if names != sorted(names):
    raise SystemExit("release archive members are not canonically ordered")

manifest_name = "release/ai-bot-sample-review-release.json"
raw_manifest = files.pop(manifest_name, None)
if raw_manifest is None:
    raise SystemExit("release archive manifest is missing")
try:
    manifest = json.loads(raw_manifest.decode("utf-8"))
except (UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise SystemExit(f"invalid release archive manifest: {exc}")
required = {"schema", "commit", "commit_short", "source_date_epoch", "files"}
if (not isinstance(manifest, dict) or set(manifest) != required or
        manifest.get("schema") != "ai-bot.sample-review.release.v1" or
        manifest.get("commit") != expected_commit or
        manifest.get("commit_short") != expected_commit[:12] or
        not isinstance(manifest.get("source_date_epoch"), int) or
        manifest["source_date_epoch"] <= 0):
    raise SystemExit("release archive manifest identity is invalid")
recorded = manifest.get("files")
if not isinstance(recorded, dict) or set(recorded) != set(files):
    raise SystemExit("release archive manifest file set mismatch")
for name, payload in files.items():
    if recorded.get(name) != hashlib.sha256(payload).hexdigest():
        raise SystemExit(f"release archive file digest mismatch: {name}")

if destination:
    root = pathlib.Path(destination)
    root.mkdir(mode=0o755, parents=True, exist_ok=False)
    all_files = dict(files)
    all_files[manifest_name] = raw_manifest
    for name, payload in sorted(all_files.items()):
        target = root.joinpath(*pathlib.PurePosixPath(name).parts)
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        target.chmod(0o644)
PY
}

verify_or_extract "$archive"
if [[ "$validate_only" == 1 ]]; then
  echo AI_BOT_SAMPLE_REVIEW_ARCHIVE_VALIDATION_OK
  exit 0
fi
command -v sqlite3 >/dev/null || {
  echo 'missing required command: sqlite3' >&2
  exit 2
}

if [[ "$test_mode" != 1 && "$(id -u)" != 0 ]]; then
  echo 'sample-review deployment must run as root' >&2
  exit 2
fi
if [[ "$test_mode" != 1 ]]; then
  [[ "$base" == /opt/ai-bot-sample-review &&
     "$data_root" == /srv/ai-bot-sample-review &&
     "$unit_path" == /etc/systemd/system/ai-bot-sample-review.service &&
     "$lock_file" == /run/lock/ai-bot-sample-review-deploy.lock &&
     "$health_url" == http://127.0.0.1:8793/healthz ]] || {
    echo 'production sample-review deployment paths are fixed' >&2
    exit 2
  }
fi

base="$(realpath -m -- "$base")"
data_root="$(realpath -m -- "$data_root")"
unit_path="$(realpath -m -- "$unit_path")"
lock_file="$(realpath -m -- "$lock_file")"
[[ "$base" == /* && "$data_root" == /* && "$unit_path" == /* && "$lock_file" == /* ]] || {
  echo 'sample-review deployment paths must be absolute' >&2
  exit 2
}
[[ -d "$data_root" && ! -L "$data_root" ]] || {
  echo 'existing non-symlink sample-review data root is required' >&2
  exit 2
}

install -d -m 0700 "$base" "$base/releases" "$base/archives" "$data_root/backups"
install -d -m 0755 "$(dirname -- "$lock_file")"
exec 9>"$lock_file"
flock 9

trusted_archive="$base/archives/${expected_sha256}.tar.gz"
trusted_partial="$base/archives/.${expected_sha256}.$$.partial"
staging="$base/.staging-${expected_commit}-$$"
release_dir="$base/releases/$expected_commit"
current="$base/current"
current_next="$base/.current-${expected_commit}-$$"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="$data_root/backups/release-${timestamp}-${expected_commit:0:12}"
old_current=""
activation_started=0
release_succeeded=0

cleanup_work_path() {
  local target="$1"
  case "$target" in
    "$base"/.staging-*|"$base"/.current-*) ;;
    *) echo "refusing to remove unexpected work path: $target" >&2; return 1 ;;
  esac
  if [[ -d "$target" && ! -L "$target" ]]; then
    rm -rf -- "$target"
  elif [[ -L "$target" ]]; then
    rm -f -- "$target"
  fi
}

rollback() {
  local status=$?
  rm -f -- "$trusted_partial"
  cleanup_work_path "$staging" || true
  cleanup_work_path "$current_next" || true
  if [[ "$release_succeeded" == 0 && "$activation_started" == 1 ]]; then
    echo 'sample-review activation failed; restoring previous unit and release' >&2
    systemctl stop ai-bot-sample-review.service >/dev/null 2>&1 || true
    if [[ -f "$backup/ai-bot-sample-review.service" ]]; then
      install -m 0644 "$backup/ai-bot-sample-review.service" "$unit_path"
    fi
    if [[ -n "$old_current" ]]; then
      ln -s "$old_current" "$current_next"
      mv -Tf -- "$current_next" "$current"
    else
      rm -f -- "$current"
    fi
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl start ai-bot-sample-review.service >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap rollback EXIT

python3 - "$archive" "$trusted_partial" "$expected_sha256" <<'PY'
import hashlib
import os
import stat
import sys

source, destination, expected = sys.argv[1:]
maximum = 64 * 1024 * 1024
source_fd = destination_fd = None
try:
    source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    details = os.fstat(source_fd)
    if not stat.S_ISREG(details.st_mode) or details.st_size > maximum:
        raise ValueError("release archive exceeds compressed-size limit")
    destination_fd = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    digest = hashlib.sha256()
    copied = 0
    while True:
        chunk = os.read(source_fd, 1024 * 1024)
        if not chunk:
            break
        copied += len(chunk)
        if copied > maximum:
            raise ValueError("release archive exceeds compressed-size limit")
        digest.update(chunk)
        view = memoryview(chunk)
        while view:
            view = view[os.write(destination_fd, view):]
    os.fsync(destination_fd)
    if digest.hexdigest() != expected:
        raise ValueError("release archive SHA-256 mismatch")
except (OSError, ValueError) as exc:
    raise SystemExit(str(exc))
finally:
    if source_fd is not None:
        os.close(source_fd)
    if destination_fd is not None:
        os.close(destination_fd)
PY
mv -f -- "$trusted_partial" "$trusted_archive"
chmod 0600 "$trusted_archive"
verify_or_extract "$trusted_archive"

if [[ -e "$release_dir" || -L "$release_dir" ]]; then
  [[ -d "$release_dir" && ! -L "$release_dir" ]] || {
    echo 'existing release path has an unsafe type' >&2
    exit 3
  }
  grep -Fq "\"commit\":\"$expected_commit\"" \
    "$release_dir/release/ai-bot-sample-review-release.json" || {
    echo 'existing release identity mismatch' >&2
    exit 3
  }
else
  verify_or_extract "$trusted_archive" "$staging"
  mv -- "$staging" "$release_dir"
fi
[[ -f "$release_dir/tools/sample_review/ai-bot-sample-review-server8.service" ]] || {
  echo 'release is missing the Server-8 systemd unit' >&2
  exit 3
}

if [[ -L "$current" ]]; then
  old_current="$(readlink -- "$current")"
elif [[ -e "$current" ]]; then
  echo 'current release path must be a symlink' >&2
  exit 3
fi

install -d -m 0700 "$backup"
if [[ -f "$unit_path" && ! -L "$unit_path" ]]; then
  install -m 0600 "$unit_path" "$backup/ai-bot-sample-review.service"
else
  echo 'existing non-symlink sample-review unit is required' >&2
  exit 3
fi
printf '%s\n' "$old_current" >"$backup/previous-current.txt"
printf '%s  %s\n' "$expected_sha256" "$(basename -- "$trusted_archive")" >"$backup/release.sha256"

activation_started=1
systemctl stop ai-bot-sample-review.service
database="$data_root/data/review.sqlite3"
if [[ -f "$database" && ! -L "$database" ]]; then
  sqlite3 "$database" ".timeout 5000" ".backup '$backup/review.sqlite3'"
  [[ "$(sqlite3 "$backup/review.sqlite3" 'PRAGMA integrity_check;')" == ok ]] || {
    echo 'SQLite backup integrity check failed' >&2
    exit 3
  }
  chmod 0600 "$backup/review.sqlite3"
fi

ln -s "$release_dir" "$current_next"
mv -Tf -- "$current_next" "$current"
install -m 0644 \
  "$release_dir/tools/sample_review/ai-bot-sample-review-server8.service" \
  "$unit_path"
systemctl daemon-reload
systemctl start ai-bot-sample-review.service

healthy=0
for _ in $(seq 1 "$health_attempts"); do
  if curl --fail --silent --show-error --max-time 3 "$health_url" >/dev/null; then
    healthy=1
    break
  fi
  sleep "$health_sleep"
done
[[ "$healthy" == 1 ]] || {
  echo 'sample-review health check failed after activation' >&2
  exit 4
}

release_succeeded=1
echo AI_BOT_SAMPLE_REVIEW_RELEASE_OK
printf 'AI_BOT_SAMPLE_REVIEW_RELEASE=%s\n' "$release_dir"
printf 'AI_BOT_SAMPLE_REVIEW_RELEASE_SHA256=%s\n' "$expected_sha256"
printf 'AI_BOT_SAMPLE_REVIEW_BACKUP=%s\n' "$backup"
