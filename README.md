# Takumi Research Agent

Takumi membantu menyiapkan artikel sains dan teknologi dalam Bahasa Indonesia dan English. Sistem menggabungkan sinyal topik harian, pemeriksaan riwayat artikel blog, riset sumber primer, pencatatan kutipan yang dapat diverifikasi, penulisan, dan review editorial.

## Status implementasi

Repository sudah memiliki fondasi agent, dashboard, migrasi database, worker terjadwal, konektor tren publik, adapter baca blog berbasis OpenAPI, dan pipeline yang menyimpan pasangan artikel sebagai draft lokal.

**Pengiriman artikel ke API blog belum diaktifkan.** Kontrak API blog nyata belum diberikan, dan capability create/publish belum diuji di sandbox. Saat ini tidak ada artikel yang dibuat atau diterbitkan pada blog. Draft tersimpan di database lokal dan bisa dilihat dari Run history.

| Kebutuhan | Status saat ini |
| --- | --- |
| Maksimal 10 blog dan dashboard konfigurasi | Tersedia |
| Provider OpenAI-compatible dan Anthropic | Adapter tersedia; kredensial/model perlu dikonfigurasi dan di-probe |
| Jadwal harian default pukul 06:00 Asia/Jakarta | Worker dan slot harian tersedia; mulai menyiapkan 120 menit sebelumnya, dan admission control menandai slot `at_risk`/`infeasible` bila antrean tidak muat sebelum target |
| Sinyal tren teknologi dan sains | Hacker News Algolia, arXiv, dan RSS publik terkonfigurasi dengan skor heuristik yang transparan |
| Riwayat dan mapping JSON blog | GET read-only, OpenAPI operation list, JSONPath terbatas, preview dan pagination |
| Riset jurnal dan sumber primer | Crossref, Europe PMC, pemeriksaan DOAJ, pembaca HTTP dan Playwright |
| Evidence serta artikel bilingual | Pipeline ID/EN, critic pass, sanitasi, hash dan penyimpanan draft |
| Run detail | Klaim terhubung ke kutipan dan sumber; trace agent dan evidence ditampilkan secara terstruktur |
| Publish bila didukung jurnal; selain itu draft | Outbox delivery dan approval fingerprint tersedia; eksekusi remote tetap menunggu kontrak blog dan capability create/publish yang terbukti |
| Uji kapasitas dan antrean 10 blog | Simulasi antrean 10 profil dengan target sama tersedia sebagai test; benchmark wall-clock nyata dengan provider/model sungguhan belum dilakukan |
| Backup, verifikasi, dan restore | Tersedia lewat CLI: snapshot database + manifest artifact ber-hash, restore wajib `--acknowledge` dan selalu menyalakan recovery mode |
| Import data SQLite legacy | Tersedia lewat CLI `takumi import-sqlite`: satu file lama dipetakan ke satu blog profile yang nonaktif, idempoten, dan menyimpan laporan migrasi |
| Evaluasi kualitas model | Baseline runtime deterministik v1 tersedia di CI; qualification mutu riset per model belum selesai |

Headless browser lokal menghindari langganan search API berbayar untuk pengambilan halaman publik. Layanan model, bandwidth, mesin, dan hosting tetap dapat menimbulkan biaya.

## Jalankan dengan Docker Compose

1. Salin .env.example menjadi .env. Ganti POSTGRES_PASSWORD dan TAKUMI_MASTER_KEY dengan nilai acak sendiri. Gunakan password database alfanumerik agar URL database tidak perlu di-escape.
2. Periksa DATABASE_URL, SESSION_COOKIE_SECURE, timezone, dan allowlist sumber. Untuk akses lokal melalui HTTP gunakan SESSION_COOKIE_SECURE=false; di balik HTTPS gunakan true.
3. Jalankan:

       docker compose up --build -d

4. Buka http://localhost:8000, buat akun administrator, lalu konfigurasi provider dan blog.

Compose menjalankan PostgreSQL, migrasi Alembic, API/dashboard, dan worker. Data database serta artifact sumber berada pada Docker volumes. Backup dan restore produksi belum dilengkapi runbook otomatis.

### Konfigurasi pertama

