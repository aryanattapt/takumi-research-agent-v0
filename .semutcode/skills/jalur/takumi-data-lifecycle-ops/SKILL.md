---
name: takumi-data-lifecycle-ops
description: Menambah operasi data-lifecycle di Takumi Research Agent (backup, verifikasi, restore, import data lama, GC artifact) sebagai modul operations + perintah CLI + endpoint/dashboard, dengan manifest ber-hash, recovery mode, dan marker idempotensi. Gunakan saat user minta "tambahkan backup/restore", "bikin importer data SQLite lama", "migrasi data lama", "tambahkan GC artifact", atau "lanjutkan sisa M1 operasional".
metadata:
  created-by: semut-jalur
---
# Operasi data-lifecycle Takumi

Pola untuk fitur yang memindahkan/menyalin/memulihkan data Takumi: baca sumber secara read-only, tulis dengan gate fail-closed, hasilkan laporan yang bisa diaudit, dan jangan pernah mengulang efek remote yang tidak diketahui. Tidak mencakup delivery/publish ke API blog (lihat `takumi-guarded-feature`) maupun pemilihan gap milestone secara umum (lihat `takumi-resume-milestone`).

## When to use

- User menulis "tambahkan backup/restore", "restore dari snapshot", "verifikasi backup".
- User minta "importer data SQLite lama", "migrasi data lama ke Postgres", "pindahkan data blog lama".
- User minta "GC artifact", "retensi bukti", "manifest artifact".
- Sisa milestone M1/M7 yang berbunyi backup-restore, migrasi data, atau recovery.

## Steps

1. **Tetapkan invariant dulu** dari `docs/planning/plan.md` §18.3 (artifact/backup/restore) dan §12.6 (migrasi SQLite). Ringkasnya ada di `references/design-invariants.md`.
2. **Siapkan gate eksekusi.** Semua perintah butuh env yang sama seperti worker:
   ```
   export PYTHONPATH=src
   export DATABASE_URL="sqlite:///./_tmp_ops.db"   # atau URL Postgres
   export DATA_DIR="./_tmp_ops_data"
   export TAKUMI_MASTER_KEY="<minimal 32 karakter>"
   python -m alembic upgrade head
   ```
3. **Buat modul baru** di `src/takumi_research_agent/operations/<nama>.py`. Satu modul = satu kelas operasi (`create_backup`, `restore_backup`, `import_legacy_sqlite`). Jangan taruh logika ini di `api/` atau `cli/`.
4. **Baca sumber eksternal secara read-only:**
   - SQLite: `sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)`.
   - Postgres/DB aktif: baca semua tabel dalam satu transaksi agar snapshot konsisten (`for table in Base.metadata.sorted_tables: session.execute(select(table))`).
5. **Tulis dengan gate berurutan, semuanya sebelum efek samping:** file/backup ada → backup lolos verifikasi hash → schema revision cocok → profil tujuan nonaktif (untuk import) → `acknowledge` eksplisit (untuk restore) → baru menulis.
6. **Dry-run pakai savepoint**, bukan rollback transaksi: `savepoint = session.begin_nested()` … `savepoint.rollback()`. Laporan tetap dihitung, database tidak berubah.
7. **Buat idempotensi dengan tabel marker** (mis. `legacy_import_records`) unik pada (`source_fingerprint`, `table_name`, `record_key`). Fingerprint dari **identitas** sumber (nama file/DSN), bukan hash isi, supaya re-run tidak menggandakan data saat isi sumber bertambah.
8. **Petakan hanya yang punya identitas.** Baris lama tanpa run/pair/blog tidak boleh dikarang: simpan isi penuh sebagai artifact (`ArtifactStore.put("legacy", ...)`) dan jejaknya sebagai `AuditEvent`. Yang punya identitas jelas dipetakan ke `sources`, `blog_history`, `content_ledger`.
9. **Tulis laporan + audit.** Kembalikan `dict` yang JSON-serialisable: `counts` per tabel (`imported`/`duplicates`/`skipped`), `skipped` beralasan, `verification` (jumlah baris + relasi + artifact terbaca), `artifacts_written`, `backup_id`. Simpan juga `AuditEvent(action="<domain>_report")` dan `AuditEvent(action="legacy_publication_reconciliation_required")` untuk baris yang menuntut rekonsiliasi.
10. **Restore wajib menormalkan keadaan:** set `GlobalSettings.recovery_mode=True` + `content_mutations_paused=True` + `revision += 1`, batalkan lease run aktif (`blocked`/`restore_recovery`, `lease_epoch += 1`), dan ubah intent `dispatching` menjadi `unknown` (jangan dikirim ulang). Tulis laporan yang sama ke `AuditEvent(action="system_restore")`.
11. **Ekspos lewat CLI** di `src/takumi_research_agent/cli.py`: subcommand baru, `print(json.dumps(report, indent=2))`, `return 1` bila `BackupBlocked`/`<Domain>Blocked`. Untuk import, ambil backup otomatis sebelum menulis.
    ```
    python -m takumi_research_agent.cli backup
    python -m takumi_research_agent.cli verify-backup <path>
    python -m takumi_research_agent.cli restore <path> --acknowledge
    python -m takumi_research_agent.cli import-sqlite <path> --blog-id <id> --dry-run
    ```
