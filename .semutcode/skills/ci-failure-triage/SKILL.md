---
name: ci-failure-triage
description: Mendiagnosis dan memperbaiki CI GitHub Actions yang gagal untuk repo ini, termasuk menemukan job/step yang gagal tanpa `gh`, mereproduksi test secara lokal, memperbaiki akar masalah, lalu memverifikasi run hijau. Gunakan saat user minta "ci/cd failed", "cek kenapa CI gagal", "fix pipeline", atau "push lalu pastikan CI hijau".
metadata:
  created-by: semut-jalur
---
# CI failure triage

Menemukan step CI yang gagal, membedakan kegagalan kode dari lingkungan lokal, memperbaiki, dan memastikan run untuk commit terbaru hijau. Tidak mencakup perubahan workflow CI itu sendiri.

## When to use

- "ci/cd failed", "CI merah", "cek pipeline".
- Setelah push dan user ingin memastikan Actions hijau.
- Saat test lulus lokal tetapi CI gagal, atau sebaliknya.

## Steps

1. Cek state dan identitas repo.
   ```
   git -c safe.directory="$PWD" status -sb
   git -c safe.directory="$PWD" log --oneline -3
   git -c safe.directory="$PWD" remote -v
   ```

2. Temukan run dan step yang gagal.
   ```
   python .semutcode/skills/ci-failure-triage/scripts/ci_status.py
   python .semutcode/skills/ci-failure-triage/scripts/ci_status.py --sha <sha-prefix>
   ```
   Script memakai GitHub REST API, menampilkan `backend`/`dashboard`, dan menandai `FAILED STEP`. Endpoint log job membutuhkan admin (403), jadi jangan bergantung padanya.

3. Baca definisi workflow `.github/workflows/ci.yml` untuk tahu perintah persisnya (install, compile, pytest, alembic, npm build).

4. Reproduksi step backend secara lokal.
   ```
   python -m pip install -q "sqlalchemy>=2.0,<3" "pydantic>=2.10,<3" "httpx>=0.28,<1" \
     "pytest>=8.3,<10" "pytest-asyncio>=0.25,<2" "fastapi>=0.115,<1" \
     "cryptography>=44,<47" "python-dotenv>=1.0,<2" "PyYAML>=6,<7" \
     "markdown>=3.7,<4" "bleach>=6.2,<7" "openai>=1.75,<3" "anthropic>=0.52,<1"
   PYTHONPATH=src python -m pytest -q
   ```
   Jika `pip install -e .` gagal karena build backend, jangan buang waktu: pasang dependency langsung dan pakai `PYTHONPATH=src`.

5. Reproduksi dashboard bila job `dashboard` yang gagal.
   ```
   npm --prefix web ci
   npm --prefix web run build
   ```

6. Jalankan test terfokus dan baca assertion-nya.
   ```
   PYTHONPATH=src python -m pytest tests/test_<domain>.py -q
   ```
   Bedakan dua jenis kegagalan:
   - **Kode/test salah**: assertion tidak sesuai perilaku yang dijanjikan. Perbaiki test atau kode, lalu ulangi.
   - **Lingkungan lokal**: paket OS tidak ada (mis. `tzdata` di Windows), DLL diblokir policy. Jangan ubah kode untuk ini; CI Ubuntu sudah menyediakannya.

7. Perbaiki akar masalah, bukan gejalanya. Jika mengubah test, pastikan ekspektasinya cocok dengan desain: misalnya approval per unit berarti mengubah satu locale hanya memblokir locale itu. Ubah semua input bila memang ingin menguji blokir menyeluruh.

8. Jalankan ulang test terfokus, lalu seluruh suite.

9. Commit dan push.
   ```
   git -c safe.directory="$PWD" add <paths>
   git -c safe.directory="$PWD" commit -m "<type>: <perbaikan>"
   GIT_TERMINAL_PROMPT=0 git -c safe.directory="$PWD" push origin main
   ```

10. Tunggu dan verifikasi run untuk commit itu.
    ```
    python .semutcode/skills/ci-failure-triage/scripts/ci_status.py \
      --sha "$(git -c safe.directory="$PWD" rev-parse --short HEAD)" --wait
    ```

11. Laporkan: step yang gagal, akar masalah, perbaikan, dan hasil CI (nama job + conclusion). Sebutkan jika ada kegagalan lokal yang murni lingkungan.

## Pitfalls

- Filter run berdasarkan `head_sha`, bukan "run terbaru": commit lain bisa menimpa urutan.
- Jangan memakai endpoint log job tanpa token admin: hasilnya 403; gunakan daftar jobs/steps.
- Jangan menyimpulkan dari nama job saja: baca `FAILED STEP` lalu workflow untuk perintah pastinya.
- Jangan menandai kegagalan `tzdata`/DLL sebagai bug kode: itu perbedaan OS, bukan regresi.
- Jangan mengubah test agar "hijau" dengan melonggarkan invariant: perbaiki ekspektasi hanya jika perilaku kode memang benar.
- Jangan push sebelum test terfokus lulus: setiap push memicu run baru dan memperpanjang siklus.
- Jangan mengklaim CI hijau dari `compileall` atau dari status `in_progress`: tunggu `completed` + `success` pada kedua job.
- Jangan retry pipeline berulang tanpa perubahan: kegagalan deterministik butuh perbaikan kode/test.

## Verify

```
python .semutcode/skills/ci-failure-triage/scripts/ci_status.py \
  --sha "$(git -c safe.directory="$PWD" rev-parse --short HEAD)"
```

Berhasil jika exit code `0` dan output menunjukkan `RUN ... completed success` dengan `JOB backend: success` dan `JOB dashboard: success`.
