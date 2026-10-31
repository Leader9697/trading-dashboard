# TrendMarti Dual Cycle V14 Smart (EURUSD)

Versi perbaikan dari `TrendMarti_Dual_Cycle_V13_Recovery`. Tetap buka dua arah (basket buy dan
basket sell jalan bersamaan), tetap grid + martingale, tapi kerugian terburuk per basket sudah
diketahui dan dibatasi sebelum order pertama.

## Yang berubah dari V13

| | V13 | V14 |
|---|---|---|
| Lot awal | 0,01 tetap | dihitung dari equity: basket penuh yang kena hard stop = rugi `InpRiskPct` (4%) |
| Multiplier / layer | 1,5 x 12 layer | 1,3 x 6 layer (searah tren), 3 layer (melawan tren) |
| Jarak grid | 100 point tetap | 1,5 x ATR(H1,14), minimal 8 pips, x1,2 tiap layer |
| Stop per basket | tidak ada | hard stop 1 langkah grid setelah layer terakhir + pengaman uang 6% equity |
| Filter tren | tidak ada | close H1 vs EMA200: basket searah tren boleh 6 layer, lawan tren 3, tidak tambah layer kalau tren berbalik |
| Entry | RSI(6) < 40 / > 60 | searah tren RSI < 35 / > 65, lawan tren hanya RSI < 20 / > 80 |
| TP | harga rata-rata + 10 pips (2 pips mulai layer 4) | profit bersih (swap & komisi dihitung) >= 0,8 ATR, 0,25 ATR mulai layer 3 |
| DD guard | floating vs balance 40% | dari equity puncak 20%, lalu jeda 24 jam |
| Waktu | libur Desember | + tidak buka basket 20:00-01:00 UTC (rollover), Jumat setelah 18:00 UTC, opsi tutup Jumat |
| Berita | tidak ada | kalender MT5: jeda +-30 menit berita high impact USD/EUR (live saja, tidak jalan di tester) |
| Bug | `SYMBOL_FILLING_MODE` dibaca sebagai satu nilai (itu bit flag) | diperbaiki, plus cek margin sebelum order |

Status basket (step grid, lot awal, layer maks) disimpan di Global Variables, jadi aman kalau
MT5/VPS restart.

## Hasil uji (EURUSD, Jan 2023 - Sep 2026, modal $10.000)

Data: Binance EURUSDT 1 menit sebagai pengganti EURUSD (data broker tidak bisa diambil dari
server uji). Data 2020-2022 dibuang karena penuh harga palsu (puluhan ribu spike per tahun)
yang membuat grid tampak untung besar secara palsu. Biaya: spread 1,2 pips, swap, leverage
1:500. Setting V14 dipilih hanya dari 2023-2024, lalu dicek di 2025-2026 yang tidak dipakai
untuk memilih.

| Versi | Hasil | DD maks | Catatan |
|---|---|---|---|
| V13 asli, 0,01 lot, $10.000 | +15,7% | 7,3% | di akhir uji dua basket nyangkut 12 layer, floating -$1.400; 2025 sendiri -4,9% |
| V13 asli, 0,01 lot, $1.000 | +49% | 72-80% | equity sempat tinggal $286 |
| V13 asli, 0,10 lot, $10.000 | -95% | 99% | akun habis (rasio lot/modal sama dengan baris atas) |
| V14, 2023-2024 (dipakai memilih) | -7,1% | 19,8% | 24 variasi diuji, semuanya rugi; ini yang paling kecil ruginya |
| V14, 2025-2026 (data baru) | +2,8% | 7,4% | dengan spread 2x: -9,7% |
| V14 per tahun | 2023 -5,3%, 2024 +0,4%, 2025 -0,7%, 2026 +3,4% | | basket rugi terbesar -$401 |

Kesimpulan jujur: V14 tidak pernah mendekati akun habis, tapi juga belum terbukti untung.
"Profit" V13 datang dari risiko ekor yang disembunyikan: menang ~98% basket kecil-kecil, lalu
sesekali satu basket nyangkut atau kena guard dan menghapus semuanya. Begitu ekornya dipotong,
yang tersisa kira-kira impas dikurangi spread. Tidak ada setting grid yang ditemukan bisa
agresif, untung, dan aman sekaligus di EURUSD.

## Cara pakai

1. Salin `TrendMarti_Dual_Cycle_V14_Smart.mq5` ke `MQL5/Experts/`, compile di MetaEditor.
2. Pasang di chart **EURUSD M5** (RSI pakai M5, tren dan ATR pakai H1).
3. Uji dulu di Strategy Tester MT5 broker sendiri: "Every tick based on real ticks", 2023 sampai
   sekarang. Itu lebih akurat dari uji di atas.
4. Kalau mau live: akun terpisah atau akun cent, uang yang siap hilang. `InpRiskPct` 2-4%.
   Pada $10.000 dan risiko 4%, lot awal sekitar 0,03-0,05. Di bawah kira-kira $2.000 lot awal
   jatuh di bawah 0,01 dan EA tidak buka posisi (pesan muncul di chart); pakai akun cent.

Simulator: `backend/app/research_ea_grid.py`.
