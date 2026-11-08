---
name: takumi-guarded-feature
description: Menambah fitur durable dan bergate di Takumi Research Agent (outbox, approval fingerprint, capability gate, API, dashboard) dengan mengikuti layer serta tabel yang sudah ada. Gunakan saat user minta "implementasi fitur delivery", "tambahkan fitur yang masih kosong", "buat alur dengan pause/approval", "lanjutkan opsi implementasi", atau "tambah fitur aman di Takumi".
metadata:
  created-by: semut-jalur
---
# Takumi guarded feature slice

Menambahkan satu fitur end-to-end ke Takumi tanpa melemahkan invariant riset/editorial: intent disimpan dulu, mutasi remote digate capability, dan status tidak boleh menyesatkan. Tidak mencakup perubahan pada pipeline riset/editorial inti atau penulisan remote tanpa kontrak dan capability test.

## When to use

- User memilih opsi implementasi setelah audit daftar fitur yang belum selesai (misalnya menjawab "c").
- Permintaan menambah outbox, delivery, publish, cancel, atau state machine baru di Takumi.
- Permintaan menambah endpoint/dashboard untuk aksi yang menyentuh data eksternal atau butuh audit.

## Steps

1. Petakan repo dan cek dependency lebih dulu.
   ```
   find src web/src tests migrations -type f | sort
   find src web/src tests -type f \( -name "*.py" -o -name "*.ts" -o -name "*.tsx" \) | xargs wc -l | sort -n
   python -m compileall -q src tests
   python - <<'EOF'
   import importlib.util
   for m in ["sqlalchemy", "pydantic", "httpx", "fastapi", "pytest"]:
       print(m, bool(importlib.util.find_spec(m)))
   EOF
   ```
   Jika dependency tidak ada, jangan paksa install; verifikasi dengan `compileall` dan katakan test tidak dijalankan.

2. Baca desain sebelum kode: `docs/planning/plan.md` §9.6 (capability matrix), §9.7 (unit pengiriman), §12.4 (idempotency/outbox), §12.5 (approval), dan `docs/planning/review.md` §10. Invariant di sana adalah acceptance, bukan saran.

3. Cari scaffolding yang sudah ada; jangan bikin ulang.
   ```
   rg -n "DeliveryIntent|DeliveryAttempt|approval_fingerprint|delivery_key|ContentLedger" src tests
   grep -n "delivery_intents\|delivery_attempts\|content_ledger" migrations/versions/*.py
   ```
   Gunakan model/tabel/helper yang ada. Tambah migrasi Alembic baru hanya jika kolom benar-benar belum ada; jangan edit migrasi lama.

4. Tentukan layer pemilik (satu fitur menyentuh semua ini):
   | Layer | Lokasi |
   | --- | --- |
   | Service/domain baru | `src/takumi_research_agent/<domain>/service.py` |
   | Wiring pipeline | `src/takumi_research_agent/orchestration/pipeline.py` setelah persist |
   | HTTP + validasi | `src/takumi_research_agent/api/app.py`, `api/schemas.py` |
   | Dashboard | `web/src/App.tsx`, `web/src/types.ts` |
   | Test | `tests/test_<domain>.py` |

5. Implementasikan pola outbox di service:
   - `prepare(session, *, run_id)`: baca run/pair/blog dari DB, buat satu intent per unit fisik (`article:<locale>`), payload final dari `build_create_payload`, kunci deterministik lewat `delivery_key()`, dan `approval_fingerprint()`.
   - `dispatch(session, *, run_id, adapter)`: jalankan semua gate dulu, baru claim intent, catat `DeliveryAttempt(in_flight)`, panggil adapter, lalu finalkan.
   - State intent: `prepared` → `dispatching` → `succeeded`/`failed`/`unknown`; `invalidated` bila approval berubah.
   - Detail material fingerprint dan urutan gate: `references/outbox-invariants.md`.