12. **Endpoint hanya read-only + mahal dihitung saat diminta:** contoh `GET /api/v1/recovery?verify_artifacts=true`; default polling mengembalikan `{"checked": false, ...}` agar dashboard tidak menghitung hash tiap 15 detik.
13. **Migrasi Alembic baru** untuk tabel marker (`op.create_table` aman di SQLite). Constraint tambahan pada tabel lama hanya dibuat untuk PostgreSQL.
14. **Verifikasi:** `bash .semutcode/skills/jalur/takumi-resume-milestone/scripts/verify-local.sh` lalu smoke test kelas ini: `python .semutcode/skills/jalur/takumi-data-lifecycle-ops/scripts/ops_smoke.py`.
15. **Dokumentasikan perilaku yang berubah**: README (tabel status + tabel perintah + section operasional + guard) dan `docs/planning/plan.md` §15.4, lalu commit/push dan cek `python .semutcode/skills/ci-failure-triage/scripts/ci_status.py`.

## Pitfalls

- Jangan `session.rollback()` di dalam blok `with sessions.begin()` untuk dry-run: SQLAlchemy melempar `InvalidRequestError: Can't operate on closed transaction inside context manager`; gunakan savepoint.
- Jangan membandingkan **byte** file SQLite lama untuk membuktikan "file asli tidak berubah": database mode WAL baru di-checkpoint saat koneksi terakhir ditutup, dan helper lama (`Store`) tidak pernah memanggil `close()`; bandingkan isi logis dari salinan `.sqlite3` + `-wal` + `-shm`.
- Pindahkan `import uvicorn` ke dalam cabang `api` di `cli.py`: kalau diimpor di level modul, perintah `backup`/`restore`/`import-sqlite` gagal dengan `ModuleNotFoundError` di environment yang tidak memasang server.
- Simpan master key hanya sebagai **nama referensi** di manifest (`manifest["key_reference"] == "TAKUMI_MASTER_KEY"`), bukan nilainya: menyalin nilainya ke backup membuat backup sama sensitifnya dengan kredensial.
- Restore harus menolak backup yang tidak lolos hash dan yang schema revision-nya beda: restore setengah valid lebih merusak daripada menolak restore.
- Intent `dispatching` saat restore **wajib** jadi `unknown`, bukan `prepared`: outcome POST yang sedang jalan tidak bisa direkonstruksi dari snapshot, jadi retry otomatis berisiko duplikat.
- Import legacy wajib menolak profil `active`: record publikasi lama harus direkonsiliasi sebelum jadwal boleh menulis lagi.
- Jangan mengarang run/pair untuk `jobs`, `drafts`, `agent_events` lama: tidak ada identitasnya; simpan sebagai audit + artifact.
- Hitung baris laporan dengan `select(func.count()).select_from(Model)`, bukan `select(text(...))`, agar tipenya konsisten di SQLite maupun PostgreSQL.
- Perintah CLI harus mencetak JSON dan memakai exit code (0 sukses, 1 blocked) supaya script/smoke test bisa diverifikasi tanpa mata manusia.
- Jalankan seeding data SQLite (lama maupun baru) di proses terpisah saat menyiapkan smoke test: `Store` lama tidak pernah menutup koneksi, dan di Windows handle yang masih terbuka membuat penghapusan direktori sementara gagal diam-diam.

## Verify

`python .semutcode/skills/jalur/takumi-data-lifecycle-ops/scripts/ops_smoke.py` mencetak `OK ops smoke`: backup → verify → import → re-import (duplikat) → restore ditolak tanpa `--acknowledge` → restore menyalakan recovery mode dan mengembalikan baris ke kondisi backup. Dijalankan pada SQLite sementara di dalam repo, jadi data asli tidak tersentuh.