1. Tambahkan endpoint model OpenAI-compatible atau Anthropic. Probe mengirim satu request model dan dapat menggunakan token/biaya provider.
2. Tambahkan blog profile, base URL, kredensial, OpenAPI spec, provider, timezone, waktu harian (default 06:00), lead time, dan cakupan topik.
3. Baca operasi OpenAPI. Masukkan mapping untuk respons list, coba preview terhadap sample JSON, pilih operasi GET, lalu jalankan validasi read-only.
4. Aktifkan profil setelah riwayat blog tervalidasi. Aktivasi belum mengizinkan mutasi atau publish.
5. Gunakan tombol Jalankan dry-run untuk memulai riset lebih awal. Tombol ini mengakses provider model dan sumber eksternal, tetapi tidak menulis ke API blog.
6. Untuk mengaktifkan delivery, pilih operasi POST/PUT create, lalu jalankan Uji sandbox create. Uji ini mengirim **satu draft nyata** ke blog dan harus dipakai pada endpoint sandbox; hasilnya membuktikan create tunggal, bukan publish, idempotency, atau lookup.
7. Jalankan sandbox run untuk menyiapkan intent delivery, lalu buka Run history dan konfirmasi Kirim ke blog. Dry-run tidak pernah mengirim request. Jika intent `unknown` atau `dispatching` lebih dari dua menit, periksa riwayat blog sebelum memilih Sukses/Gagal; menandai gagal akan membuka intent untuk retry dan bisa membuat duplikat bila post sebenarnya sudah ada.
8. Buka Run history untuk melihat keputusan, hasil review, artikel Indonesia/English, dan status outbox delivery.

Remote OpenAPI URL harus memakai HTTPS dan host-nya dicantumkan di ADMIN_ENDPOINT_ALLOWLIST. URL OpenAPI tanpa allowlist gagal tertutup. RESEARCH_ALLOWED_DOMAINS membatasi host yang dapat dibaca tool riset. Feed RSS opsional dikonfigurasi secara global lewat `TREND_RSS_FEEDS` sebagai JSON array berisi `name`, `url`, dan `field` (`technology`, `physics`, `chemistry`, atau `biology`); setiap host feed harus ditambahkan ke `RESEARCH_ALLOWED_DOMAINS`. Hanya HTTPS yang diterima. Feed diambil dengan batas 2 MB, cache kondisional ETag/Last-Modified, dan kegagalannya terlihat pada Coverage discovery di detail Run. Pengaturan feed terpisah per profil blog belum tersedia.

### Kapasitas dan deadline

Lead time tidak menjamin semua profil selesai tepat waktu. Worker menghitung ulang antrean setiap loop dengan urutan earliest-deadline-first dan menulis hasilnya ke setiap run:

- `capacity_state=ok` berarti run muat sebelum target.
- `capacity_state=at_risk` berarti antrean melewati waktu mulai paling lambat (`start_by_at`); solusinya menambah lane, menaikkan lead time, atau mengurangi profil yang aktif pada jam yang sama.
- `capacity_state=infeasible` berarti target lebih dekat daripada satu run, sehingga deadline tidak mungkin dipenuhi; ini dilaporkan, bukan dikejar dengan retry.

`WORKER_CONCURRENCY` (1–16 lane) dan `CAPACITY_RUN_MINUTES` (estimasi wall-clock per run, default 45) dibaca oleh worker dan API, jadi setel keduanya dengan nilai yang sama. `GET /api/v1/capacity` menampilkan rencana antrean tanpa mengubah run, dan Run history menampilkan kolom Kapasitas beserta `start_by_at`.

## Backup dan recovery

Backup dijalankan dari CLI pada deployment yang berjalan:

1. `takumi backup` menulis satu direktori di `DATA_DIR/backups/<backup_id>/` berisi snapshot logis database (`database.json`), salinan setiap artifact evidence (`artifacts/<kind>/<digest>`), dan `manifest.json` dengan SHA-256 tiap file serta schema revision Alembic. Master key hanya dirujuk namanya (`TAKUMI_MASTER_KEY`) dan tidak pernah disimpan.
2. `takumi verify-backup <path>` memeriksa ulang hash setiap file, jumlah baris per tabel, dan kecocokan schema revision. Backup yang tidak lolos tidak boleh dipakai.
3. `takumi restore <path> --acknowledge` menolak backup yang gagal verifikasi maupun schema revision yang berbeda. Setelah data diganti, deployment dipaksa ke recovery mode (`recovery_mode=true`, `content_mutations_paused=true`, revision kontrol naik).

