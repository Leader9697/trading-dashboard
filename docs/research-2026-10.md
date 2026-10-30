# Riset aturan sinyal (30 Okt 2026)

Data: Binance spot. Swing memakai 4h/1d dari 10 koin (BTC, ETH, SOL, BNB, XRP, DOGE, ADA, AVAX, LINK, LTC) untuk periode Mar 2024 sampai Okt 2026. Scalp memakai 15m/1h dari 5 koin selama ±7 bulan.
Biaya 0,08% per trade sudah dipotong. Exit: setengah posisi di 2R, stop lalu digeser ke entry, sisanya ke 3R. Bila SL dan TP tersentuh di bar yang sama, SL dianggap kena lebih dulu.
Sebanyak 96 varian diuji (`app/research.py`), dengan kombinasi berikut:
- entry: limit / konfirmasi / market
- filter tren BTC
- filter EMA200
- SL lebar
- skor ≥25 / ≥40
- filter volatilitas

Varian dipilih memakai 60% data awal (in-sample), lalu dinilai pada 40% sisanya (out-of-sample).

## Hasil

| | In-sample | Out-of-sample |
|---|---|---|
| Aturan lama, swing | 1602 trade, WR 35,5%, −0,06R, PF 0,91 | 1093 trade, WR 38,8%, +0,06R, PF 1,10 |
| Varian terbaik (v2), swing | 769 trade, WR 40,2%, +0,02R, PF 1,04 | 655 trade, WR 44,7%, +0,12R, PF 1,25 |
| Varian terbaik, scalp | 722 trade, −0,02R | 504 trade, −0,11R (0 dari 96 varian positif) |

Aturan v2 terdiri dari: entry market, searah tren BTC 1D (close di atas EMA50 yang naik, atau sebaliknya), |skor| ≥ 40, dan ATR% di atas median 200 bar.

Walk-forward 4 periode (masing-masing ±8 bulan), dalam rata-rata R per trade:

| | P1 | P2 | P3 | P4 |
|---|---|---|---|---|
| Aturan lama | −0,07 | −0,05 | +0,13 | −0,04 |
| v2 | −0,04 | +0,06 | +0,25 | −0,02 |

## Kesimpulan
- v2 lebih baik dari aturan lama di keempat periode, jadi v2 dipakai di dashboard.
- v2 hanya untung di 2 dari 4 periode, dan periode terakhir masih sedikit rugi. Kriteria sinyal otomatis menuntut ≥3/4 periode positif, PF ≥ 1,2 dan ≥3 koin positif, sehingga v2 **belum lolos**.
- Scalp tidak punya edge sama sekali di data ini.
- Hasil positif out-of-sample sebagian besar datang dari satu periode pasar yang kebetulan menguntungkan (P3). Itu tanda edge yang tipis dan bergantung pada kondisi pasar.

Langkah berikutnya yang paling menjanjikan adalah menambah data di luar harga, misalnya funding, OI, dan likuidasi. Histori data ini perlu dikumpulkan sendiri, karena API gratis hanya menyediakan sekitar 30 hari.
