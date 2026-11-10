#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

backup_dir="${BACKUP_DIR:-$repo_root/backups}"
mkdir -p -- "$backup_dir"
umask 077

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
work_dir="$(mktemp -d "$backup_dir/.takumi-backup.XXXXXXXX")"
archive="$backup_dir/takumi-$timestamp-$$.tar.gz"
archive_tmp="$archive.tmp"
cleanup() { rm -rf -- "$work_dir"; rm -f -- "$archive_tmp"; }
trap cleanup EXIT

if ! docker compose ps --status running --services | grep -Fxq 'postgres'; then
  echo "PostgreSQL service is not running; start the Compose stack first." >&2
  exit 1
fi

docker compose exec -T postgres sh -eu -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump --format=custom --no-owner --no-acl --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"' \
  > "$work_dir/database.dump"

# Artifacts are immutable and are only removed by an explicit retention/GC process.
# A volume helper container lets this work even when the API itself is stopped.
docker compose run --rm --no-deps -T --entrypoint sh api -eu -c \
  'mkdir -p /data && tar -czf - -C /data .' \
  > "$work_dir/data.tar.gz"

git_commit="$(git rev-parse HEAD 2>/dev/null || printf 'unknown')"
schema_version="$(docker compose exec -T postgres sh -eu -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" psql --no-psqlrc --tuples-only --no-align --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --command="SELECT version_num FROM alembic_version LIMIT 1"' \
  | tr -d '\r' | sed '/^[[:space:]]*$/d' | head -n 1 || true)"
schema_version="${schema_version:-unknown}"

cat > "$work_dir/metadata.json" <<EOF
{
  "format": "takumi-backup-v1",
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "repository_commit": "$git_commit",
  "database_dump": "PostgreSQL custom format",
  "schema_version": "$schema_version",
  "data_archive": "Docker /data volume contents",
  "master_key_included": false
}
EOF

(cd "$work_dir" && sha256sum database.dump data.tar.gz metadata.json > SHA256SUMS)
tar -czf "$archive_tmp" -C "$work_dir" database.dump data.tar.gz metadata.json SHA256SUMS
chmod 600 -- "$archive_tmp"
mv -- "$archive_tmp" "$archive"

echo "Backup created: $archive"
echo "Keep the matching TAKUMI_MASTER_KEY separately; it is not part of this archive."