Selama recovery mode, lease lama dibatalkan, run yang sedang berjalan menjadi `blocked` dengan stage `restore_recovery`, dan intent yang masih `dispatching` ditandai `unknown` tanpa dikirim ulang. Operator masih harus:

1. Memulihkan `TAKUMI_MASTER_KEY` dari tempat terpisah, lalu memastikan kredensial provider dan blog bisa didekripsi (`providers_requiring_recredential`, `blogs_requiring_recredential`).
2. Menyinkronkan ulang history blog dan jadwal remote profil pada `schedule_cancel_or_resync_review`.
3. Mereview intent `unknown` dan profil pada `profiles_requiring_revalidation`.
4. Baru setelah itu menonaktifkan recovery mode lewat prosedur terkontrol. Slot yang terlewat tidak di-backfill otomatis.

Laporan restore terakhir tersedia di `GET /api/v1/recovery` dan panel "Backup dan recovery" pada halaman Kontrol sistem.

### Import data SQLite legacy

Deployment lama menyimpan satu blog di `DATA_DIR/takumi.sqlite3` (papers, blog_posts, drafts, jobs, publications, agent_events). Import dijalankan sekali per file:

1. Nonaktifkan blog profile tujuan. Import menolak profil yang masih `active`, karena record publikasi lama harus direkonsiliasi sebelum jadwal boleh menulis lagi.
2. Uji dengan `takumi import-sqlite <path> --blog-id <id> --dry-run`. Perintah ini hanya membaca file lama dan melaporkan hitungan per tabel.
3. Jalankan tanpa `--dry-run`. CLI membuat backup database aktif dulu, lalu memetakan papers ke `sources` (isi penuh disimpan sebagai artifact `legacy/`), blog_posts ke `blog_history`, publications ke `content_ledger` sebagai `legacy_publication` dengan audit `legacy_publication_reconciliation_required`, serta jobs/drafts/agent_events menjadi audit event dan artifact.
4. Ulangi perintah yang sama bila perlu: setiap record legacy ditandai di `legacy_import_records`, sehingga record yang sudah masuk dilaporkan sebagai `duplicates` dan tidak digandakan.
5. Rekonsiliasi `content_ledger` legacy sebelum mengaktifkan profil. Laporan migrasi tersimpan sebagai `audit_events` bertipe `legacy_import_report`.

File lama dibuka read-only dan tidak diubah. Pemetaan historis hanya masuk ke satu blog yang dipilih, tidak disalin ke semua profil.

## Jalankan lokal untuk development

Persyaratan: Python 3.11+, Node.js 22+, PostgreSQL, dan Chromium Playwright jika headless browser digunakan.

    python -m venv .venv
    source .venv/bin/activate
    python -m pip install -e '.[dev,browser,pdf]'
    python -m playwright install chromium
    npm --prefix web ci
    npm --prefix web run build
    takumi migrate
    takumi api

Pada terminal kedua jalankan takumi worker. API dan worker membaca konfigurasi dari .env. Untuk development, Vite dapat dijalankan melalui npm --prefix web run dev.

Perintah layanan:

| Perintah | Fungsi |
| --- | --- |
| takumi migrate | Terapkan migrasi database. |
| takumi api | Jalankan FastAPI dan dashboard production build. |
| takumi worker | Jalankan coordinator jadwal dan worker pipeline. |
| takumi backup | Snapshot konsisten database + salinan artifact yang dirujuk, plus manifest ber-hash. |
| takumi verify-backup &lt;path&gt; | Verifikasi ulang hash semua file backup tanpa menyentuh database. |
| takumi restore &lt;path&gt; --acknowledge | Ganti data lokal dengan backup terverifikasi, lalu masuk recovery mode. |
| takumi import-sqlite &lt;path&gt; --blog-id &lt;id&gt; | Import `takumi.sqlite3` lama ke satu blog profile (tambah `--dry-run` untuk laporan tanpa menulis). |

## Guard dan batas integrasi

