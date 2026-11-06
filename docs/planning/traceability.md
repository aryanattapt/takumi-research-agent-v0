# Takumi — Requirement Traceability

| Metadata | Nilai |
| --- | --- |
| Dokumen acuan | [plan.md v1.2](plan.md) |
| Review | [review.md](review.md), review pertama dan lanjutan |
| Penyusun | Codex |
| Tanggal | 6 November 2026 |
| Status | Cakupan desain; belum menjadi hasil implementasi/test. |

## 1. Requirement pengguna ke desain dan acceptance

Nomor bagian mengacu pada plan v1.2. Seluruh UAT adalah skenario yang harus dibangun/dijalankan, bukan hasil test yang sudah lolos.

| Requirement | Desain utama | Milestone | Acceptance/UAT |
| --- | --- | --- | --- |
| BR-01 — Maksimal 10 blog | 3, 8.3, 12.1; limit atomik dan scope per blog | M1, M3, M6 | UAT-01, UAT-14; profil paused tetap dihitung. |
| BR-02 — Dashboard konfigurasi | 8, 13, 14; forms, revisions, secrets, jadwal dan observasi | M1, M3 | UAT-01, UAT-17, UAT-50; E2E save/reload, masked secret, dan validation errors. |
| BR-03 — Technology | 10.3–10.5; registry AI agent/software engineering/cloud | M0, M4, M5 | UAT-21, UAT-46; research suite mempunyai contoh tiga subbidang tersebut. |
| BR-04 — Physics/chemistry/biology | 10.3–10.5, 17.3; discovery dan full-text coverage | M0, M4, M5 | UAT-46; minimal 10 research cases per bidang pada qualification suite. |
| BR-05 — Topik harian terbaik/global | 3.1, 10.3, 11.4; observasi, ranking, quota slot | M4, M6 | UAT-14, UAT-24, UAT-43; satu topik/pair per slot dan skor dengan provenance. |
| BR-06 — Sudah terbit 06:00 | 11, 12.2; persiapan awal, delivery window, verifikasi | M6, M7 | UAT-14, UAT-15, UAT-19, UAT-33, UAT-34, UAT-36. |
| BR-07 — API/JSON dinamis | 8.3, 9; safe mapping, capabilities, unit fisik | M0, M3, M7 | UAT-02, UAT-12, UAT-32, UAT-35, UAT-44, UAT-45. |
| BR-08 — Cek list/history blog | 9.8, 10.6, 10.9; remote history dan content ledger | M2, M4 | UAT-03, UAT-04, UAT-30, UAT-37, UAT-38, UAT-39. |
| BR-09 — OpenAI-compatible/Anthropic | 6.4, 13; provider contracts, capabilities, budgets | M0, M2, M3, M5 | UAT-17, UAT-31, UAT-42, UAT-43, UAT-48; fixture kedua protokol. |
| BR-10 — Jurnal/primer kredibel | 10.4, 10.5, 10.7; source/evidence contract | M0, M4, M5 | UAT-05, UAT-06, UAT-07, UAT-26, UAT-46. |
| BR-11 — Skip hoaks/keraguan | 10.7, 10.7.1, 12.5; support, contradiction, approval | M2, M5 | UAT-07, UAT-08, UAT-16, UAT-25, UAT-26, UAT-32. |
| BR-12 — Indonesia dan English | 9.7, 10.8, 12.2; ArticlePair, completeness, parity | M2, M3, M5 | UAT-11, UAT-18, UAT-35, UAT-37; default dua post terpisah. |
| BR-13 — Awam dan menarik | 10.8, 17.3–17.4; hook, istilah, analogi, limits | M5 | UAT-49; rubric 1–5 dan pertanyaan pemahaman, terpisah dari gate fakta. |
| BR-14 — Jurnal publish, lainnya draft | 10.7, 10.9, 11.6, 12.5 | M2, M5, M7 | UAT-05, UAT-06, UAT-09, UAT-25, UAT-33, UAT-39; draft unsupported tetap lokal. |
| BR-15 — API disediakan kemudian | 4.2, 15.3, 16, 18; fixtures lalu live UAT | M0–M7 | UAT kontrak/sandbox M7; tanpa kontrak valid integrasi production tetap blocked. |

## 2. Invariant lintas requirement

