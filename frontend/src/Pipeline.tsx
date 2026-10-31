import { useState } from 'react';
import type { Decision, DecisionRow, LivePlan, Portfolio } from './api';

type Ready = Extract<Portfolio, { available: true }>;

const SIDE = { l: 'Long', s: 'Short', long: 'Long', short: 'Short' } as const;
const STATUS: Record<string, string> = { tp: 'kena TP', stop: 'kena SL', time: 'tutup 5 hari', timeout: 'tutup 5 hari' };

function reason(d: Decision, maxPos: number) {
  return {
    take: 'Diambil',
    coin: 'Koin belum masuk daftar tahun itu',
    busy: 'Koin ini masih punya posisi',
    slots: `${maxPos} slot sudah penuh`,
    min_order: 'Di bawah minimal order Binance',
    margin: 'Margin tidak cukup',
    unfilled: 'Limit tidak terisi dalam 16 jam',
  }[d];
}

function base(s: string) {
  return s.replace(/USDT$/, '');
}

function when(t: number) {
  return new Date(t * 1000).toLocaleString('id-ID', { day: 'numeric', month: 'short', year: '2-digit', hour: '2-digit', minute: '2-digit' });
}

/** "-1553 koin masih buka, -574 slot penuh", leaving out steps that never fired. */
function cuts(parts: [number | undefined, string][]) {
  const out = parts.filter(([n]) => n).map(([n, label]) => `-${n} ${label}`);
  return out.length ? out.join(', ') : 'Tidak ada yang gugur';
}

function usd(n: number) {
  return `${n < 0 ? '-' : n > 0 ? '+' : ''}$${Math.abs(n).toFixed(2)}`;
}

type LiveStep = 'none' | 'slots' | 'min_order' | 'take';

/** This candle's decision for every listed coin, run through the same steps as the
 *  replay. Open positions aren't known to the dashboard, so slots count from empty. */
function liveDecisions(live: LivePlan[], maxPos: number) {
  const rows = live.filter((l) => !l.error).map((l) => {
    let step: LiveStep = 'none';
    if (l.active) step = l.fits === false ? 'min_order' : 'take';
    return { l, step: step as LiveStep };
  });
  const order = (a: LivePlan, b: LivePlan) => (b.score ?? 0) - (a.score ?? 0);
  rows.filter((r) => r.step === 'take').sort((a, b) => order(a.l, b.l))
    .forEach((r, i) => { if (i >= maxPos) r.step = 'slots'; });
  return rows;
}

