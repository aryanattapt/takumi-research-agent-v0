# Conformance checklist

Gunakan saat mengaudit fitur Takumi terhadap `docs/planning/plan.md`. Setiap baris harus punya bukti jalur kode, bukan hanya keberadaan kolom/endpoint.

## Status

- **Sesuai**: invariant ditegakkan di jalur runtime dan ada test/event yang membuktikan.
- **Sebagian**: ada mekanismenya, tetapi satu kondisi gagal atau verifikasinya belum ada.
- **Belum**: tidak diimplementasikan, atau hanya menunggu kontrak/kapabilitas eksternal.

## Inti yang dicek

| Invariant | Bukti yang dicari |
| --- | --- |
| Intent disimpan sebelum network | `prepare()` menulis `DeliveryIntent` + payload; `dispatch()` tidak membangun payload baru |
| Identitas stabil | `delivery_key` deterministik dari blog, epoch, pair, unit, action, operation |
| Approval mengikat konten | fingerprint mencakup body hash, payload, review, evidence/source, mapping, editorial, jadwal, epoch, mode, unit |
| Retry tidak menggandakan | state `succeeded` terminal; `unknown` tidak di-retry; `failed` retry memakai intent yang sama |
| Unknown punya jalan keluar | endpoint rekonsiliasi + `RunEvent` audit; tidak membuat key baru |
| Gate sebelum mutasi | mode non-dryrun, recovery, pause, blog aktif, snapshot revision/epoch, capability, attempt limit |
| Capability tidak dikarang | hanya dianggap terbukti setelah request nyata; ganti operasi create mereset flag |
| Status tidak menyatu | intent state, remote status, local state, editorial decision, completeness, deadline terpisah |
| Dry-run tidak mutasi | `dispatch` menolak `dryrun`; test-create/aksi remote butuh acknowledgment eksplisit |
| Race finalisasi | transisi state bersyarat; proses yang kalah tidak menimpa `unknown` |

## Jebakan audit

- Kolom ada ≠ aturan ditegakkan: telusuri siapa yang membaca kolom itu di jalur mutasi.
- Evidence tersimpan ≠ evidence dibekukan: cek apakah hash/referensinya masuk fingerprint.
- Capability di JSON ≠ capability terbukti: cari request yang benar-benar menguji.
- "Publish" decision ≠ publish capability: cek gate action `publish` terpisah dari `create_draft`.
- Status `succeeded` ≠ remote published: cek label remote dan verifikasi baca.
- Plan menyebut lookup/cancel/schedule: jangan tandai sesuai jika hanya ada hold-unknown.
- Perubahan adapter (base URL, mapping, create operation, token) harus membatalkan approval/capability; cek jalur update.
- Jangan memperbaiki gap yang butuh kontrak eksternal dengan mengarang capability.

## Peta requirement → area kode

| Area | Path |
| --- | --- |
| Outbox/approval/gate | `src/takumi_research_agent/delivery/service.py` |
| Persist pair/evidence/ledger | `src/takumi_research_agent/orchestration/pipeline.py` |
| Adapter create/http | `src/takumi_research_agent/blogs/adapter.py` |
| Mapping/operation | `src/takumi_research_agent/blogs/contracts.py` |
| Endpoint + status | `src/takumi_research_agent/api/app.py` |
| Model/state | `src/takumi_research_agent/persistence/models.py` |
| Scheduler/lease | `src/takumi_research_agent/orchestration/scheduler.py`, `persistence/queue.py` |
| Test pola | `tests/test_delivery.py`, `tests/test_api.py`, `tests/test_blog_adapter.py` |
