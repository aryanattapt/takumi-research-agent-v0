---
name: takumi-requirement-audit
description: Mengaudit kesesuaian implementasi Takumi Research Agent dengan requirement di plan/traceability, memisahkan yang sudah sesuai, sebagian, dan belum, lalu memperbaiki gap yang ada dalam scope. Gunakan saat user minta "code review ulang apa sudah sesuai requirement", "audit kesesuaian requirement", "cek gap terhadap plan.md", "review implementasi vs desain", atau "apakah sudah sesuai requirement".
metadata:
  created-by: semut-jalur
---
# Takumi requirement audit

Menilai implementasi terhadap acceptance di `docs/planning/` dan melaporkan gap dengan bukti jalur kode. Bukan untuk menambah fitur baru; untuk implementasi fitur gunakan `takumi-guarded-feature`.

## When to use

- User meminta review ulang setelah implementasi: "sudah sesuai requirement?", "cek gap", "audit".
- Sebelum menandai milestone selesai atau menjawab pertanyaan kepatuhan.
- Saat README/klaim dashboard perlu diverifikasi terhadap desain.

## Steps

1. Cetak anchor dokumen, jangan mengandalkan ingatan.
   ```
   bash .semutcode/skills/takumi-requirement-audit/scripts/audit-anchors.sh .
   ```
   Script menampilkan heading `plan.md`, seluruh `traceability.md`, tabel invariant, review lintas komponen, dan baris UAT.

2. Baca section desain yang mengatur area yang diaudit. Cari dulu nomornya dengan `grep -n`, lalu baca rentangnya dengan `sed -n`. Untuk fitur delivery/outbox, wajib: §8.3, §9.6, §9.7, §9.8, §10.10, §11.6, §12.2, §12.3, §12.4, §12.5. Baca juga baris UAT terkait (mis. UAT-10, 23, 25, 26, 44, 45, 47, 51).

3. Inventaris implementasi dan test yang benar-benar ada.
   ```
   rg -n "DeliveryIntent|DeliveryAttempt|ContentLedger|approval_fingerprint|delivery_key" src tests
   rg -n "execution_mode|content_mutations_paused|recovery_mode|capability_result" src/takumi_research_agent
   find src/takumi_research_agent -name "*.py" | xargs wc -l | sort -n
   ```

4. Bangun matriks sebelum menyimpulkan: `requirement → section desain → bukti file:baris → status`.
   Status hanya tiga: **Sesuai**, **Sebagian**, **Belum**. "Sebagian" wajib menyebut kondisi yang gagal.

5. Uji invariant lintas komponen memakai `references/conformance-checklist.md`. Telusuri jalur penegakan, bukan keberadaan kolom: siapa membaca nilai itu sebelum mutasi, apa yang terjadi saat config berubah, apa yang terjadi saat proses lain menang.

6. Saat menemukan bug saat audit, perbaiki hanya yang berada di scope dan tidak butuh kontrak eksternal. Perbaikan tipikal:
   - tambahkan input yang belum masuk fingerprint;
   - fence transisi state dengan conditional update;
   - ekspos status yang sudah ada agar tidak menyesatkan;
   - reset capability saat operasi/adapter berubah;
   - tambah endpoint rekonsiliasi audit untuk state `unknown`.
   Untuk setiap perbaikan, tambahkan test di `tests/test_<domain>.py` memakai SQLite in-memory dan fake adapter/`MockTransport`.

7. Jangan menutup gap yang bergantung pada kontrak eksternal dengan mengarang capability. Catat sebagai trigger: butuh kontrak/sandbox, proof publish, lookup, cancel, atau idempotency.

8. Verifikasi apa yang benar-benar tersedia.
   ```
   python -m compileall -q src tests
   python -m pytest tests/test_<domain>.py -q    # hanya jika dependency terpasang
   ```
   Jika pytest tidak bisa jalan, katakan test belum dieksekusi; jangan menyebut lulus.

9. Laporkan dengan urutan: verdict singkat → tabel Sesuai/Sebagian/Belum dengan bukti → perbaikan yang dilakukan saat audit → gap tersisa + dampak + trigger berikutnya. Pisahkan "belum diimplementasikan" dari "menunggu kontrak".

## Pitfalls

- Gunakan `plan.md` dan `traceability.md` sebagai acceptance, bukan README: README adalah klaim status yang justru perlu diverifikasi.
- Jangan menandai "Sesuai" hanya karena kolom/endpoint ada: telusuri jalur mutasi yang membaca nilai itu.
- Masukkan evidence/source ke fingerprint approval: menyimpan evidence saja tidak membatalkan approval saat sumber dikoreksi.
- Fence finalisasi state dengan conditional `WHERE state='dispatching'`: dua dispatch bersamaan bisa saling menimpa `unknown`.
- Jangan retry `unknown` otomatis: outcome remote tidak diketahui dan retry menciptakan duplikat.
- Jangan menyamakan keputusan editorial `publish` dengan capability `publish`; buktikan aksi fisiknya terpisah.
- Reset capability saat operasi create/mapping/base URL berubah: capability lama tidak berlaku untuk operasi baru.
- Jangan menganggap `remote_status=accepted_unverified` sebagai published; verifikasi baca adalah langkah terpisah.
- Jangan memperbaiki gap eksternal dengan flag buatan; laporkan sebagai trigger.
- Jangan mengklaim test lulus saat dependency tidak terpasang: `compileall` hanya memeriksa sintaks.

## Verify

```
bash .semutcode/skills/takumi-requirement-audit/scripts/audit-anchors.sh .
python -m compileall -q src tests
```

Audit dianggap selesai jika setiap requirement yang diklaim **Sesuai** punya bukti file:baris atau test/event, setiap **Sebagian** menyebut kondisi gagal, dan setiap gap eksternal punya trigger yang eksplisit.
