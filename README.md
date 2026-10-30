# Crypto Trade Setup Dashboard

Satu layar untuk keputusan trading crypto: tabel ringkasan watchlist, grafik dengan garis entry/SL/TP, kartu trade setup, konteks derivatif, dan ringkasan debat AI (bull vs bear vs manajer risiko) yang diperbarui berkala.

## Dari mana idenya

| Sumber | Yang diambil |
| --- | --- |
| [daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis) | Format "decision dashboard": kesimpulan, skor, zona entry, stop loss, target, checklist |
| [AI-Trader](https://github.com/HKUDS/AI-Trader) / TradingAgents | Debat multi-agen: bull, bear, manajer riset, manajer risiko |
| [dexter](https://github.com/virattt/dexter) | Pola rencanakan, jalankan, validasi: angka dihitung kode, AI hanya berdebat di atas fakta itu |
| [lightweight-charts](https://github.com/tradingview/lightweight-charts) | Grafik candlestick, EMA, dan garis harga setup |
| [FinceptTerminal](https://github.com/Fincept-Corporation/FinceptTerminal) | Tidak dipakai di kode (AGPL); cocok sebagai terminal riset terpisah |

## Cara kerja

```
Binance (spot klines, funding, OI, long/short) + Fear & Greed
        │
backend FastAPI ── /api/setup      setup per koin (deterministik, tanpa LLM)
        │       ── /api/snapshot   ringkasan ~1 KB semua koin, input debat
        │       ── /api/debate     hasil debat dari Vercel Edge Config
        │
frontend React + lightweight-charts
```

Debat AI dijalankan Claude lewat tugas terjadwal: membaca `/api/snapshot`, menulis debat singkat, lalu menyimpannya ke Edge Config (key `debate`). Dashboard langsung menampilkannya tanpa redeploy. Karena input sudah diringkas jadi angka, satu putaran debat untuk 5 koin hanya butuh sedikit token.

### Aturan setup

- **Swing**: tren dari 1D, entry di 4H, risiko 1% per trade. **Scalp**: tren 1H, entry 15m, risiko 0,5%.
- **Skor bias** (-100 sampai 100): susunan EMA20/50/200 di dua timeframe (bobot 60/40), RSI, histogram MACD, funding, rasio long/short, Fear & Greed. Long jika ≥ 25, short jika ≤ -25, selain itu tunggu.
- **Entry**: pullback ke support/resisten swing terdekat atau EMA20 di timeframe entry.
- **Stop loss**: di balik struktur terdekat, dibatasi 0,8 sampai 2,5× ATR14.
- **Target**: TP1 = 2R, TP2 = 3R. Level struktur di antara entry dan TP1 ditandai sebagai hambatan.
- **Checklist**: enam syarat. Jika kurang dari 4 terpenuhi, risiko per trade dipotong setengah.

## Menjalankan lokal

```bash
cd frontend && npm install && npm run build
cd ../backend && pip install -r requirements.txt
DATA_MODE=demo uvicorn app.main:app --port 8100   # tanpa DATA_MODE = data live Binance
```

Buka http://localhost:8100. Untuk mengembangkan frontend: `npm run dev` di `frontend/` (proxy ke :8100).

## Deploy ke Vercel

Import repo di [vercel.com/new](https://vercel.com/new); pengaturan build ada di `vercel.json`. Fungsi Python berjalan di region `sin1` (Singapura) karena Binance menolak IP Amerika Serikat. Untuk panel debat, sambungkan store Edge Config ke project supaya variabel `EDGE_CONFIG` terisi.

## API

| Endpoint | Isi |
| --- | --- |
| `GET /api/config` | Watchlist, mode, status debat |
| `GET /api/candles/{pair}?interval=4h` | OHLCV + EMA20/50/200 (interval 15m, 1h, 4h, 1d) |
| `GET /api/setup/{pair}?mode=swing` | Setup lengkap: arah, skor, rating, level, checklist, poin bull/bear |
| `GET /api/snapshot?mode=swing` | Ringkasan padat semua koin |
| `GET /api/debate` | Debat AI terbaru |

## Tes

```bash
cd backend && pytest
```

Grafik memakai TradingView Lightweight Charts™ (Apache-2.0); logo atribusinya sengaja dibiarkan aktif.

Analisis riset, bukan saran keuangan.
