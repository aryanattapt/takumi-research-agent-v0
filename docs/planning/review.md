# Takumi Research Agent — Review Mendalam

| Metadata | Nilai |
| --- | --- |
| Reviewer | Codex |
| Tanggal | 6 November 2026 |
| Baseline | [plan awal, commit 195b6a3](https://github.com/aryanattapt/takumi-research-agent-v0/blob/195b6a35a6a336a3a4ee48e40d610fb5f727362f/docs/planning/plan.md) |
| Hasil revisi | [plan.md v1.1](plan.md) |
| Metode | static self-review terhadap requirement, kontrak desain, failure scenarios, dan sumber primer. |
| Batas | belum ada runtime agent/dashboard/publisher atau benchmark live yang dijalankan. |

## 1. Kesimpulan

Plan awal mempunyai fondasi yang tepat: modular monolith, API/RSS-first, policy publish/draft, adapter tervalidasi, outbox, dan status unknown/partial. Pendekatan tersebut dipertahankan.

Plan awal belum cukup konkret untuk menyatakan automatic publishing siap diimplementasikan tanpa keputusan tambahan. Review menemukan **16 gap desain**, terutama loop agent, pembuktian dukungan klaim, approval konten, stale worker, quota saat jadwal berubah, dan evaluasi kualitas.

Plan v1.1 menangani gap tersebut pada tingkat desain. Status ini tidak berarti implementasi sudah lolos pengujian atau semua risiko telah hilang.

## 2. Apakah ini AI agent?

Targetnya adalah agentic workflow: satu research agent memilih query/tools berdasarkan observation; workflow aplikasi mengendalikan jadwal, izin, budget, dan mutation blog.

Perilaku yang wajib dibuktikan:

1. Full text pertama unavailable: agent mencari sumber terbuka lain.
2. Sumber baru membantah premis: agent memperbarui fact sheet atau menolak klaim.
3. Budget habis: agent berhenti dengan daftar gap, bukan mengarang bukti.
4. Permintaan publish dari model: executor menolak karena research tools tidak punya izin tersebut.
5. Kritik editorial memerlukan bukti tambahan: riset dilanjutkan dalam budget run yang sama.

Beberapa panggilan LLM berurutan tidak otomatis memenuhi kriteria tersebut. Pembedaan workflow tetap dan agent yang memilih tools merujuk [Anthropic: Building effective agents](https://www.anthropic.com/research/building-effective-agents). Ini acuan arsitektur, bukan kewajiban memakai framework atau layanan vendor.

## 3. Temuan berdasarkan prioritas

P0 harus diselesaikan sebelum automatic publish. P1 penting untuk reliabilitas beberapa blog. P2 mengurangi risiko implementasi. Severity menilai rencana, bukan menyatakan insiden produksi.

| ID | Prioritas | Gap awal | Dampak | Perbaikan desain |
| --- | --- | --- | --- | --- |
| R-01 | P0 | Loop observation/action belum mempunyai kontrak. | Rangkaian prompt tetap atau agent tanpa batas. | 6.4: typed actions, tool registry, budget, stop conditions, dan eval adaptasi. |
| R-02 | P0 | JSON validation belum menjelaskan otoritas per action. | Instruksi dari web/OpenAPI mengarahkan tools berizin. | 6.4, 9.4, 14: scope dari runtime, input tetap data, authorization executor. |
| R-03 | P0 | Claim-to-source belum mendefinisikan validitas span dan semantic support. | Sitasi menuju paper benar, tetapi tidak mendukung kalimat artikel. | 10.6.1: evidence contract, locator/hash, jenis klaim, dan pemeriksaan konteks. |
| R-04 | P0 | Approval belum terikat versi konten dan payload. | Body atau mapping berubah tetapi memakai review lama. | 12.5: fingerprint serta invalidation; approval draft tidak berlaku untuk publish. |
| R-05 | P0 | Lease/heartbeat belum menangani worker lama setelah reclaim. | Stale update dan risiko duplicate effect. | 12.3: fencing epoch, compare-and-set, dan reconciliation in-flight effect. |
| R-06 | P1 | Run key mengikuti target UTC yang dapat diedit. | Dua jatah terbit pada hari sama setelah perubahan jadwal. | 11.4, 12.4: ledger editorial date/ordinal dan slot lock. |
| R-07 | P1 | Context selection dan compaction belum jelas. | Evidence/gap hilang, pencarian berulang, context membesar. | 6.5: memory terpisah, evidence references, context budget, dan replay. |
| R-08 | P1 | UAT belum memiliki gold set, multi-trial, dan gate terukur. | Satu demo atau parsing JSON dianggap bukti kualitas AI. | 17.3: labeled suite, holdout, rubric, trial, dan versioned qualification. |
| R-09 | P1 | Semantic verdict belum diikuti pengukuran retrieval. | Artikel relevan tidak pernah masuk shortlist. | 10.5, 17.3: recall@20 dengan distractor dan ukuran arsip yang dilaporkan. |
| R-10 | P1 | Growth score sudah mensyaratkan beberapa observation, tetapi collectornya belum dijadwalkan. | Data growth tetap unavailable untuk ranking harian. | 3.1, 10.3: polling ringan tanpa LLM serta observation coverage. |
| R-11 | P1 | Antrean sudah disebut, tetapi admission control dan rate-limit budget bersama belum dirinci. | Slot diterima saat kapasitas agregat tidak cukup. | 11.4: admission control, earliest deadline, provider limits, dan capacity benchmark. |
| R-12 | P1 | Backfill/delta/watermark belum menjadi protokol. | Sync mahal atau history incomplete dianggap lengkap. | 9.7: initial backfill, overlap, atomic watermark, dan perubahan feed. |
| R-13 | P1 | Shared cache belum membedakan sumber publik dan private context. | History/draft blog lain bocor melalui cache/prompt. | 9.7: scoped cache serta version keys. |
| R-14 | P1 | Budget, protokol, dan error classification terlalu umum. | Parallel overspend, tool-call mismatch, atau retry POST berbahaya. | 6.4, 13.2, 16.3: reservation, normalization, dan error classes. |
| R-15 | P2 | Risiko agent/publisher dibuktikan terlalu akhir. | Dashboard selesai sebelum loop/recovery layak. | 15–16: risk spike, persistent vertical slice, CI/eval sejak awal. |
| R-16 | P2 | Runtime/framework dan dua browser belum memiliki batas keputusan. | Membuat framework sendiri atau control loop tumpang tindih. | 6.6: runtime kecil, spike/ADR, Playwright dahulu, Browser Use opsional. |

Semua 16 temuan berstatus **ditangani pada desain, verifikasi runtime pending**.

## 4. Penjelasan temuan kritis

### Agent autonomy yang dibatasi

Kontrol alur luar tetap deterministik. Di dalam riset, model memilih tool berikutnya dan memperbarui hipotesis berdasarkan hasilnya. Finish dari model harus diperiksa controller, termasuk unresolved gaps.

Writer dan critic adalah role calls dengan context berbeda. Memakai model sama pada context berbeda tetap berisiko mengulang bias yang sama. Menambahkan banyak agent atau provider berbeda bukan pengganti evaluasi.

### Bukti harus mendukung klaim

Contoh: tulisan menyebut metode menurunkan biaya 80%. Menemukan angka 80% pada paper belum cukup: angka itu mungkin tingkat keberhasilan eksperimen, prediksi, atau konteks berbeda.

Evidence contract mengikat klaim ke artifact versi tertentu dan locator yang bisa diperiksa. Check span memastikan teks ada; check semantik memastikan teks mendukung klaim. Negasi, satuan, keterbatasan, dan generalisasi juga diperiksa.

Sumber yang diunduh lengkap tidak otomatis telah dibaca lengkap. Draft tetap harus dapat dipertanggungjawabkan; label draft tidak menghalalkan klaim salah.

### Approval harus membeku bersama konten

Boolean approved terlalu lemah jika body, evidence, status mapping, atau payload masih dapat berubah. Fingerprint baru mengikat versi konten, sumber, policy, adapter, review, dan action.

Perubahan material membatalkan approval. Profil paused atau credential revoked tetap bisa menahan pengiriman meskipun config snapshot lama masih ada.

### Lease tidak menjamin exactly-once

Worker A bisa kehilangan lease, sementara worker B mengambil alih dan request A masih in-flight. Fencing menolak stale update lokal, tetapi tidak bisa menarik kembali HTTP request.

Remote idempotency dan reconciliation tetap diperlukan. Jika API tidak menyediakan lookup/idempotency andal, hasil unknown ditahan, bukan diatasi dengan create ulang.

### Tenggat perlu kapasitas global

Target 06:00 adalah sudah terbit, bukan mulai riset. Lead time 120 menit merupakan default yang perlu dibuktikan. Antrean, browser, provider RPM/TPM, dan verification latency semuanya memengaruhi kapasitas.

Scheduled, accepted, atau HTTP 200 belum cukup untuk menyatakan published. Slot missed, draft, partial, dan unknown tetap dibedakan.

## 5. Best practice yang diterapkan dan bukti yang diperlukan

| Prinsip | Penerapan | Bukti sebelum produksi |
| --- | --- | --- |
| Autonomy sesuai tugas | Loop riset adaptif; mutation dikontrol aplikasi. | Eval observation mengubah action. |
| Least privilege | Research tools tidak punya mutation blog/secret. | Negative authorization dan injection tests. |
| Tool contracts | Input/output bertipe, namespace, error codes, dan batas output. | Fixture kedua provider dan tool schemas. |
| Grounded writing | Fact sheet, span, dan support review. | Kasus nyata berlabel serta kalibrasi grader. |
| Context discipline | Evidence references, compaction, dan working memory. | Long-run/restart evaluation. |
| Durable effects | Outbox, frozen intent, quota, fencing, reconciliation. | Fault injection pada fake dan sandbox blog. |
| Evaluation-driven change | Holdout, multi-trial, trace, dan qualification berversi. | Hasil terukur pada model/prompt yang dipilih. |
| Incremental rollout | Shadow, sandbox, pilot satu blog, lalu skala. | Kontrak nyata dan capacity benchmark. |
| Cost awareness | API/RSS-first, polling tanpa LLM, scoped cache, budget reservation. | Usage dan latency aktual. |

Angka gate pada plan merupakan target proyek yang diusulkan, bukan standar resmi universal atau hasil yang sudah dicapai. Lulus finite suite tidak membuktikan zero hallucination di dunia nyata.

Framework khusus tidak wajib. Runtime kecil tetap harus membuktikan contract/recovery; framework tidak menghilangkan kebutuhan evidence, authorization, dan eval.

## 6. Requirement yang dipertahankan

- Maksimal 10 blog dengan konfigurasi dan jadwal independen.
- Default satu topik terbaik per blog per hari dari tren global.
- Artikel ditargetkan sudah terbit 06:00 Asia/Jakarta.
- Dua versi Indonesia/English untuk pembaca awam.
- Dukungan jurnal relevan yang lolos pemeriksaan menghasilkan publish.
- Sumber primer kredibel tanpa jurnal memadai menghasilkan draft.
- Informasi meragukan tidak dijadikan konten layak kirim.
- OpenAPI atau sampel yang cukup untuk API dengan struktur JSON berbeda.
- Provider OpenAI-compatible dan Anthropic di dashboard.
- Kontrak API blog nyata tetap dependency integrasi terakhir.

## 7. Hal yang belum terbukti dari review dokumen

1. Model dapat menjalankan tools dan menilai bukti sesuai target.
2. Threshold dedup/retrieval cocok dengan arsip asli.
3. Kapasitas host/provider cukup untuk 10 blog.
4. Biaya dan lead time memenuhi konfigurasi.
5. API blog menyediakan draft, status, lookup, scheduling, atau idempotency.

Hasil review adalah plan yang lebih konkret untuk memulai spike. Runtime, evaluasi model, dan live UAT perlu membuktikan hasilnya sendiri.

## 8. Author commit GitHub

Commit awal `195b6a35a6a336a3a4ee48e40d610fb5f727362f` diperiksa langsung. Author dan committer sama-sama “Aryanatta Paramita Tiratana”, dengan noreply email akun GitHub pengguna.

Commit itu dibuat melalui operasi create-file konektor. Interface tersebut menerima path/content/message/branch, tanpa parameter author/committer; identitas akun terhubung dipakai. Nama pemilik produk di dokumen juga berbeda dari atribusi penulis isi.

Git lokal mempunyai identitas Codex, tetapi tidak mempunyai credential push GitHub. Dry-run push gagal pada autentikasi. Mengubah user.name lokal tidak mengubah commit yang dibuat melalui konektor.

Revisi memberi atribusi penyusun/reviewer kepada Codex dan menggunakan trailer `Co-authored-by: Codex <noreply@openai.com>`. Co-author adalah atribusi kontribusi, bukan penggantian author/committer utama atau akun pusher. Dengan operasi yang memakai default akun terhubung, nama utama tetap akun tersebut.

Untuk author utama Codex diperlukan jalur Git terautentikasi atau interface API yang mengekspos author/committer. Mengubah metadata commit lama mengubah SHA dan memerlukan rewrite history; hal tersebut tidak dilakukan pada revisi ini.

## 9. Referensi primer

- [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents).
- [Anthropic — Writing tools for agents](https://www.anthropic.com/research/writing-tools-for-agents).
- [Anthropic — Context engineering](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents).
- [Anthropic — Demystifying evals](https://www.anthropic.com/research/demystifying-evals-for-ai-agents).
- [Official OpenAI documentation — Agent safety](https://developers.openai.com/api/docs/guides/agent-builder-safety).
- [Official OpenAI documentation — Agent evaluations](https://developers.openai.com/api/docs/guides/agent-evals).
- [GitHub REST API — Git commits](https://docs.github.com/en/rest/git/commits#create-a-commit).

Panduan vendor dipakai untuk prinsip yang relevan; plan tidak mewajibkan layanan managed agent atau platform evaluasi vendor.