6. Kirim payload yang tersimpan, jangan rebuild saat kirim. Tambahkan `send_payload(payload, *, intent_key, publish)` di `blogs/adapter.py`, lalu buat `create()` memanggilnya. Dengan begitu body preview/outbox/wire identik.

7. Urutan gate dispatch (semua sebelum network):
   `run.execution_mode != "dryrun"` → `GlobalSettings` ada dan bukan `recovery_mode` → tidak `content_mutations_paused` → `blog.active` dan `blog.revision`/`destination_epoch` cocok snapshot → capability `create_draft`/`publish` terbukti → fingerprint cocok → attempt < batas.
   Kegagalan global → `DeliveryBlocked(code)`; kegagalan per intent → state tetap/`invalidated` dan masuk daftar `blocked`.

8. Tambahkan API minimal: schema `StrictRequest`, endpoint dengan `Depends(require_csrf)`, decrypt secret via `SecretCipher(settings.master_key)`, bangun adapter dengan capability dari `blog.capability_result`, dan map `DeliveryBlocked` → HTTP 409. Sertakan status outbox (`state`, `remote_status`, `local_state`) di `GET /api/v1/runs/{id}` dan endpoint rekonsiliasi untuk intent `unknown`/`dispatching` yang mencatat event audit.

9. Dashboard: tampilkan status outbox dan tombol aksi hanya untuk mode non-dryrun; setiap aksi remote wajib `window.confirm`. Jangan pernah mengirim otomatis dari render atau polling.

10. Tulis test service dengan SQLite in-memory dan adapter palsu, bukan jaringan.
    ```
    engine, sessions = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    ```
    Kasus minimum: prepare idempoten; dispatch sukses memperbarui ledger; pause memblokir tanpa panggilan; dry-run tidak mengirim; body berubah → `invalidated`; evidence/review berubah → `invalidated`; `DeliveryOutcomeUnknown` → `unknown` dan tidak di-retry; capability belum terbukti → tetap lokal.
    Untuk adapter HTTP, pakai `httpx.MockTransport` seperti `tests/test_blog_adapter.py`.

11. Update `README.md` (status, guard, langkah operasional) hanya untuk perilaku yang benar-benar ada.

12. Verifikasi (lihat bagian Verify) dan laporkan batasan yang tersisa.

## Pitfalls

- Simpan intent sebelum network call: crash setelah POST tanpa intent membuat retry menghasilkan duplikat.
- Jangan retry otomatis state `unknown`: outcome remote tidak diketahui; butuh rekonsiliasi operator.
- Hitung ulang fingerprint saat dispatch: body/review/mapping/destination yang berubah setelah review harus membatalkan approval.
- Masukkan `destination_epoch`, `blog.revision`, mapping, capability, dan `execution_mode` ke fingerprint: tanpa itu intent lama bisa terkirim ke konfigurasi baru.
- Jangan menyimpan secret di payload atau material fingerprint: hanya referensi/versi credential.
- Jangan menurunkan keputusan `publish` menjadi `draft` secara diam-diam: keputusan editorial dan aksi fisik harus eksplisit.
- Jangan memanggil adapter sebelum semua gate lulus: satu request terkirim tidak bisa dibatalkan oleh kill switch lokal.
- Jangan menulis test yang menembak API/model nyata: inject fake adapter atau `MockTransport`.
- Jangan mengklaim pytest lulus saat dependency tidak terpasang: cukup `compileall` dan sebutkan test belum dijalankan.
- Tandai payload uji capability dengan jelas dan gunakan status draft: test-create membuat konten remote nyata yang tidak otomatis terhapus.

## Verify

```
python -m compileall -q src tests
python -m pytest tests/test_<domain>.py -q    # hanya jika dependency terpasang
```

Acceptance: prepare mengembalikan intent deterministik dan idempoten; dispatch sukses mencatat attempt + remote ID + ledger; pause/dry-run/capability/fingerprint memblokir tanpa panggilan adapter; state `unknown` tidak terkirim ulang saat dispatch dijalankan lagi.
