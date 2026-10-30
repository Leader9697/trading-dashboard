# Dashboard Keputusan Trading

Satu layar untuk memutuskan beli, tahan, atau jual: grafik harga, sinyal, level stop loss dan target, alasan, risiko, dan panel riset mendalam.

Dashboard ini tidak menyalin kode repo lain. Ia memanggil masing-masing sebagai layanan terpisah lewat satu gateway:

| Komponen | Peran | Status |
| --- | --- | --- |
| [daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis) | Data harga + laporan analisis LLM (skor, aksi, stop loss, target, risiko) | Tersambung via `DSA_URL` |
| [lightweight-charts](https://github.com/tradingview/lightweight-charts) | Grafik candlestick, volume, MA20/MA50, penanda sinyal | Terpasang |
| [dexter](https://github.com/virattt/dexter) | Riset fundamental on-demand | Endpoint siap, bridge menyusul (Tahap 3) |
| [AI-Trader](https://github.com/HKUDS/AI-Trader) | Opini kedua + paper trading | Tahap 4 |

```
frontend (React + Vite + lightweight-charts)
        │  /api/*
backend gateway (FastAPI)
   ├─ harga:   daily_stock_analysis | yfinance | demo
   ├─ sinyal:  laporan daily_stock_analysis | aturan MA/RSI bawaan
   └─ riset:   dexter bridge (DEXTER_URL)
```

## Menjalankan

Butuh Python 3.11+ dan Node 20+.

```bash
# 1. Backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # isi seperlunya
uvicorn app.main:app --port 8100 --env-file .env

# 2. Frontend (terminal lain)
cd frontend
npm install
npm run dev                     # buka http://localhost:5173
```

Atau satu port saja: `npm run build` di `frontend/`, lalu backend otomatis menyajikan `frontend/dist` di http://localhost:8100.

## Tiga mode data

1. **Demo** (`DATA_MODE=demo`): data sintetis, tanpa internet dan tanpa API key. Cocok untuk mencoba tampilan.
2. **yfinance** (default jika `DSA_URL` kosong): harga asli dari Yahoo Finance, termasuk saham BEI dengan akhiran `.JK`. Sinyal memakai aturan teknikal bawaan (MA20/MA50 + RSI14), bukan LLM.
3. **daily_stock_analysis** (`DSA_URL=http://localhost:8000`, alamat server DSA): harga dan sinyal diambil dari laporan analisis LLM terbaru.

Untuk mode 3, jalankan daily_stock_analysis secara terpisah:

```bash
git clone https://github.com/ZhuLinsen/daily_stock_analysis && cd daily_stock_analysis
pip install -r requirements.txt && cp .env.example .env   # isi kunci LLM + STOCK_LIST
python main.py --stocks BBCA.JK,AAPL        # buat laporan dulu
python main.py --serve-only                 # API DSA di :8000, gateway ini di :8100
```

Catatan: jika autentikasi admin DSA diaktifkan, endpoint `/api/v1/*` butuh cookie sesi; prototipe ini mengasumsikan autentikasi mati (default). Dukungan ticker `.JK` di DSA belum diuji.

## API gateway

| Endpoint | Isi |
| --- | --- |
| `GET /api/config` | Watchlist dan sumber data aktif |
| `GET /api/candles/{ticker}?days=180` | OHLCV + garis MA20/MA50 |
| `GET /api/decision/{ticker}` | Aksi, skor, tren, alasan, risiko, level, penanda |
| `POST /api/research` | `{symbol, question}` diteruskan ke dexter |

## Tes

```bash
cd backend && pytest
```

## Lisensi pihak ketiga

Grafik memakai TradingView Lightweight Charts™ (Apache-2.0). Logo atribusi TradingView di grafik sengaja dibiarkan aktif sesuai syarat NOTICE-nya.

Alat bantu riset, bukan saran investasi.
