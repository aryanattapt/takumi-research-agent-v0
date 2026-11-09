# Invariant dan bentuk laporan operasi data

Ringkasan kontrak dari `docs/planning/plan.md` §18.3 (artifact, backup, restore) dan §12.6 (migrasi SQLite), plus bentuk implementasi yang sudah ada di repo. Pakai sebagai checklist desain, bukan dokumen bebas tafsir.

## Gate berurutan (semua sebelum efek samping)

| Operasi | Gate |
| --- | --- |
| Backup | source artifact ada dan hash cocok → direktori tujuan di luar artifact root → tidak menimpa backup non-kosong. Gagal di gate mana pun = `BackupBlocked(code)`, tidak ada file setengah jadi. |
| Verify | manifest format didukung → hash `database.json` cocok → jumlah baris per tabel cocok → setiap artifact ada dan hash cocok. |
| Restore | `acknowledge=True` → verify lolos → schema revision backup == revision DB aktif (kecuali keduanya `unmanaged`) → baru delete+insert. |
| Import legacy | file sumber ada → blog profile ada → profil **tidak** `active` → ambil backup DB aktif → lalu menulis. |

## Bentuk artifact backup

```
<DATA_DIR>/backups/<backup_id>/
  manifest.json      # format, backup_id, created_at, schema_revision, key_reference, database{file,sha256,size,rows}, artifacts[]
  database.json      # {"format":1,"schema_revision":...,"tables":{tabel:[baris...]}}
  artifacts/<kind>/<digest>
```

- Encode nilai khusus dengan penanda: `{"type":"datetime","value":iso}`, `{"type":"date",...}`, `{"type":"bytes","value":base64}`.
- Urutan insert = `Base.metadata.sorted_tables`; urutan delete = kebalikannya.
- `alembic_version` bukan model: baca revision dengan `select version_num from alembic_version`, anggap `"unmanaged"` bila tabel tidak ada.

## Efek restore pada state

| Objek | Hasil |
| --- | --- |
| `GlobalSettings` | `recovery_mode=True`, `content_mutations_paused=True`, `revision += 1` |
| Run `running`/`delivering`/`reconciling` | `status="blocked"`, `stage="restore_recovery"`, `pipeline_outcome="restore_fault"`, lease dikosongkan, `lease_epoch += 1`, `RunEvent restore_lease_invalidated` |
| `DeliveryIntent.state == "dispatching"` | `"unknown"` + `response_summary.reconciliation_required=True` |
| `DeliveryAttempt.outcome == "in_flight"` | `"unknown"`, `finished_at` diisi |
| `ContentLedger` unit terkait | `local_state="delivery_unknown"`, `remote_status="unknown"` |
| Audit | `AuditEvent(action="system_restore", resource_type="backup", resource_id=<backup_id>, details=<report>)` |

## Bentuk laporan (dipakai CLI, API, dan dashboard)

```json
{
  "backup_id": "…",
  "schema_revision": {"backup": "…", "current": "…"},
  "tables": {"runs": 3},
  "artifacts": {"checked": true, "total": 2, "verified": 2, "missing": [], "corrupt": [], "truncated": false},
  "master_key_present": true,
  "blocked_runs": ["…"],
  "unknown_intents": ["…"],
  "profiles_requiring_revalidation": ["…"],
  "providers_requiring_recredential": [],
  "blogs_requiring_recredential": [],
  "schedule_cancel_or_resync_review": ["…"],
  "recovery_mode": true
}
```

Import menambahkan `dry_run`, `source{fingerprint,file_sha256,blog_id}`, `counts{tabel:{imported,duplicates,skipped}}`, `skipped[]`, `reconciliation_required`, `artifacts_written`, `verification{sources_total,blog_history_for_blog,content_ledger_for_blog,blog_history_without_profile,content_ledger_without_profile,artifacts_readable,artifacts_unreadable}`.

## Pemetaan legacy SQLite (`DATA_DIR/takumi.sqlite3`)

| Tabel legacy | Tujuan | Catatan |
| --- | --- | --- |
| `papers(doi, record, content)` | `sources` id `doi:<doi>` | isi penuh → artifact kind `legacy`, digest di `metadata_json.legacy_import.content_artifact` |
| `blog_posts(post_id, language, title, content)` | `blog_history` | `snapshot_id` = hash `legacy:<fingerprint>`; baris tanpa judul di-skip beralasan |
| `publications(publication_key, slot, status, payload, response)` | `content_ledger` `local_state="legacy_publication"` | `pair_id="legacy-<sha256[:32]>"`, `unit_key="legacy:publication"`, wajib audit rekonsiliasi |
| `jobs`, `drafts`, `agent_events` | `AuditEvent` + artifact | tidak ada run/pair identity; jangan dikarang |

## Endpoint yang sudah ada

- `GET /api/v1/recovery` → `{recovery_mode, content_mutations_paused, artifacts, last_restore}`; `?verify_artifacts=true` memaksa hashing, default mengembalikan `checked=false` agar polling murah.
- `GET /api/v1/capacity` → rencana antrean (lihat modul `orchestration/capacity.py`).
