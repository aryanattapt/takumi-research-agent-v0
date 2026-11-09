# Peta layer Takumi Research Agent

Rujukan cepat saat menambah satu fitur: di mana kode diletakkan, konvensi yang sudah berlaku, dan anchor dokumen yang harus diperbarui. Semua path relatif terhadap root repo.

## Struktur yang sudah ada

| Layer | Path | Isi |
| --- | --- | --- |
| Domain/penjadwalan | `src/takumi_research_agent/orchestration/` | `scheduler.py` (slot harian, lease, `RunWorker`), `capacity.py` (admission control), `pipeline.py` (`DailyResearchProcessor`) |
| Antrean durabel | `src/takumi_research_agent/persistence/queue.py` | `claim_next_run`, `fenced_stage_update`, `renew_lease` |
| Model | `src/takumi_research_agent/persistence/models.py` | `Base`, `Run`, `BlogProfile`, `ProviderProfile`, `ContentLedger`, `DeliveryIntent`, `RunEvent`, `AuditEvent` |
| HTTP | `src/takumi_research_agent/api/app.py`, `api/schemas.py` | `create_app(settings, engine=None, sessions=None)`, semua route `/api/v1/...` |
| Dashboard | `web/src/App.tsx`, `web/src/types.ts`, `web/src/api.ts` | Satu file komponen besar + tipe + helper `api<T>()` |
| Delivery bergate | `src/takumi_research_agent/delivery/service.py` | Outbox `prepare`/`dispatch`/rekonsiliasi |
| Operasi | `src/takumi_research_agent/operations/backup.py` | `create_backup`, `verify_backup`, `restore_backup`, `artifact_integrity_report`, `last_restore_report` |
| Import legacy | `src/takumi_research_agent/operations/legacy_import.py` | `import_legacy_sqlite` (papers→sources, blog_posts→blog_history, publications→content_ledger, sisanya audit + artifact) |
| Migrasi | `migrations/versions/` | Alembic; head terakhir menentukan `down_revision` |
| Status proyek | `docs/planning/plan.md` §15.4 dan §16.2, `README.md` | Tabel milestone, urutan `IP-*`, tabel status fitur |

## Konvensi model dan audit

- Semua tabel memakai `String(36)` id dengan `default=uid` (UUID4 string), `DateTime(timezone=True)`, JSON untuk snapshot/metadata.
- `Run` memuat `status`, `stage`, `execution_mode` (`dryrun`/`sandbox`/`live`), `deadline_result`, `next_run_at`, `lease_owner`/`lease_until`/`lease_epoch`, `state_version`, `attempts`, `config_snapshot`, plus `schedule_version`, `capacity_state`, `start_by_at`.
- Bukti perubahan state ditulis ke `RunEvent(run_id=..., event_type="<domain>_<state>", details={...})`, bukan hanya kolom. Tulis event hanya saat nilai berubah agar tidak spam.
- Constraint gaya repo: `CheckConstraint("<kolom> in ('a', 'b')", name="ck_<tabel>_<kolom>")` dan `UniqueConstraint(...)` dengan nama eksplisit.

## Konvensi API

- Konfigurasi memakai `Settings.from_env()`; rahasia didekripsi lewat `SecretCipher(settings.master_key)`.
- Dependency auth: `Depends(get_auth)` untuk baca, `Depends(require_csrf)` untuk mutasi (header `X-CSRF-Token`).
- Error dikembalikan sebagai `HTTPException(status.HTTP_409_CONFLICT | 422 | 503, "<pesan operasional>")`.
- Response dibangun sebagai dict eksplisit (bukan serialisasi ORM), sehingga kolom baru harus ditambahkan manual di `list_runs`, `get_run_details`, dan endpoint terkait.
- Body request memakai `StrictRequest` (`extra="forbid"`) di `api/schemas.py`.
- Endpoint preview read-only lebih disukai daripada mutasi implisit; contoh: `GET /api/v1/capacity`.

## Konvensi test

- Nama file `tests/test_<domain>.py`, SQLite in-memory: `create_database("sqlite:///:memory:")` lalu `Base.metadata.create_all(engine)`.
- API test: `create_app(settings, engine=engine, sessions=sessions)` + `TestClient`, bootstrap admin `POST /api/v1/setup/admin`, login `POST /api/v1/auth/login` untuk mendapat `csrf_token`.
- Nilai env untuk test diatur lewat fixture `monkeypatch.setenv(...)` supaya tidak bergantung environment mesin.
- Datetime dari SQLite kembali naive; normalisasi sebelum dibandingkan dengan datetime ber-tz.