| Invariant | Kontrak | Acceptance |
| --- | --- | --- |
| Agent adaptif tetapi berizin | Typed action, scope dari runtime, stop/budget enforcement | UAT-21, UAT-22, UAT-32. |
| Context tidak menghapus bukti/gap | Artifact references dan checkpoint | UAT-13, UAT-27, UAT-41. |
| Tidak mengulang efek yang belum diketahui | Outbox, unit fisik, operation identity, reconciliation | UAT-10, UAT-11, UAT-23, UAT-35, UAT-40. |
| Tidak mengirim konten yang berubah setelah review | Approval fingerprint, destination/mode, expected remote version | UAT-25, UAT-26, UAT-44, UAT-45, UAT-47. |
| Pause tidak memberi kesan remote schedule otomatis batal | Cancel capability, control intent, status unknown | UAT-33, UAT-34. |
| Kuota dan config tidak tertimpa race | Slot ledger, creation transaction, revision conflict | UAT-01, UAT-24, UAT-50. |
| Biaya dibatasi dan jujur | Satu retry owner, reservation, actual attempts, stage reserves | UAT-17, UAT-28, UAT-42, UAT-43. |
| Restore tidak mengulangi post yang sudah dibuat | Recovery mode, history/schedule sync, artifact manifest | UAT-20, UAT-40, UAT-41. |
| Preview dan payload identik secara editorial dan aman | Canonical body, renderer/sanitizer bersama, source-linked references | UAT-25, UAT-51. |
| Status tidak menyesatkan | Processing, editorial, delivery, completeness, deadline axes | UAT-19, UAT-34, UAT-36. |
| Sumber dan history terisolasi | URL policy, scoped cache, prompt boundaries | UAT-16, UAT-29, UAT-32. |

## 3. Keputusan terkonfirmasi dan default usulan

| Jenis | Isi |
| --- | --- |
| Dikonfirmasi pengguna | Maksimal 10 blog; dashboard; dua keluarga provider; satu topik harian/global; target sudah terbit 06:00 WIB; dua versi bahasa; jurnal untuk publish dan primer kredibel lainnya untuk draft. |
| Default implementasi yang diusulkan | Lead time 120 menit, polling 60 menit, concurrency 2/1/2, 20 request model, 40 tool actions, PostgreSQL, FastAPI, React, serta threshold eval. Semuanya perlu compatibility/capacity test. |
| Alternatif yang harus dipilih eksplisit | Single-resource multilingual jika API tidak memisahkan post; remote scheduling setelah read/cancel terbukti. Default tetap separate posts dan direct delivery dengan margin. |
| Bukan janji produk | Zero hallucination, exactly-once pada API tanpa dukungan, semua sumber selalu dapat diakses, atau tepat waktu tanpa syarat. |
| Tidak ditambahkan sebagai syarat | Search/browser berbayar, Kubernetes, multi-agent swarm, atau approval manual setiap artikel yang sudah lolos policy. |

Default satu topik berlaku per blog, sehingga 10 blog aktif memerlukan kapasitas hingga 10 pairs/20 versi sehari. Tidak ada kuota kategori wajib secara default. Jika topik tidak layak, kualitas tidak dikorbankan untuk mengisi slot.

## 4. Dependency eksternal yang belum selesai

| Dependency | Cara ditutup |
| --- | --- |
| API blog, autentikasi, schema, dan capability | Diberikan pengguna lalu dites pada sandbox M7; mutasi tidak ditebak. |
| Model, endpoint, dan limit provider | Diisi di dashboard, capability probe, lalu qualification sesuai role. |
| Hardware dan deployment limits | Benchmark browser/worker/provider sebelum menaikkan kapasitas. |
| Budget dan durasi aktual | Ukur usage, retry, serta latency; sesuaikan lead time dan quota. |

Daftar ini adalah input implementasi/UAT yang belum tersedia, bukan requirement pengguna yang terlupakan.

## 5. Status kelengkapan review

- BR-01 sampai BR-15 mempunyai rujukan desain dan acceptance.
- UAT-01 sampai UAT-51 bernomor unik dan meliputi alur utama serta fault scenarios yang direncanakan.
- Review pertama dan lanjutan dicatat dengan baseline berbeda agar perubahan dapat ditelusuri.
- Status saat ini: desain siap untuk risk spike; runtime, quality metrics, dan integrasi live belum dinyatakan lulus.
