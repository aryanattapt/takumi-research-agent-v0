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
| Sinyal tren teknologi dan sains | Hacker News Algolia dan arXiv dengan skor heuristik yang transparan |
| Riwayat dan mapping JSON blog | GET read-only, OpenAPI operation list, JSONPath terbatas, preview dan pagination |
| Riset jurnal dan sumber primer | Crossref, Europe PMC, pemeriksaan DOAJ, pembaca HTTP dan Playwright |
| Evidence serta artikel bilingual | Pipeline ID/EN, critic pass, sanitasi, hash dan penyimpanan draft |
| Publish bila didukung jurnal; selain itu draft | Outbox delivery dan approval fingerprint tersedia; eksekusi remote tetap menunggu kontrak blog dan capability create/publish yang terbukti |
| Uji kapasitas dan antrean 10 blog | Simulasi antrean 10 profil dengan target sama tersedia sebagai test; benchmark wall-clock nyata dengan provider/model sungguhan belum dilakukan |
| Evaluasi kualitas model dan backup/restore | Belum selesai |

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
7. Jalankan sandbox run untuk menyiapkan intent delivery, lalu buka Run history dan konfirmasi Kirim ke blog. Dry-run tidak pernah mengirim request. Jika hasil sebuah intent `unknown`, gunakan tombol Sukses/Gagal untuk rekonsiliasi; keputusan itu tercatat sebagai event audit, bukan intent baru.
8. Buka Run history untuk melihat keputusan, hasil review, artikel Indonesia/English, dan status outbox delivery.

Remote OpenAPI URL harus memakai HTTPS dan host-nya dicantumkan di ADMIN_ENDPOINT_ALLOWLIST. URL OpenAPI tanpa allowlist gagal tertutup. RESEARCH_ALLOWED_DOMAINS membatasi host yang dapat dibaca tool riset.

### Kapasitas dan deadline

Lead time tidak menjamin semua profil selesai tepat waktu. Worker menghitung ulang antrean setiap loop dengan urutan earliest-deadline-first dan menulis hasilnya ke setiap run:

- `capacity_state=ok` berarti run muat sebelum target.
- `capacity_state=at_risk` berarti antrean melewati waktu mulai paling lambat (`start_by_at`); solusinya menambah lane, menaikkan lead time, atau mengurangi profil yang aktif pada jam yang sama.
- `capacity_state=infeasible` berarti target lebih dekat daripada satu run, sehingga deadline tidak mungkin dipenuhi; ini dilaporkan, bukan dikejar dengan retry.

`WORKER_CONCURRENCY` (1–16 lane) dan `CAPACITY_RUN_MINUTES` (estimasi wall-clock per run, default 45) dibaca oleh worker dan API, jadi setel keduanya dengan nilai yang sama. `GET /api/v1/capacity` menampilkan rencana antrean tanpa mengubah run, dan Run history menampilkan kolom Kapasitas beserta `start_by_at`.

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

## Guard dan batas integrasi

- Agent riset hanya mempunyai tool baca sumber, history blog, dan rekam evidence. Tool riset tidak dapat menerbitkan artikel.
- Delivery hanya berjalan dari outbox intent yang sudah tersimpan; approval fingerprint harus masih cocok dengan body, review, evidence/source, mapping, editorial settings, jadwal, dan destination.
- Dry-run tidak pernah mengirim request ke blog. Eksekusi sandbox/live tetap ditahan oleh pause, recovery mode, dan capability create/publish yang belum terbukti.
- Outcome `unknown` tidak di-retry otomatis; rekonsiliasi operator dicatat dengan audit trail dan tidak membuat payload/key baru.
- Admission control hanya menulis field jadwal lokal (`next_run_at`, `start_by_at`, `capacity_state`). Slot `at_risk` dan `infeasible` dilaporkan apa adanya; tidak ada pekerjaan tambahan yang dikirim ke provider untuk mengejar deadline.
- Mapping API adalah JSONPath subset, bukan kode executable.
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

Test menguji agent runtime, mapping/pagination blog, policy editorial, outbox delivery, quota/lease, dan API dashboard dengan SQLite/fixture. Tes live ke model atau API blog belum dilakukan.

## Struktur utama

| Path | Isi |
| --- | --- |
| src/takumi_research_agent/api | Auth, konfigurasi dashboard, probe, runs, dan read-only adapter validation |
| src/takumi_research_agent/agent | Tool loop bertipe dan batas request/action |
| src/takumi_research_agent/blogs | OpenAPI loader, JSONPath mapping, history reader, dan guarded create primitive |
| src/takumi_research_agent/discovery | Trend collector Hacker News/arXiv dan skor heuristik |
| src/takumi_research_agent/research | Retriever, browser fallback, artifacts, evidence, serta research tools |
| src/takumi_research_agent/editorial | Penulis bilingual, critic, sanitizer, dan final body |
| src/takumi_research_agent/orchestration | Jadwal slot, admission control kapasitas, worker lease, dan research-to-local-draft pipeline |
| src/takumi_research_agent/delivery | Outbox intent, approval fingerprint, dan dispatcher bertingkat capability |
| src/takumi_research_agent/persistence | SQLAlchemy models, database, dan fenced queue |
| migrations | Schema versi Alembic |
| web | React/TypeScript dashboard |
| docs/planning | Plan, deep review, dan traceability requirement |

## Atribusi

Pemilik produk: Arya. Penyusun implementasi dan review kode: Codex.
