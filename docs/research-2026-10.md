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

## Lanjutan: filter funding, OI, long/short dan taker (aturan v3)

Data posisi diambil dari arsip resmi Binance: funding lewat API (histori penuh), sedangkan open interest, rasio long/short akun dan rasio taker diambil per 5 menit dari data.binance.vision sejak Des 2021. Setiap nilai dibaca saat candle sinyal close, jadi tidak ada data masa depan yang ikut masuk. Ada 48 varian yang diuji (`app/research_deriv.py`) memakai protokol 60/40 yang sama.

Varian terbaik in-sample adalah **v2 ditambah tiga syarat**: OI naik dalam 24 jam, rasio long/short retail tidak berada di 30% teratas (untuk long) atau 30% terbawah (untuk short) dari kisaran 90 hari, dan rata-rata taker 4 jam searah posisi.

| Periode (10 koin) | v1 | v2 | v3 |
|---|---|---|---|
| In-sample Mar 2024 – Jan 2026 | | +0,02R | +0,08R (362 trade) |
| Out-of-sample Jan – Okt 2026 | | +0,12R, PF 1,25 | **+0,21R, WR 47%, PF 1,48** (242 trade, 10/10 koin positif) |
| Walk-forward 4 periode | | 2/4 positif | **3/4 positif** (−0,06 / +0,05 / +0,37 / +0,10) |
| **Uji buta 2022 – Mar 2024** (tidak dipakai untuk memilih) | −0,01R | −0,01R | **+0,09R, PF 1,19** (576 trade) |
| ↳ 2022 / 2023 / awal 2024 | +0,03 / −0,05 / +0,05 | 0,00 / −0,09 / +0,35 | +0,03 / +0,06 / +0,49 |

Kesimpulan: v3 adalah aturan pertama yang bertahan di data yang tidak pernah dipakai untuk memilihnya, termasuk bear market 2022 dan pasar datar 2023. Edge-nya tetap tipis, sekitar +0,03 s/d +0,2R per trade dengan winrate 40–47%, dan biaya serta slippage nyata bisa menggerusnya. Karena itu statusnya **forward test**: jalankan dengan risiko kecil atau paper trading selama 4–8 minggu, lalu bandingkan hasilnya dengan angka di atas.

## Single take profit (2022 – Oct 2026, v3, 5 coins)

Same v3 entries, exit variants compared. Fees included.

| Exit | Trades | Winrate | Avg R | PF | Blind 2022–24/03 | Max DD (R) | Longest losing run |
|---|---|---|---|---|---|---|---|
| Half 2R, rest 3R | 1193 | 43.3% | +0.110 | 1.23 | +0.090 | 56.3 | 22 |
| TP 1.5R | 1327 | 45.8% | +0.077 | 1.17 | +0.023 | 62.6 | 15 |
| TP 2R | 1244 | 43.7% | +0.112 | 1.24 | +0.071 | 59.0 | 23 |
| **TP 2.5R** | 1219 | 42.0% | +0.126 | 1.26 | +0.087 | 63.6 | 23 |
| TP 3R | 1187 | 41.3% | +0.128 | 1.26 | +0.112 | 61.1 | 22 |

The dashboard now uses one TP at 2.5R. The stop is at a median of 2.9% from entry (p80 4.95%, p95 8.3%). By year, the average R per trade was +0.006 (2022), +0.055 (2023), +0.223 (2024), +0.158 (2025) and +0.204 (2026). The edge is thin in bear and sideways years.

Sizing for a small account: position = equity × risk% / SL%. Leverage only reduces the margin you post, not the risk. Keep leverage below 1/(2 × SL%) on isolated margin so that liquidation sits beyond the stop. Binance minimum order values (BTC 100 USDT, ETH 20 USDT) make BTC untradeable at 1% risk on a $55 account.

## Price only, 2017 – Sep 2026 (10 coins, Binance spot archive)

Rules v2, without the OI, crowd and taker filters, because that data doesn't exist before late 2021. Average R per trade by year:

| Year | Avg R per trade |
|---|---|
| 2017 | +0.37 (36 trades) |
| 2018 | −0.04 |
| 2019 | +0.07 |
| 2020 | +0.06 |
| 2021 | +0.23 |
| 2022 | −0.03 |
| 2023 | −0.09 |
| 2024 | +0.09 |
| 2025 | +0.09 |
| 2026 | +0.12 |

Overall: 4260 trades, winrate 42%, +0.06R per trade. On the years v2 was never fitted on (2017 – Feb 2024): +0.04R with PF 1.08. The old v1 rules made +0.02R and lost in 5 of the 10 years.

Conclusion: the system makes money in trending markets and loses in bear and sideways years. A losing year is normal for it.