- Agent riset hanya mempunyai tool baca sumber, history blog, dan rekam evidence. Tool riset tidak dapat menerbitkan artikel.
- Delivery hanya berjalan dari outbox intent yang sudah tersimpan; approval fingerprint harus masih cocok dengan body, review, evidence/source, mapping, editorial settings, jadwal, dan destination.
- Dry-run tidak pernah mengirim request ke blog. Eksekusi sandbox/live tetap ditahan oleh pause, recovery mode, dan capability create/publish yang belum terbukti.
- Outcome `unknown` tidak di-retry otomatis; rekonsiliasi operator dicatat dengan audit trail dan tidak membuat payload/key baru.
- Admission control hanya menulis field jadwal lokal (`next_run_at`, `start_by_at`, `capacity_state`). Slot `at_risk` dan `infeasible` dilaporkan apa adanya; tidak ada pekerjaan tambahan yang dikirim ke provider untuk mengejar deadline.
- Mapping API adalah JSONPath subset, bukan kode executable.
- Backup memuat manifest ber-hash untuk database dan setiap artifact yang dirujuk; restore menolak backup yang tidak lolos verifikasi dan tidak pernah menyalin master key.
- Setelah restore, deployment selalu berada di recovery mode: lease lama dibatalkan, intent yang masih `dispatching` menjadi `unknown` dan tidak dikirim ulang otomatis, dan profil tetap nonaktif sampai laporan recovery diselesaikan operator.
- Import SQLite legacy membuka file lama read-only, hanya menulis ke satu blog profile yang nonaktif, dan tidak pernah mengaktifkan jadwal atau memindahkan kredensial.
- History pagination yang berulang, tidak maju, atau tidak lengkap memblokir keputusan novelty.
- Source quote harus cocok dengan artifact content-addressed sebelum dipakai sebagai evidence.
- DOI, indeks jurnal, atau peer-review policy tingkat jurnal bukan bukti tunggal bahwa semua klaim paper benar.
- Artikel tanpa dukungan jurnal yang memenuhi policy diarahkan sebagai draft; klaim inti yang tidak didukung diblokir.
- Run harian melewatkan slot yang terlambat dan tidak melakukan catch-up massal.
- Pipeline saat ini selalu berjalan tanpa mutasi remote; dashboard tidak menampilkannya sebagai published.

## Verifikasi yang tersedia

    python -m pytest
    python -m compileall -q src
    npm --prefix web run build
    DATABASE_URL=sqlite:////tmp/takumi-migration-check.db python -m alembic upgrade head

Test menguji agent runtime termasuk gold cases v1 yang memastikan observation mengubah aksi berikutnya, mapping/pagination blog, RSS parser/cache/coverage dengan MockTransport, policy editorial, fixture trend-to-article, replay worker setelah crash tanpa duplikasi, kill proses saat request blog tertahan, restore ke status `unknown`, rekonsiliasi operator tanpa repost, outbox delivery, quota/lease, detail run yang menghubungkan claim/evidence/source dan coverage discovery, serta uji capability create draft dengan MockTransport dan pause/recovery guards. Gold cases ini menguji kontrak runtime deterministik, bukan mutu jawaban model; capability dan feed test memakai server palsu. Tes live ke model atau API blog belum dilakukan.

## Struktur utama

| Path | Isi |
| --- | --- |
| src/takumi_research_agent/api | Auth, konfigurasi dashboard, probe, runs, dan read-only adapter validation |
| src/takumi_research_agent/agent | Tool loop bertipe dan batas request/action |
| src/takumi_research_agent/blogs | OpenAPI loader, JSONPath mapping, history reader, dan guarded create primitive |
| src/takumi_research_agent/discovery | Trend collector Hacker News/arXiv/RSS, coverage source, dan skor heuristik |
| src/takumi_research_agent/research | Retriever, browser fallback, artifacts, evidence, serta research tools |
| src/takumi_research_agent/editorial | Penulis bilingual, critic, sanitizer, dan final body |
| src/takumi_research_agent/orchestration | Jadwal slot, admission control kapasitas, worker lease, dan research-to-local-draft pipeline |
| src/takumi_research_agent/delivery | Outbox intent, approval fingerprint, dan dispatcher bertingkat capability |
| src/takumi_research_agent/operations | Backup, verifikasi manifest, restore, import SQLite legacy, dan laporan recovery |
| src/takumi_research_agent/persistence | SQLAlchemy models, database, dan fenced queue |
| migrations | Schema versi Alembic |
| web | React/TypeScript dashboard |
| docs/planning | Plan, deep review, dan traceability requirement |

## Atribusi

Pemilik produk: Arya. Penyusun implementasi dan review kode: Codex.