## Konvensi dashboard

- Tambah tipe di `web/src/types.ts`, panggil API dengan `api<T>(...)` (mutasi butuh argumen `csrf`), render in-place di `App.tsx`.
- Class pill yang tersedia di `web/src/style.css`: `status-green`, `status-gray`, `status-blue`, `status-amber`; untuk nada lain pakai utility Tailwind langsung (mis. `bg-rose-50 text-rose-700`).
- Setiap aksi remote wajib `window.confirm` dan hanya tampil pada mode non-`dryrun`.

## Modul capacity (contoh fitur penjadwalan yang sudah jadi)

`orchestration/capacity.py` mengekspor `plan_capacity`, `summarize`, `capacity_report`, `apply_admission_control`, serta konstanta `DEFAULT_CONCURRENCY`, `DEFAULT_RUN_MINUTES`, `MAX_CONCURRENCY`, `MIN_RUN_MINUTES`, `MAX_RUN_MINUTES`.

- `apply_admission_control(session, now=..., concurrency=..., run_minutes=...)` menulis `capacity_state`, `start_by_at`, `next_run_at` + `RunEvent capacity_*`; dipanggil `enqueue_due_slots` dan loop worker.
- `capacity_report(...)` read-only untuk `GET /api/v1/capacity`.
- Env yang dibaca worker dan API: `WORKER_CONCURRENCY`, `CAPACITY_RUN_MINUTES`.

## Modul backup/restore (contoh fitur operasi yang sudah jadi)

`operations/backup.py` menyimpan backup sebagai direktori `<DATA_DIR>/backups/<backup_id>/` berisi `database.json` (snapshot logis seluruh tabel `Base.metadata` dengan penanda tipe datetime/date/bytes), salinan `artifacts/<kind>/<digest>`, dan `manifest.json` ber-hash.

- `create_backup(sessions, artifacts_root=..., output_dir=...)` membaca semua tabel dalam satu transaksi dan menolak backup bila artifact evidence tidak ada atau hash-nya beda.
- `verify_backup(path)` memeriksa hash file, jumlah baris per tabel, dan schema revision; `restore_backup(session, ..., acknowledge=True)` menolak backup yang gagal verifikasi atau schema revision beda.
- Restore selalu menyalakan `GlobalSettings.recovery_mode` + `content_mutations_paused`, membatalkan lease run yang aktif (status `blocked`, stage `restore_recovery`, `lease_epoch += 1`), mengubah intent `dispatching` menjadi `unknown`, dan menulis `AuditEvent(action="system_restore")`.
- CLI: `takumi backup`, `takumi verify-backup <path>`, `takumi restore <path> --acknowledge`; endpoint `GET /api/v1/recovery?verify_artifacts=true`.
- Snapshot tabel memakai `Base.metadata.sorted_tables` untuk urutan insert dan kebalikannya untuk delete; `alembic_version` bukan model sehingga revision dibaca manual via `select version_num from alembic_version`.

## Modul import SQLite legacy

`operations/legacy_import.py` membaca `DATA_DIR/takumi.sqlite3` lama lewat URI read-only (`file:<path>?mode=ro`) dan menulis hanya ke satu blog profile yang nonaktif.

- Marker idempotensi ada di tabel `legacy_import_records`, unik pada (`source_fingerprint`, `table_name`, `record_key`); fingerprint stabil dari nama file, bukan hash isi, supaya re-run tidak menggandakan data.
- Dry run menulis di dalam savepoint (`session.begin_nested()`) lalu rollback, sehingga laporan tetap akurat tanpa mengubah data.
- Isi penuh paper dan bundle draft lama disimpan sebagai artifact kind `legacy`; jobs/drafts/agent_events menjadi `AuditEvent`, bukan run palsu.
- Legacy SQLite berjalan di WAL: file utama baru ter-checkpoint saat koneksi terakhir ditutup, jadi bandingkan isi logis (copy `-wal` + `-shm`), bukan byte file, saat menguji "file asli tidak berubah".
