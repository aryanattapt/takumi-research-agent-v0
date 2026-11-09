---
name: takumi-resume-milestone
description: Melanjutkan pekerjaan Takumi Research Agent dengan memilih gap milestone berikutnya dari docs/planning/plan.md, mengimplementasikannya pada layer yang sudah ada, lalu memverifikasi lokal, commit, push, dan memastikan CI hijau. Gunakan saat user menulis "lanjutkan progress", "lanjutkan milestone", "kerjakan sisa M6", "apa gap berikutnya", atau "implementasikan milestone berikutnya".
metadata:
  created-by: semut-jalur
---
# Lanjut milestone Takumi

Menutup satu gap milestone Takumi dari dokumen plan sampai commit yang CI-nya hijau. Tidak mencakup fitur delivery/publish bergate (pakai skill `takumi-guarded-feature`), dan tidak mencakup live UAT yang butuh kontrak API blog atau limit provider nyata.

## When to use

- User menulis "lanjutkan progress" atau "lanjutkan" tanpa menyebut fitur spesifik.
- User menyebut milestone atau area, misalnya "kerjakan M6", "sisa deadline dan kapasitas", "backup/restore belum ada".
- User minta "implementasikan status yang masih sebagian di plan".
- Bukan untuk: mengubah pipeline riset/editorial inti, atau mengaktifkan penulisan remote ke blog.
- Bila gap yang dipilih berupa operasi data (backup/restore/import/GC), ikuti pola di `../takumi-data-lifecycle-ops/SKILL.md` dan jalankan smoke test-nya di langkah verifikasi.

## Steps

1. **Perbaiki akses git lebih dulu.** Repo ini di drive exFAT sehingga git menolak dengan "dubious ownership". Semua perintah git butuh flag per-perintah, jangan mengubah config global:
   ```
   git -c safe.directory="D:/RnD Projects/takumi-research-agent-v0" status --short
   git -c safe.directory="D:/RnD Projects/takumi-research-agent-v0" log --oneline -5
   ```
2. **Pilih gap dari dokumen, bukan dari tebakan.** Baca kolom "Sisa utama sebelum kriteria keluar" di `docs/planning/plan.md` §15.4 (tabel status milestone) dan urutan `IP-*` di §16.2. Ambil satu gap yang bisa ditutup tanpa dependency eksternal; kontrak/sandbox API blog, limit provider nyata, dan live UAT adalah blocker yang harus dilewati.
3. **Pastikan gap itu belum ada.** `rg -n "<istilah domain>" src tests migrations/versions` dan `ls migrations/versions`. Kalau sudah ada sebagian, lanjutkan yang ada—jangan bikin modul paralel.
4. **Baca layer yang akan disentuh sebelum mendesain:** `persistence/models.py`, `persistence/queue.py`, `orchestration/scheduler.py`, `api/app.py`, `web/src/types.ts`, `web/src/App.tsx`, dan test domain terdekat di `tests/`. Peta layer dan konvensinya ada di `references/layer-map.md`.
5. **Implementasi urut dari dalam ke luar:** modul domain baru (`src/takumi_research_agent/<area>/<fitur>.py`) → kolom model di `persistence/models.py` → migrasi Alembic baru di `migrations/versions/` (`down_revision` = revisi head terakhir, jangan edit migrasi lama) → wiring scheduler/CLI → endpoint API → dashboard → test → dokumentasi.
6. **Jaga kepemilikan field.** Scheduler/admin hanya boleh menulis field penjadwalan miliknya (`next_run_at`, `start_by_at`, status kapasitas). Field fencing (`state_version`, `lease_owner`, `lease_epoch`) hanya ditulis oleh claim/heartbeat/commit worker; menulisnya dari scheduler membuat commit worker gagal fencing dan run diulang.
7. **Tulis test dengan SQLite in-memory dan objek palsu**, bukan jaringan:
   ```
   engine, sessions = create_database("sqlite:///:memory:")
   Base.metadata.create_all(engine)
   ```
   Kalau perilakunya menyentuh deadline/kapasitas, sertakan skenario berskala nyata (mis. 10 profil target sama) dan laporkan angka `ok`/`at_risk`/`infeasible` apa adanya.
8. **Verifikasi lokal:** `bash .semutcode/skills/jalur/takumi-resume-milestone/scripts/verify-local.sh` (compile, pytest, alembic upgrade→downgrade→upgrade di sqlite, dashboard build). Untuk fitur operasi data, tambahkan `python .semutcode/skills/jalur/takumi-data-lifecycle-ops/scripts/ops_smoke.py`.
9. **Perbarui status hanya untuk perilaku yang benar-benar berubah:** baris milestone di `docs/planning/plan.md` §15.4 dan tabel status di `README.md`. Jangan menandai selesai sesuatu yang masih bergantung dependency eksternal.
10. **Commit dengan Conventional Commits** (`feat:`, `fix:`, `test:`, `docs:` sesuai isi), satu paket fitur + test + docs:
    ```
    git -c safe.directory="D:/RnD Projects/takumi-research-agent-v0" add -A
    git -c safe.directory="D:/RnD Projects/takumi-research-agent-v0" commit -m "feat: <ringkasan perilaku>"
    git -c safe.directory="D:/RnD Projects/takumi-research-agent-v0" push origin HEAD
    ```
11. **Tunggu CI hijau** dengan script read-only (tanpa `gh`): `python .semutcode/skills/ci-failure-triage/scripts/ci_status.py`. Kalau gagal, pakai skill `ci-failure-triage`.

## Pitfalls

- Jalankan pytest dengan `PYTHONPATH=src`: paket tidak di-install di interpreter lokal, tanpa itu semua modul test error `ModuleNotFoundError: takumi_research_agent`.
- Cek `tzdata` sebelum menyalahkan kode: Windows tidak punya database tz IANA, sehingga `ZoneInfo("Asia/Jakarta")` melempar `ZoneInfoNotFoundError` dan test jadwal gagal seolah-olah bug validasi.
- Jangan memakai `npm test` sebagai gate dashboard: tidak ada file vitest sehingga vitest keluar dengan kode 1; gate-nya `npm run build` yang sudah mencakup `tsc -b`.
- Jalankan `npm --prefix web ci` bila `web/node_modules` belum ada; warning npm tentang install script `esbuild` boleh diabaikan selama `npm run build` lulus.
- Uji migrasi di sqlite, bukan hanya PostgreSQL: CI menjalankan `alembic upgrade head` dengan `DATABASE_URL=sqlite://…`, jadi migrasi yang hanya valid di PostgreSQL memerahkan pipeline.
- Jangan menambah check constraint lewat `op.create_check_constraint` tanpa cek dialek: SQLite tidak mendukung ALTER TABLE ADD CONSTRAINT, jadi guard dengan `op.get_bind().dialect.name == "postgresql"` dan biarkan model tetap mendeklarasikan constraint untuk database baru.
- Jangan menjalankan `alembic` pada path `/tmp` dari Git Bash: Python Windows tidak bisa membuka path itu; pakai file sementara di dalam repo lalu hapus.
- Jangan menyimpulkan deadline terpenuhi hanya karena run berhasil dibuat: bandingkan kapasitas (lane × menit tersedia) dengan beban (jumlah run × estimasi) dan tampilkan `at_risk`/`infeasible` sebagai bukti.

## Verify

`bash .semutcode/skills/jalur/takumi-resume-milestone/scripts/verify-local.sh` keluar dengan `== OK`, lalu `python .semutcode/skills/ci-failure-triage/scripts/ci_status.py` menampilkan `completed success` untuk commit terakhir.
