# Takumi Research Agent

Takumi membantu menyiapkan artikel sains dan teknologi dalam Bahasa Indonesia dan English. Sistem menggabungkan sinyal topik harian, pemeriksaan riwayat artikel blog, riset sumber primer, pencatatan kutipan yang dapat diverifikasi, penulisan, dan review editorial.

## Status implementasi

Repository sudah memiliki fondasi agent, dashboard, migrasi database, worker terjadwal, konektor tren publik, adapter baca blog berbasis OpenAPI, dan pipeline yang menyimpan pasangan artikel sebagai draft lokal.

**Pengiriman artikel ke API blog belum diaktifkan.** Kontrak API blog nyata belum diberikan, dan capability create/publish belum diuji di sandbox. Saat ini tidak ada artikel yang dibuat atau diterbitkan pada blog. Draft tersimpan di database lokal dan bisa dilihat dari Run history.

| Kebutuhan | Status saat ini |
| --- | --- |
| Maksimal 10 blog dan dashboard konfigurasi | Tersedia |
| Provider OpenAI-compatible dan Anthropic | Adapter tersedia; kredensial/model perlu dikonfigurasi dan di-probe |
| Jadwal harian default pukul 06:00 Asia/Jakarta | Worker dan slot harian tersedia; mulai menyiapkan 120 menit sebelumnya |
| Sinyal tren teknologi dan sains | Hacker News Algolia dan arXiv dengan skor heuristik yang transparan |
| Riwayat dan mapping JSON blog | GET read-only, OpenAPI operation list, JSONPath terbatas, preview dan pagination |
| Riset jurnal dan sumber primer | Crossref, Europe PMC, pemeriksaan DOAJ, pembaca HTTP dan Playwright |
| Evidence serta artikel bilingual | Pipeline ID/EN, critic pass, sanitasi, hash dan penyimpanan draft |
| Publish bila didukung jurnal; selain itu draft | Kebijakan keputusan tersedia; remote draft/publish menunggu kontrak blog dan capability test |
| Evaluasi kualitas model, benchmark 10 blog, backup/restore | Belum selesai |

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
6. Buka Run history untuk melihat keputusan, hasil review, dan artikel Indonesia/English.

Remote OpenAPI URL harus memakai HTTPS dan host-nya dicantumkan di ADMIN_ENDPOINT_ALLOWLIST. URL OpenAPI tanpa allowlist gagal tertutup. RESEARCH_ALLOWED_DOMAINS membatasi host yang dapat dibaca tool riset.

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

Tes menguji agent runtime, mapping/pagination blog, policy editorial, quota/lease, dan API dashboard dengan SQLite/fixture. Tes live ke model atau API blog belum dilakukan.

## Struktur utama

| Path | Isi |
| --- | --- |
| src/takumi_research_agent/api | Auth, konfigurasi dashboard, probe, runs, dan read-only adapter validation |
| src/takumi_research_agent/agent | Tool loop bertipe dan batas request/action |
| src/takumi_research_agent/blogs | OpenAPI loader, JSONPath mapping, history reader, dan guarded create primitive |
| src/takumi_research_agent/discovery | Trend collector Hacker News/arXiv dan skor heuristik |
| src/takumi_research_agent/research | Retriever, browser fallback, artifacts, evidence, serta research tools |
| src/takumi_research_agent/editorial | Penulis bilingual, critic, sanitizer, dan final body |
| src/takumi_research_agent/orchestration | Jadwal slot, worker lease, dan research-to-local-draft pipeline |
| src/takumi_research_agent/persistence | SQLAlchemy models, database, dan fenced queue |
| migrations | Schema versi Alembic |
| web | React/TypeScript dashboard |
| docs/planning | Plan, deep review, dan traceability requirement |

## Atribusi

Pemilik produk: Arya. Penyusun implementasi dan review kode: Codex.