function DecisionTable({ rows, maxPos }: { rows: ReturnType<typeof liveDecisions>; maxPos: number }) {
  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th>Koin</th><th>Keputusan</th><th>Alasan</th></tr></thead>
        <tbody>
          {rows.map(({ l, step }) => (
            <tr key={l.symbol}>
              <td><strong>{base(l.symbol)}</strong>{l.side && <small> {SIDE[l.side]}</small>}</td>
              <td>{step === 'take' ? <span className="pill up">Ambil</span> : <span className="muted small">Lewati</span>}</td>
              <td className="small">
                {step === 'take' && `Semua ${l.checks} syarat lolos, order muat`}
                {step === 'min_order' && `Posisi di bawah minimal order Binance (${l.min_notional} USDT)`}
                {step === 'slots' && `${maxPos} slot sudah terisi sinyal yang skornya lebih tinggi`}
                {step === 'none' && `${l.checks_ok}/${l.checks} syarat. Gagal: ${(l.failed ?? []).join('; ') || '–'}`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function PipelinePanel({ p }: { p: Ready }) {
  const st = p.settings;
  const dec = p.decisions;
  const sk = p.result.skipped;
  const live = liveDecisions(p.live, st.max_pos);
  const count = (s: LiveStep) => live.filter((r) => r.step === s).length;
  const signals = live.length - count('none');
  const risk = (p.equity * st.risk_pct) / 100;
  const from = new Date(p.result.curve[0][0] * 1000).getUTCFullYear();
  const [all, setAll] = useState(false);
  const waiting = live.filter((r) => r.step === 'none');

  const steps: { name: string; does: string; now: string; past: string; tone?: string }[] = [
    {
      name: 'Scanner',
      does: `Cek ${p.recommended.length} koin tiap candle 4 jam tutup`,
      now: `${signals} sinyal v3 dari ${live.length} koin`,
      past: dec ? `${dec.signals} sinyal di koin pilihan sejak ${from}` : '–',
    },
    {
      name: 'Filter',
      does: `Satu posisi per koin, maks ${st.max_pos} posisi`,
      now: !signals ? 'Tidak ada sinyal' : count('slots') ? `${count('slots')} lewat, slot penuh` : 'Semua lolos (anggap akun kosong)',
      past: sk ? cuts([[sk.busy, 'koin masih buka'], [sk.slots, 'slot penuh']]) : '–',
    },
    {
      name: 'Sizer',
      does: `Risiko ${st.risk_pct}% ($${risk.toFixed(2)}), dibulatkan ke lot Binance`,
      now: !signals ? 'Tidak ada sinyal' : count('min_order') ? `${count('min_order')} lewat, di bawah minimal` : 'Semua muat',
      past: sk ? cuts([[sk.min_order, 'di bawah minimal'], [sk.margin, 'margin kurang']]) : '–',
    },
    {
      name: 'Executor',
      does: 'Order limit 0,25 ATR, batal kalau 16 jam tidak terisi',
      now: `${count('take')} order siap dipasang`,
      past: sk ? `-${sk.unfilled} tidak terisi, ${p.result.trades} terisi` : '–',
    },
    {
      name: 'Logger',
      does: 'Setiap keputusan dicatat beserta alasannya',
      now: `${live.length} keputusan candle ini`,
      past: `${p.result.trades} trade, winrate ${p.result.wr ?? '–'}%`,
    },
    {
      name: 'Equity',
      does: 'Untung rugi dibukukan saat posisi tutup',
      now: `Modal $${p.live_equity}`,
      past: `$${p.equity} jadi $${p.result.final.toFixed(2)}, turun terdalam -${p.result.max_dd_pct}%`,
      tone: p.result.final >= p.equity ? 'up' : 'down',
    },
  ];

  const log = dec ? [...dec.log].reverse() : [];
  const shown = all ? log : log.slice(0, 12);

  return (
    <section className="panel pipeline wide-panel">
      <div className="panel-head">
        <h2>Alur keputusan engine<small>tiap sinyal lewat 6 langkah; yang gugur dicatat alasannya</small></h2>
      </div>

      <ol className="strip flow">
        {steps.map((s, i) => (
          <li key={s.name}>
            <span className="flow-name">{i + 1}. {s.name}</span>
            <span className="flow-does">{s.does}</span>
            <strong>{s.now}</strong>
            <small className={s.tone}>{s.past}</small>
          </li>
        ))}
      </ol>
      <p className="muted small">Baris tebal: candle 4 jam terakhir. Baris kecil: simulasi akun ${p.equity} sejak {from}.</p>

      <h3>Keputusan candle ini</h3>
      {signals === 0 && <p className="muted small">Tidak ada koin yang memenuhi semua {live[0]?.l.checks ?? 7} syarat, jadi engine tidak memasang order.</p>}
      {signals > 0 && <DecisionTable rows={live.filter((r) => r.step !== 'none')} maxPos={st.max_pos} />}
      {waiting.length > 0 && (
        <details className="coins">
          <summary>Alasan {waiting.length} koin dilewati</summary>
          <DecisionTable rows={waiting} maxPos={st.max_pos} />
        </details>
      )}

      {dec && (
        <>
          <h3>Catatan keputusan simulasi, {dec.log.length} terakhir</h3>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Sinyal</th><th>Koin</th><th>Keputusan</th><th>Alasan</th><th className="num">Posisi buka</th><th className="num">Saldo</th><th className="num">Hasil</th></tr></thead>
              <tbody>
                {shown.map((row: DecisionRow) => (
                  <tr key={`${row.s}-${row.t}`}>
                    <td>{when(row.t)}</td>
                    <td><strong>{base(row.s)}</strong> <small>{SIDE[row.side]}</small></td>
                    <td>{row.d === 'take' ? <span className="pill up">Ambil</span> : <span className="muted small">Lewati</span>}</td>
                    <td className="small">{row.d === 'take' ? `Order $${row.notional?.toFixed(2)}${row.status ? `, ${STATUS[row.status] ?? row.status}` : ''}` : reason(row.d, st.max_pos)}</td>
                    <td className="num">{row.open}/{st.max_pos}</td>
                    <td className="num">${row.eq.toFixed(2)}</td>
                    <td className={`num ${row.pnl === undefined ? '' : row.pnl > 0 ? 'up' : 'down'}`}>
                      {row.pnl === undefined ? '–' : <>{usd(row.pnl)} <small>{row.r! > 0 ? '+' : ''}{row.r!.toFixed(2)}R</small></>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {log.length > shown.length && (
            <button type="button" className="link" onClick={() => setAll(true)}>Tampilkan {log.length - shown.length} lagi</button>
          )}
        </>
      )}
    </section>
  );
}
