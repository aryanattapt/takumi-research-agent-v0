# Outbox invariants

Rujukan saat mengadaptasi `src/takumi_research_agent/delivery/service.py` ke fitur lain (cancel, publish-existing, single-resource). Jika kode berubah, perbarui dokumen ini.

## Identity

- `unit_key` = unit fisik pengiriman. Mode default `separate_posts`: `article:<locale>`.
- `delivery_key = hash(blog_id, destination_epoch, pair_id, unit_key, action, operation_id)`. Nilainya harus sama untuk retry operasi logis yang sama.
- `approval_fingerprint` mengikat intent ke konten yang direview. Material minimal:
  - `blog_id`, `blog_revision`, `destination_epoch`, `execution_mode`
  - digest adapter: `base_url`, `adapter_mapping`, `capabilities`
  - digest `editorial_settings` dan `schedule` (timezone, publish_time, lead_minutes, enabled)
  - `operation_id`, `operation_method`, `operation_path`
  - `pair_id`, `decision`, digest `review_result` (termasuk referensi evidence/source yang dibekukan saat review)
  - `locale`, `unit_key`, `action`, `body_hash`, digest `payload`

Jangan masukkan token, API key, atau secret lain ke material.

## State machine

```
prepared ──claim──> dispatching ──success──> succeeded
    │                    ├──4xx/error──> failed ──retry──> dispatching
    │                    └──timeout/5xx─> unknown   (terminal, manual reconcile)
    └──fingerprint berubah──> invalidated (terminal)

dispatching lebih dari 2 menit ──> unknown
```

Intent yang masih `dispatching` dianggap stale hanya setelah dua menit tanpa attempt yang selesai. Sebelum itu, dispatcher kedua harus membiarkan request tetap berjalan dan dashboard tidak menawarkan rekonsiliasi. Setelah window lewat, dispatcher berikutnya dapat mengubahnya menjadi `unknown`; intent tetap tidak dikirim ulang otomatis.

- `unknown` tidak di-retry otomatis. Selesaikan lewat endpoint rekonsiliasi operator (`POST /api/v1/runs/{run_id}/delivery/{intent_id}/reconcile` dengan `outcome: succeeded|failed` dan `remote_id` opsional); endpoint mencatat event audit dan tidak membuat intent/key baru.
- Rekonsiliasi `dispatching`/`unknown` menutup attempt yang belum selesai. Claim state intent harus atomik agar dua operator tidak saling menimpa hasil rekonsiliasi.

- `failed` boleh di-retry sampai batas attempt; `unknown` tidak pernah otomatis.
- `succeeded` tidak pernah dikirim ulang meskipun locale lain gagal.

## Gate order (sebelum network)

1. Run dan blog ada.
2. `run.execution_mode` bukan `dryrun`.
3. `GlobalSettings` ada, `recovery_mode` false, `content_mutations_paused` false.
4. Blog aktif; `blog.revision` dan `destination_epoch` sama dengan `run.config_snapshot`.
5. Mapping dan create operation valid.
6. Pair punya versi `id` dan `en`.
7. Per intent: capability `create_draft`/`publish` terbukti; versi artikel ada; fingerprint masih cocok; attempt di bawah batas.
8. Baru claim `dispatching`, catat attempt `in_flight`, lalu panggil adapter.

## Attempt & response

- Payload final disimpan di intent sebelum request; adapter menerima payload itu apa adanya.
- `DeliveryAttempt` mencatat nomor, outcome, HTTP status, dan ringkasan response (bukan body penuh).
- Header `Idempotency-Key` hanya dikirim jika capability `idempotency_key` sudah terbukti.
- Remote ID diambil dari `mapping.response_fields["id"]` atas response body.

## Ledger

- `prepare`: `local_state = delivery_prepared`.
- `succeeded`: `local_state = delivered`, `remote_status = accepted_unverified`, simpan `remote_id`; verifikasi remote tetap terpisah.
- `unknown`: `local_state = delivery_unknown`, `remote_status = unknown`.

## Status yang tidak boleh disatukan

HTTP status, outcome attempt, remote status, editorial decision, pair completeness, dan deadline adalah state berbeda. Jangan memakai satu kolom untuk menyimpulkan "published".
