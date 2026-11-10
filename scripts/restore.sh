#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  echo "Usage: $0 BACKUP_ARCHIVE --confirm-overwrite" >&2
  echo "This replaces the configured PostgreSQL database and /data Docker volume." >&2
}

if [[ $# -ne 2 || "$2" != "--confirm-overwrite" ]]; then
  usage
  exit 2
fi

archive="$(realpath -- "$1")"
if [[ ! -f "$archive" ]]; then
  echo "Backup archive not found: $archive" >&2
  exit 1
fi

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
restore_temp_parent="${RESTORE_TMPDIR:-$(dirname -- "$archive")}"
mkdir -p -- "$restore_temp_parent"
work_dir="$(mktemp -d "$restore_temp_parent/.takumi-restore.XXXXXXXX")"
cleanup() { rm -rf -- "$work_dir"; }
trap cleanup EXIT
umask 077

python - "$archive" "$work_dir" <<'PY'
import sys
import tarfile
from pathlib import Path
from pathlib import PurePosixPath

archive = Path(sys.argv[1])
destination = Path(sys.argv[2])
expected = {"database.dump", "data.tar.gz", "metadata.json", "SHA256SUMS"}
with tarfile.open(archive, "r:gz") as bundle:
    members = bundle.getmembers()
    names = [member.name for member in members]
    if set(names) != expected or len(names) != len(expected):
        raise SystemExit("Backup archive must contain exactly the four expected files")
    for member in members:
        if not member.isfile():
            raise SystemExit(f"Backup entry is not a regular file: {member.name}")
        source = bundle.extractfile(member)
        if source is None:
            raise SystemExit(f"Could not read backup entry: {member.name}")
        with source, (destination / member.name).open("xb") as output:
            while chunk := source.read(1024 * 1024):
                output.write(chunk)

with tarfile.open(destination / "data.tar.gz", "r:gz") as data_bundle:
    for member in data_bundle.getmembers():
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise SystemExit(f"Artifact archive contains an unsafe path: {member.name}")
        if not (member.isfile() or member.isdir()):
            raise SystemExit(f"Artifact archive contains an unsupported entry: {member.name}")
PY

(cd "$work_dir" && sha256sum --check --status SHA256SUMS)
if ! python - "$work_dir/metadata.json" <<'PY'
import json
import sys
from pathlib import Path

metadata = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if metadata.get("format") != "takumi-backup-v1":
    raise SystemExit("Unsupported Takumi backup format")
PY
then
  echo "Backup metadata is invalid or unsupported." >&2
  exit 1
fi

echo "Restore will replace the configured database and the shared /data volume."
echo "All running Takumi API and worker services will be stopped."

docker compose up -d postgres
ready=false
for _ in {1..60}; do
  if docker compose exec -T postgres sh -eu -c 'pg_isready --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"' >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 1
done
if [[ "$ready" != true ]]; then
  echo "PostgreSQL did not become ready within 60 seconds." >&2
  exit 1
fi
docker compose stop api worker

cat "$work_dir/database.dump" | docker compose exec -T postgres sh -eu -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" pg_restore --clean --if-exists --no-owner --no-acl --exit-on-error --single-transaction --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"'

# Clear old artifacts before restoring the exact /data snapshot.
cat "$work_dir/data.tar.gz" | docker compose run --rm --no-deps -T --entrypoint sh api -eu -c \
  'find /data -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; tar -xzf - -C /data'

# A restored snapshot is never allowed to resume content changes automatically.
docker compose exec -T postgres sh -eu -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" psql --no-psqlrc --set=ON_ERROR_STOP=1 --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --command="INSERT INTO global_settings (id, recovery_mode, content_mutations_paused, revision) VALUES (1, TRUE, TRUE, 1) ON CONFLICT (id) DO UPDATE SET recovery_mode = TRUE, content_mutations_paused = TRUE, revision = global_settings.revision + 1"'

echo "Restore finished with recovery mode and content pause enabled."
echo "Review artifact integrity and remote blog state before manually clearing recovery mode."
echo "Start the dashboard for inspection with: docker compose up -d api"
