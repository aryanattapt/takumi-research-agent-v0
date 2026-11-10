# Backup and restore

These scripts back up the PostgreSQL database and the shared `/data` Docker volume. The database dump is a PostgreSQL custom-format dump; `/data` contains immutable research artifacts. The archive includes SHA-256 checksums and metadata, but it does **not** include `.env`, API tokens in plaintext, or `TAKUMI_MASTER_KEY`.

## Backup

Start the Compose stack so PostgreSQL is available, then run from the repository root:

```sh
./scripts/backup.sh
```

The default destination is `./backups`. Set `BACKUP_DIR=/secure/path` to use another directory. The script creates a mode-restricted archive and prints its location. Copy backups to an encrypted off-host location and retain the matching master key separately. Without that key, encrypted provider and blog credentials cannot be decrypted after restore. Keep enough free disk space for the database dump, data archive, and final bundle while the backup is assembled. SHA-256 checksums detect accidental corruption; they do not authenticate who created an archive.

The PostgreSQL dump uses a consistent database snapshot. Artifacts are immutable; the archive can contain unreferenced artifacts written while the snapshot runs, which is safe. If artifact deletion/garbage collection is added later, pause the worker during backup or replace this with a coordinated snapshot procedure.

## Restore

Restore overwrites the database objects in the configured database and replaces the contents of the shared `/data` volume. Stop other deployments that use either volume first. Confirm that `.env` points to the intended Compose project, the matching `TAKUMI_MASTER_KEY` is available, and the host has enough temporary disk space. Set `RESTORE_TMPDIR=/path/with/space` if the archive directory does not have enough room.

```sh
./scripts/restore.sh /secure/path/takumi-YYYYMMDDTHHMMSSZ.tar.gz --confirm-overwrite
```

The script verifies the archive checksums and format before starting PostgreSQL or stopping the API/worker. It restores the database and artifact volume, then sets `recovery_mode=true` and `content_mutations_paused=true`. It does not re-enable scheduled content changes. If a restore step fails after the API/worker stop, leave them stopped until the database and artifact state are reviewed.

After restore:

1. Start only the dashboard/API: `docker compose up -d api`.
2. Confirm the dashboard reports recovery mode and the expected profiles and runs are present.
3. Check the matching master key by confirming configured credentials can be decrypted through the existing provider/blog probes.
4. Verify artifact integrity against referenced hashes and review run/source records.
5. Reconcile remote blog state for every possible in-flight operation. If a remote outcome cannot be established, keep the system paused and blocked.
6. Only after those checks, explicitly clear recovery mode while keeping the content pause enabled:

   ```sh
   docker compose exec -T postgres sh -eu -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql --no-psqlrc --set=ON_ERROR_STOP=1 --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --command="UPDATE global_settings SET recovery_mode = FALSE, revision = revision + 1 WHERE id = 1 AND content_mutations_paused = TRUE"'
   ```

   Confirm the dashboard shows recovery mode off and content still paused. Start the worker and enable profiles gradually only after the checks are recorded.

The current release has no remote publishing path, so there should be no Takumi-created remote posts to reconcile. A live publisher must add a durable reconciliation report and a controlled recovery-completion action before production delivery is enabled.

## Limits

- Run `./scripts/restore.sh` only when intentionally replacing the target database and artifact volume.
- The archive does not preserve Docker configuration, secrets, or a PostgreSQL server installation.
- Restoring the database and artifacts is only a storage restore; recovery of external blog state is a separate check.
- This environment did not have Docker available, so the scripts have not been exercised against a live Compose stack.
