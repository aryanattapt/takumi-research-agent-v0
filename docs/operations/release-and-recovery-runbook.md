# Release, evaluation, backup, and recovery runbook

This runbook describes the repository's local deployment and data recovery steps. Completing it on one environment does not qualify a model or prove a production restore; record the environment, code revision, operator, and outcome for each rehearsal.

## 1. Pre-release checks

- Confirm the deployment target has persistent PostgreSQL and the Takumi data volume.
- Set fresh POSTGRES_PASSWORD and TAKUMI_MASTER_KEY values in the deployment secret store. Keep a recoverable copy of TAKUMI_MASTER_KEY separately from the database and backup volume.
- Review DATABASE_URL, SESSION_COOKIE_SECURE, TZ, RESEARCH_ALLOWED_DOMAINS, ADMIN_ENDPOINT_ALLOWLIST, WORKER_CONCURRENCY, CAPACITY_RUN_MINUTES, and PROVIDER_REQUESTS_PER_MINUTE. Set the last value to the provider's confirmed RPM limit; zero disables the shared gate.
- Review the CI result for backend tests, Python compilation, migration upgrade, and dashboard build.
- Configure the shared `PROVIDER_REQUESTS_PER_MINUTE` gate in both API and worker services using a limit confirmed for the provider credential. Zero disables the gate.
- If enabling semantic history retrieval, configure `HISTORY_EMBEDDING_MODEL` and `HISTORY_EMBEDDING_API_KEY` in the deployment secret store, then confirm Indonesian-English support and privacy approval before the first history index build.
- Configure and probe the selected model provider in the dashboard. Record model name, endpoint, rate limits, request timeout, and the operator who supplied the configuration.
- Use the evaluation workflow in docs/evaluation/README.md before treating research/editorial quality as qualified. Keep run artifacts and review output in a restricted location.

## 2. Deploy and inspect

From the release checkout:

    docker compose up --build -d
    docker compose ps
    docker compose logs --tail=200 api worker migrate

The API container readiness check is available at /api/health/ready. Confirm the migration service completed, API and worker are healthy, and the dashboard reports the expected configuration. Do not interpret a healthy process as proof of model quality or external integration readiness.

## 3. Create and verify a backup

Run from the API container:

    docker compose exec api takumi backup

The command reports a backup path under /data/backups. Verify that exact path:

    docker compose exec api takumi verify-backup /data/backups/BACKUP_ID

Copy the completed backup directory to the deployment's approved backup destination. Verify the copied manifest there as well. Retain the master key separately; it is intentionally not embedded in backup data.

## 4. Restore rehearsal

Perform this rehearsal on an isolated deployment with a disposable database and data volume. Restore replaces application data and enters recovery mode.

1. Record the rehearsal environment and take a fresh backup of its current state.
2. Copy a verified backup into the isolated deployment's data volume.
3. Run verify-backup against the copied directory.
4. Restore with explicit acknowledgement:

    docker compose exec api takumi restore /data/backups/BACKUP_ID --acknowledge

5. Restore the matching TAKUMI_MASTER_KEY from the independent secret store. Confirm configured provider and blog credentials decrypt; re-enter credentials if the recovery report identifies them as unavailable.
6. Review the recovery report in the dashboard or GET /api/v1/recovery. Revalidate profile destination identities, artifact hashes, history snapshots, schedules, and any unknown delivery intents.
7. Keep content mutations paused until each recovery item is reconciled. Never resolve an unknown remote outcome by automatically repeating the operation.
8. Confirm the restored dashboard data and evidence are readable. Record the result, schema revision, backup ID, missing items, and elapsed time.
9. Discard the isolated rehearsal environment after the report is saved.

Do not rehearse restore against the live database. A successful backup verification and a successful restore rehearsal are separate outcomes and should be recorded separately.

## 5. Capacity and operational review

- Start with one active profile, then increase concurrency only after measuring run duration and provider usage.
- Record per-run physical model requests, tool calls, input/output tokens, source failures, duration, and capacity state.
- Compare the measured wall-clock duration with CAPACITY_RUN_MINUTES and adjust admission control inputs consistently in API and worker environments.
- Record rate limits and any provider 429 responses. The code-level simulation does not substitute for measuring the selected provider's actual limits.
- Rehearse backup and restore after schema changes and before a broader deployment rollout.

## Current gaps

The repository contains the backup/verify/restore commands and recovery report, but a production restore rehearsal has not been recorded. Research/model qualification also remains incomplete until human-labeled benchmark data, provider runs, and independent reviewer labels are supplied.
