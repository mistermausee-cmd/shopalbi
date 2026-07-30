'use strict';

/* shopalbi frontend — vanilla JS, no build step.
 *
 * Four views share one table renderer. Each view declares its columns; a column
 * may be server-sorted (`key` matches an API sort key) or client-sorted (it also
 * provides `value`). The previous release had a bug where the plan tab read the
 * cities tab's sort state, so clicking its headers did nothing — sort state is
 * now always keyed by the active view.
 */

// ---------------------------------------------------------------- helpers

const $ = (s) => document.querySelector(s);
const nf = new Intl.NumberFormat('ru-RU');
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const fmt = (n) => (n === null || n === undefined || n === '' ? '—' : nf.format(Math.round(n)));
const signed = (n) => (n === null || n === undefined ? '—' : (n >= 0 ? '+' : '') + nf.format(Math.round(n)));
const pct = (n) => (n === null || n === undefined ? '—' : (n >= 0 ? '+' : '') + n + '%');

function compact(n) {
  const a = Math.abs(n || 0);
  if (a >= 1e9) return (n / 1e9).toFixed(2) + ' млрд';
  if (a >= 1e6) return (n / 1e6).toFixed(2) + ' млн';
  if (a >= 1e4) return Math.round(n / 1e3) + 'k';
  return fmt(n);
}

const CITY_RU = {
  'Bridgewatch': 'Бриджуотч', 'Fort Sterling': 'Форт-Стерлинг', 'Lymhurst': 'Лимхёрст',
  'Martlock': 'Мартлок', 'Thetford': 'Тетфорд', 'Caerleon': 'Карлеон',
  'Brecilien': 'Бресилиан', 'Black Market': 'Чёрный рынок',
};
const cityRu = (c) => CITY_RU[c] || c || '—';

// Prepositional case, so headings read "в Бресилиане" rather than "в Бресилиан".
const CITY_IN = {
  'Bridgewatch': 'Бриджуотче', 'Fort Sterling': 'Форт-Стерлинге', 'Lymhurst': 'Лимхёрсте',
  'Martlock': 'Мартлоке', 'Thetford': 'Тетфорде', 'Caerleon': 'Карлеоне',
  'Brecilien': 'Бресилиане', 'Black Market': 'Чёрном рынке',
};
const cityIn = (c) => CITY_IN[c] || cityRu(c);

// Quality names, filled from /api/meta so the backend stays the single source
// of truth. Fallback matches the in-game RU client wording.
const QUALITY_RU = {
  1: 'Обычное', 2: 'Хорошее', 3: 'Выдающееся', 4: 'Отличное', 5: 'Шедевральное',
};
const qualityRu = (q) => QUALITY_RU[q] || ('q' + q);
const WIN_RU = { day: 'день', '3d': '3 дня', week: 'неделю', month: 'месяц', quarter: '90 дней' };

// "2026-07-23..2026-07-29" -> "23.07 – 29.07". Shown instead of a vague
// "last N days" because the aggregates are a stored snapshot: shortly after UTC
// midnight they still describe yesterday's range, and the exact days matter when
// you are deciding whether to trust an average.
function rangeRu(range) {
  if (!range || range.indexOf('..') < 0) return '';
  const [a, b] = range.split('..');
  const d = (s) => (s && s.length >= 10 ? s.slice(8, 10) + '.' + s.slice(5, 7) : s);
  return a === b ? d(a) : `${d(a)} – ${d(b)}`;
}

function timeAgo(iso) {
  if (!iso) return 'нет данных';
  const d = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (d < 60) return 'только что';
  if (d < 3600) return Math.floor(d / 60) + ' мин назад';
  if (d < 86400) return Math.floor(d / 3600) + ' ч назад';
  return Math.floor(d / 86400) + ' д назад';
}
function hours(h) {
  if (h === null || h === undefined) return '—';
  if (h < 1) return '~' + Math.round(h * 60) + ' мин';
  if (h < 48) return '~' + h.toFixed(1) + ' ч';
  return '~' + (h / 24).toFixed(1) + ' дн';
}
function scoreColor(v) {
  if (v >= 68) return 'var(--green)';
  if (v >= 45) return 'var(--amber)';
  return 'var(--red)';
}
function toast(msg, kind) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast ' + (kind || '');
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.hidden = true; }, 4000);
}
function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
async function fetchJSON(url, timeoutMs) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs || 30000);
  try {
    const res = await fetch(url, { signal: ctrl.signal, headers: { 'Accept': 'application/json' } });
    if (!res.ok) {
      let detail = 'HTTP ' + res.status;
      try { const j = await res.json(); if (j.detail) detail = j.detail; } catch (e) { /* ignore */ }
      throw new Error(detail);
    }
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

// ---------------------------------------------------------------- state

const state = {
  view: 'flips',
  window: 'day',
  meta: null,
  cities: [],
  rows: [],
  payload: null,
  loading: false,
  sort: {
    flips:  { key: 'opportunity', dir: 'desc' },
    plan:   { key: 'ev_profit',   dir: 'desc' },
    cities: { key: 'score',       dir: 'desc' },
    stats:  { key: 'bm_volume',   dir: 'desc' },
  },
  // Composite ranking: several criteria at once instead of one sort column.
  rankMode: false,
  rank: [],
  // «Мои сеты»
  sets: [],
  setId: null,
  setLines: [],
  setPrice: null,
};

// Criteria the server can rank by jointly, with how to read each one.
const RANK_LABELS = {
  profit: 'Прибыль/шт', profit_pct: 'Прибыль %', ev_unit: 'С учётом риска',
  ev_pct: '% с учётом риска', opportunity: 'Оценка', throughput: 'Потенциал/день',
  reliability: 'Надёжность', bm_volume: 'ЧР шт/день', available: 'Доступно',
  profit_vwap: 'По средней ЧР', bm_trend: 'Тренд',
  absorb: 'Выкуп быстрее', buy_price: 'Цена ниже',
};

// Columns where the first click should sort ascending (cheaper / sooner / A-Z).
const ASC_FIRST = new Set(['name', 'buy_city', 'city', 'best_item', 'buy_price',
  'absorb', 'cheapest', 'unit_price', 'avg_price', 'trip']);
// Views sorted in the browser (their endpoints return the full set already).
const CLIENT_SORTED = new Set(['plan', 'cities']);

// ---------------------------------------------------------------- cells

function nameCell(r) {
  const pills = [`<span class="pill tier">${esc(r.tier_ench)}</span>`];
  if (r.category_label) pills.push(`<span class="pill cat">${esc(r.category_label)}</span>`);
  if (r.quality_upsell) {
    const target = qualityRu(r.bm_quality);
    pills.push(`<span class="pill up" title="Покупаешь «${esc(qualityRu(r.quality))}», а продавать надо в ордер «${esc(target)}» — он платит больше. Ордер выкупа принимает предметы своего качества И ВЫШЕ, а Чёрный рынок оценивает каждое качество отдельно, поэтому за низкое нередко даёт больше. В игре открой вкладку поиска и выбери качество «${esc(target)}».">→ продать как «${esc(target)}»</span>`);
  }
  if (r.spike) {
    pills.push('<span class="pill spike" title="Текущая ставка ЧР сильно выше средней за период — вероятен разовый скачок, который исчезнет, пока ты едешь.">скачок</span>');
  }
  if (r.source === 'live') pills.push('<span class="pill live" title="Количество посчитано по реальному живому стакану">live</span>');
  if (r.source === 'est') pills.push('<span class="pill est" title="Живой глубины по этому предмету нет — количество ограничено сверху">оценка</span>');
  let qtxt = esc(r.quality_label);
  if (r.also_qualities && r.also_qualities.length) {
    const also = r.also_qualities.map(qualityRu).join(', ');
    qtxt += ` <span class="dimc" title="По этой же цене на рынке доступно и это качество — сделка получается та же.">(также ${esc(also)})</span>`;
  }
  return `<div class="item-name" title="${esc(r.item_id)}"><span class="q q${r.quality}" title="Качество: ${esc(r.quality_label)}"></span>${esc(r.name)}</div>`
       + `<div class="item-sub">${pills.join('')} <span>${qtxt}</span></div>`;
}
const money = (v, cls) => `<span class="num ${cls || ''}">${fmt(v)}</span>`;
const profitCell = (v) => `<span class="num ${v >= 0 ? 'pos' : 'neg'}">${signed(v)}</span>`;
const pctCell = (v) => `<span class="num ${v >= 0 ? 'pos' : 'neg'}">${pct(v)}</span>`;
function scoreCell(v) {
  const c = scoreColor(v);
  return `<div class="score"><span class="score-bar"><i style="width:${Math.max(0, Math.min(100, v))}%;background:${c}"></i></span>`
       + `<span class="score-val" style="color:${c}">${v}</span></div>`;
}

// ---------------------------------------------------------------- columns

const H = {
  item: 'Цветная точка — качество предмета: Обычное, Хорошее, Незаурядное, Отличное, Шедевральное. '
      + 'Бейдж «Т6.2» — тир и уровень зачарования (T6, зачарование 2). '
      + 'Значок «→ продать как» означает, что Чёрный рынок платит больше за ордер более низкого '
      + 'качества, а такой ордер принимает твоё качество и выше — продавать нужно именно в него.',
  opportunity: 'Итоговая оценка = ожидаемая прибыль с единицы после риска ганка × надёжность/100. Именно по ней сортируется список по умолчанию.',
  throughput: 'Оценка × сколько штук в день реально съедает Чёрный рынок. Показывает, что стоит возить потоком, а не разово.',
  profit: 'Чистая прибыль с 1 шт: ставка ЧР × 0.96 − цена покупки. 0.96 = минус налог 4% с Premium. Сбора за размещение нет: и покупка из ордера продажи, и мгновенная продажа в ордер выкупа его не платят.',
  ev: 'Ожидаемая ценность с учётом риска потерять груз: (1 − p) × выручка − затраты, где p — шанс ганка по дороге. Ползунок риска в «⚙ Модель».',
  breakeven: 'При каком проценте ганков этот флип выходит в ноль: 1 − затраты/выручка. Чем больше — тем безопаснее.',
  bmPrice: 'Текущая ставка выкупа Чёрного рынка. Стрелка «→ qN» значит, что продавать надо в ордер качества N (он принимает твоё качество и выше и платит больше).',
  vwap: 'Средневзвешенная по объёму цена ЧР за выбранный период. Если текущая ставка сильно выше — это скачок, а не норма.',
  vol: 'Сколько единиц Чёрный рынок реально выкупил в среднем за день по истории сделок.',
  avail: 'Реально доступно по живому стакану: минимум из «сколько продают в городе» и «сколько готов купить ЧР». Количество уценено по возрасту ордера: свежий считается полностью, у старого учитывается только часть — чтобы не обещать то, что уже купили. Прочерк — живых данных по предмету пока нет.',
  rel: 'Надёжность 0–100: свежесть цен (30%), ликвидность ЧР (22%), совпадение ставки с историей (25%), живая глубина (13%), конкуренция чужих sell-ордеров на ЧР (10%).',
  absorb: 'Сколько примерно времени ЧР съедает 1 шт при текущем обороте (24ч / шт в день). Мгновенная продажа в ордер происходит сразу — это оценка глубины спроса.',
  trend: 'Куда идёт цена ЧР: средняя за сутки против средней за выбранный период.',
  ask: 'Самый дешёвый чужой sell-ордер, уже стоящий на ЧР. Если он ниже ставки выкупа — этих продавцов обслужат раньше тебя.',
};

function rankCell(r) {
  if (r.rank_score == null) return '<span class="dimc">—</span>';
  const parts = Object.entries(r.rank_parts || {})
    .map(([k, v]) => `${RANK_LABELS[k] || k}: ${v}`).join('\n');
  const c = scoreColor(r.rank_score);
  return `<div class="score" title="${esc('Процентиль по каждому показателю:\n' + parts)}">`
       + `<span class="score-bar"><i style="width:${r.rank_score}%;background:${c}"></i></span>`
       + `<span class="score-val" style="color:${c}">${r.rank_score}</span></div>`;
}

function colsFlips() {
  const cols = [
    { key: 'name', label: 'Предмет', left: true, stick: true, hint: H.item, render: nameCell,
      csv: (r) => `${r.name} ${r.tier_ench} ${qualityRu(r.quality)}` },
    { key: 'opportunity', label: 'Оценка', hint: H.opportunity,
      render: (r) => `<span class="num big" style="color:var(--gold)">${fmt(r.opportunity)}</span>`,
      csv: (r) => r.opportunity },
    { key: 'buy_city', label: 'Купить в', left: true,
      render: (r) => `<span class="city">${esc(cityRu(r.buy_city))}</span>`
        + `<span class="sub2">риск ${r.gank_rate}% · ${hours(r.buy_age_h)} назад</span>`,
      csv: (r) => cityRu(r.buy_city) },
    { key: 'buy_price', label: 'Цена закупки', render: (r) => money(r.buy_price), csv: (r) => r.buy_price },
    { key: 'bm_price', label: 'Ставка ЧР', hint: H.bmPrice,
      render: (r) => money(r.bm_price, 'big') + `<span class="sub2">${hours(r.bm_age_h)} назад</span>`,
      csv: (r) => r.bm_price },
    { key: 'profit', label: 'Прибыль/шт', hint: H.profit, render: (r) => profitCell(r.profit), csv: (r) => r.profit },
    { key: 'profit_pct', label: '%', hint: H.profit, render: (r) => pctCell(r.profit_pct), csv: (r) => r.profit_pct },
    { key: 'ev_unit', label: 'С учётом риска', hint: H.ev,
      render: (r) => profitCell(r.ev_unit) + `<span class="sub2">б/у при ${r.breakeven_gank_pct}%</span>`,
      csv: (r) => r.ev_unit },
    { key: 'profit_vwap', label: 'По средней ЧР', hint: H.vwap,
      render: (r) => profitCell(r.profit_vwap) + `<span class="sub2">${fmt(r.bm_vwap)}</span>`,
      csv: (r) => r.profit_vwap },
    { key: 'bm_volume', label: 'ЧР шт/день', hint: H.vol,
      render: (r) => `<span class="num dimc">${r.bm_daily_volume}</span>`, csv: (r) => r.bm_daily_volume },
    { key: 'available', label: 'Доступно', hint: H.avail,
      render: (r) => r.available == null
        ? '<span class="dimc">—</span>'
        : `<span class="num">${fmt(r.available)}</span><span class="sub2">город ${fmt(r.avail_city)} / ЧР ${fmt(r.avail_bm)}`
          + (r.depth_age_h != null ? ` · ${hours(r.depth_age_h)}` : '') + '</span>',
      csv: (r) => (r.available == null ? '' : r.available) },
    { key: 'bm_trend', label: 'Тренд', hint: H.trend,
      render: (r) => `<span class="num ${r.bm_trend_pct >= 0 ? 'pos' : 'neg'}">${pct(r.bm_trend_pct)}</span>`,
      csv: (r) => r.bm_trend_pct },
    { key: 'absorb', label: 'Выкуп 1 шт', hint: H.absorb,
      render: (r) => `<span class="dimc">${hours(r.est_absorb_h)}</span>`, csv: (r) => r.est_absorb_h },
    { key: 'reliability', label: 'Надёжность', hint: H.rel,
      render: (r) => scoreCell(r.reliability), csv: (r) => r.reliability },
    { key: 'throughput', label: 'Потенциал/день', hint: H.throughput,
      render: (r) => `<span class="num dimc">${compact(r.throughput)}</span>`, csv: (r) => r.throughput },
  ];
  // When criteria are active, show the resulting score right after the name so
  // it is obvious what the list is ordered by.
  if (state.rank.length) {
    cols.splice(1, 0, {
      key: 'rank_score', label: 'Отбор',
      hint: 'Совокупная оценка по выбранным показателям. Для каждого считается процентиль'
          + ' среди ВСЕХ подходящих позиций (устойчиво к выбросам), затем берётся среднее.'
          + ' 100 — лучший по всем выбранным показателям одновременно.',
      render: rankCell, csv: (r) => r.rank_score,
    });
  }
  return cols;
}

function colsPlan() {
  const cols = [
    { key: 'name', label: 'Предмет', left: true, stick: true, value: (r) => r.name, render: nameCell,
      csv: (r) => `${r.name} ${r.tier_ench} ${qualityRu(r.quality)}` },
    { key: 'qty', label: 'Купить, шт', value: (r) => r.qty,
      hint: 'Сколько штук брать. При наличии живого стакана количество ограничено реальными ордерами: и предложением города, и спросом ЧР.',
      render: (r) => `<span class="num big" style="color:var(--gold)">${fmt(r.qty)}</span>`
        + (r.available != null ? `<span class="sub2">из ${fmt(r.available)} доступных</span>` : ''),
      csv: (r) => r.qty },
    { key: 'unit_price', label: 'Цена от', value: (r) => r.unit_price,
      hint: 'Самый дешёвый лот. Дальше цена растёт — смотри «средняя».',
      render: (r) => money(r.unit_price) + (r.max_price > r.unit_price ? `<span class="sub2">до ${fmt(r.max_price)}</span>` : ''),
      csv: (r) => r.unit_price },
    { key: 'avg_price', label: 'Средняя', value: (r) => r.avg_price,
      hint: 'Средняя цена за штуку, если скупить всё рекомендованное количество по возрастающим лотам.',
      render: (r) => money(r.avg_price, 'big'), csv: (r) => r.avg_price },
    { key: 'avg_sell', label: 'Продажа ЧР', value: (r) => r.avg_sell,
      hint: 'Средняя цена, по которой уйдёт ВСЁ это количество — по ней и считается прибыль.'
          + ' Верхняя ставка ЧР покрывает лишь несколько штук (на живых данных бывает 1–3% спроса),'
          + ' остальное выкупают ордера подешевле. Максимальная ставка показана под средней.'
          + ' Если они равны — всё количество уходит по лучшей цене.',
      render: (r) => `<span class="num big" style="color:var(--gold)">${fmt(r.avg_sell)}</span>`
        + `<span class="sub2">макс. ${fmt(r.bm_buy_now)}`
        + (r.quality_upsell
            ? ` · ордер «${esc(qualityRu(r.bm_quality))}»`
            : '') + '</span>',
      csv: (r) => r.avg_sell },
    { key: 'total_cost', label: 'Затраты', value: (r) => r.total_cost,
      render: (r) => money(r.total_cost), csv: (r) => r.total_cost },
    { key: 'total_profit', label: 'Прибыль', value: (r) => r.total_profit,
      render: (r) => profitCell(r.total_profit), csv: (r) => r.total_profit },
    { key: 'ev_profit', label: 'С учётом риска', value: (r) => r.ev_profit, hint: H.ev,
      render: (r) => profitCell(r.ev_profit), csv: (r) => r.ev_profit },
    { key: 'profit_pct', label: '%', value: (r) => r.profit_pct,
      render: (r) => pctCell(r.profit_pct), csv: (r) => r.profit_pct },
    { key: 'bm_daily_volume', label: 'ЧР шт/день', value: (r) => r.bm_daily_volume, hint: H.vol,
      render: (r) => `<span class="num dimc">${r.bm_daily_volume}</span>`, csv: (r) => r.bm_daily_volume },
    { key: 'absorb_h', label: 'Выкупят за', value: (r) => (r.absorb_h == null ? Infinity : r.absorb_h),
      hint: 'Сколько времени ЧР будет съедать именно это количество при текущем обороте.',
      render: (r) => `<span class="dimc">${hours(r.absorb_h)}</span>`, csv: (r) => r.absorb_h },
    { key: 'reliability', label: 'Надёжность', value: (r) => r.reliability, hint: H.rel,
      render: (r) => scoreCell(r.reliability), csv: (r) => r.reliability },
  ];
  if (state.rank.length) cols.splice(1, 0, {
    key: 'rank_score', label: 'Отбор', render: rankCell, csv: (r) => r.rank_score,
  });
  return cols;
}

function colsCities() {
  const cols = [
    { key: 'city', label: 'Город', left: true, stick: true, value: (r) => cityRu(r.city),
      render: (r) => `<div class="item-name"><span class="city" style="font-size:14px">${esc(cityRu(r.city))}</span></div>`
        + `<div class="item-sub"><span class="pill ${r.gank_rate > 0 ? 'risk' : 'safe'}" title="Шанс потерять груз по дороге в Карлеон">риск ${r.gank_rate}%</span>`
        + `<span>рейс ~${r.trip_hours} ч</span></div>`,
      csv: (r) => cityRu(r.city) },
    { key: 'score', label: 'Сумма оценок', value: (r) => r.score,
      hint: 'Сумма итоговых оценок по лучшим 100 флипам города. Показывает, где в целом больше выгодных вещей, а не одна удачная позиция.',
      render: (r) => `<span class="num big" style="color:var(--gold)">${compact(r.score)}</span>`, csv: (r) => r.score },
    { key: 'flips_count', label: 'Флипов', value: (r) => r.flips_count,
      render: (r) => `<span class="num">${fmt(r.flips_count)}</span>`, csv: (r) => r.flips_count },
    { key: 'profit_sum', label: 'Прибыль топ-100', value: (r) => r.profit_sum,
      hint: 'Суммарная прибыль с 1 шт по каждому из лучших 100 флипов города.',
      render: (r) => profitCell(r.profit_sum), csv: (r) => r.profit_sum },
    { key: 'ev_sum', label: 'То же с риском', value: (r) => r.ev_sum, hint: H.ev,
      render: (r) => profitCell(r.ev_sum), csv: (r) => r.ev_sum },
    { key: 'avg_profit_pct', label: 'Средний %', value: (r) => r.avg_profit_pct,
      render: (r) => pctCell(r.avg_profit_pct), csv: (r) => r.avg_profit_pct },
    { key: 'avg_ev_pct', label: 'Средний % с риском', value: (r) => r.avg_ev_pct,
      render: (r) => pctCell(r.avg_ev_pct), csv: (r) => r.avg_ev_pct },
    { key: 'avg_reliability', label: 'Ср. надёжность', value: (r) => r.avg_reliability,
      render: (r) => scoreCell(r.avg_reliability), csv: (r) => r.avg_reliability },
    { key: 'best_item', label: 'Лучшая позиция', left: true, value: (r) => r.best_item,
      render: (r) => r.best_item
        ? `<div class="item-name">${esc(r.best_item)}</div><div class="item-sub">`
          + `<span class="pill tier">${esc(r.best_tier_ench)}</span>`
          + `<span class="pos">${signed(r.best_profit)} (${r.best_profit_pct}%)</span></div>`
        : '<span class="dimc">—</span>',
      csv: (r) => r.best_item },
  ];
  if (state.rank.length) cols.splice(1, 0, {
    key: 'rank_score', label: 'Отбор', render: rankCell, csv: (r) => r.rank_score,
  });
  return cols;
}

function colsStats() {
  const cols = [
    { key: 'name', label: 'Предмет', left: true, stick: true, render: nameCell,
      csv: (r) => `${r.name} ${r.tier_ench} ${qualityRu(r.quality)}` },
    { key: 'bm_avg', label: 'ЧР средняя', hint: H.vwap,
      render: (r) => money(r.bm_avg, 'big'), csv: (r) => r.bm_avg },
    { key: 'bm_now', label: 'ЧР сейчас', hint: 'Текущая ставка выкупа ЧР для этого качества.',
      render: (r) => `<span class="num" style="color:var(--gold)">${fmt(r.bm_now)}</span>`, csv: (r) => r.bm_now },
    { key: 'bm_volume', label: 'Оборот ЧР', hint: 'Всего единиц выкуплено за период.',
      render: (r) => `<span class="num dimc">${fmt(r.bm_volume)}</span><span class="sub2">${r.bm_daily}/день</span>`,
      csv: (r) => r.bm_volume },
    { key: 'spread_pct', label: 'Спред', hint: 'Историческая наценка: средняя ЧР × 0.96 против средней цены в самом дешёвом городе. Скрининг, а не готовая сделка.',
      render: (r) => pctCell(r.spread_pct), csv: (r) => r.spread_pct },
  ];
  for (const c of state.cities) {
    cols.push({
      key: 'city:' + c, label: cityRu(c),
      render: (r) => {
        const e = r.cities[c];
        if (!e || !e.avg) return '<span class="dimc">—</span>';
        const cheapest = r.cheapest_city === c;
        return `<span class="num" ${cheapest ? 'style="color:var(--green);font-weight:700"' : ''}>${fmt(e.avg)}</span>`
             + `<span class="sub2">${e.daily}/день</span>`;
      },
      csv: (r) => (r.cities[c] ? r.cities[c].avg : ''),
    });
  }
  cols.push({
    key: 'cheapest', label: 'Дешевле всего', left: true,
    render: (r) => r.cheapest_city
      ? `<span class="city">${esc(cityRu(r.cheapest_city))}</span> <span class="num dimc">${fmt(r.cheapest_avg)}</span>`
      : '<span class="dimc">—</span>',
    csv: (r) => cityRu(r.cheapest_city),
  });
  if (state.rank.length) cols.splice(1, 0, {
    key: 'rank_score', label: 'Отбор', render: rankCell, csv: (r) => r.rank_score,
  });
  return cols;
}

function buildColumns() {
  if (state.view === 'flips') return colsFlips();
  if (state.view === 'plan') return colsPlan();
  if (state.view === 'cities') return colsCities();
  return colsStats();
}

// ---------------------------------------------------------------- render

const rankable = (key) => state.view !== 'sets' && key in rankableKeys();

// Which columns are rankable depends on the view: flips has 13, but plan, cities
// and stats each show different metrics. Columns declared with a `value` (the
// client-sorted ones) are automatically rankable; server-sorted ones need to be
// in RANK_LABELS already. The logic below unifies both.
function rankableKeys() {
  const cols = buildColumns();
  const out = {};
  for (const c of cols) {
    // Any column with a key in RANK_LABELS is server-rankable (flips)
    if (c.key in RANK_LABELS) { out[c.key] = RANK_LABELS[c.key]; continue; }
    // Client-sorted columns on plan/cities/stats are also rankable
    if (c.value && c.key !== 'name' && c.key !== 'city' && c.key !== 'buy_city') {
      out[c.key] = c.label;
    }
  }
  return out;
}

function renderHead(cols) {
  const s = state.sort[state.view];
  const on = state.rankMode && state.view !== 'sets';
  const rk = rankableKeys();
  $('#thead').innerHTML = '<tr>' + cols.map((c) => {
    const inRank = state.rank.indexOf(c.key) >= 0;
    const sorted = !state.rank.length && c.key === s.key;
    const isRankable = on && (c.key in rk);
    const cls = [c.left ? 'left' : '', c.stick ? 'stick' : '', sorted ? 'sorted' : '',
      inRank ? 'ranked' : '', isRankable ? 'rankable' : ''].filter(Boolean).join(' ');
    let mark;
    if (inRank) mark = '<span class="arrow">✦</span>';
    else if (sorted) mark = `<span class="arrow">${s.dir === 'asc' ? '▲' : '▼'}</span>`;
    else if (isRankable) mark = '<span class="arrow" style="opacity:.35">+</span>';
    else mark = '<span class="arrow" style="opacity:.22">▽</span>';
    let hint = c.hint || '';
    if (isRankable) {
      hint = (inRank ? 'В отборе. Клик — убрать. ' : 'Клик — добавить в отбор. ') + hint;
    }
    const t = hint ? ` title="${esc(hint)}"` : '';
    return `<th class="${cls}" data-sort="${esc(c.key)}"${t}>${esc(c.label)}${mark}</th>`;
  }).join('') + '</tr>';
  $('#thead').querySelectorAll('th[data-sort]').forEach((th) => {
    th.addEventListener('click', () => {
      if (state.rankMode && rankable(th.dataset.sort)) toggleRank(th.dataset.sort);
      else onSort(th.dataset.sort);
    });
  });
}

// Toggle a criterion in/out of the composite. Clicking the same column again
// removes it, which is the whole point: the filter is undone the same way it
// was applied.
function toggleRank(key) {
  const i = state.rank.indexOf(key);
  if (i >= 0) state.rank.splice(i, 1);
  else state.rank.push(key);
  renderRankBar();
  if (state.cols) renderHead(state.cols);
  load();
}

function renderRankBar() {
  const bar = $('#rankBar');
  bar.hidden = !(state.rankMode && state.view !== 'sets');
  $('#rankBtn').classList.toggle('on', state.rankMode && state.view !== 'sets');
  if (bar.hidden) return;
  const chips = $('#rankChips');
  const rk = rankableKeys();
  if (!state.rank.length) {
    chips.innerHTML = '';
    $('#rankHint').textContent =
      'Кликай по заголовкам столбцов — они складываются в общий отбор. Клик по тому же столбцу снимает его.';
    return;
  }
  $('#rankHint').textContent = `В отборе ${state.rank.length}: позиции упорядочены по совокупности этих показателей.`;
  chips.innerHTML = state.rank.map((k, i) =>
    `<span class="chip"><b>${i + 1}</b>${esc(rk[k] || RANK_LABELS[k] || k)}`
    + `<button data-rm="${esc(k)}" title="Убрать из отбора">×</button></span>`).join('');
  chips.querySelectorAll('button[data-rm]').forEach((b) => {
    b.addEventListener('click', () => toggleRank(b.dataset.rm));
  });
}

function onSort(key) {
  const s = state.sort[state.view];
  if (s.key === key) s.dir = (s.dir === 'asc' ? 'desc' : 'asc');
  else { s.key = key; s.dir = ASC_FIRST.has(key) ? 'asc' : 'desc'; }
  // Client-sorted views re-render instantly; server-sorted views refetch.
  if (CLIENT_SORTED.has(state.view)) {
    render();
  } else {
    // Move the arrow now so the click is acknowledged even if the fetch is
    // queued behind one already in flight.
    if (state.cols) renderHead(state.cols);
    load();
  }
}

function sortedRows(cols) {
  if (!CLIENT_SORTED.has(state.view) && !state.rank.length) return state.rows;
  const rows = state.rows.slice();

  // Composite ranking takes priority over single-column sort, and it works
  // identically whether the view is server-sorted or client-sorted. The only
  // difference is where the ranking computation happens: for flips it runs on the
  // server (all candidates), for the others it runs right here (the response is
  // already the full set).
  if (state.rank.length) {
    const rk = rankableKeys();
    const used = state.rank.filter((k) => k in rk);
    if (used.length) {
      // Compute percentiles on all rows, then sort.
      const byKey = {};
      for (const k of used) {
        const col = cols.find((c) => c.key === k);
        const getValue = col && col.value ? col.value : (r) => r[k];
        const vals = rows.map(getValue);
        const higher = !(k === 'absorb' || k === 'absorb_h' || k === 'buy_price'
                        || k === 'unit_price' || k === 'avg_price');
        byKey[k] = clientPercentiles(vals, higher);
      }
      for (let i = 0; i < rows.length; i++) {
        const parts = {};
        let sum = 0;
        for (const k of used) {
          const v = Math.round(byKey[k][i] * 100);
          parts[k] = v;
          sum += v;
        }
        rows[i] = { ...rows[i], rank_parts: parts, rank_score: Math.round(sum / used.length),
          rank_exact: sum / used.length };
      }
      const s = state.sort[state.view];
      rows.sort((a, b) => s.dir === 'asc' ? a.rank_exact - b.rank_exact : b.rank_exact - a.rank_exact);
      return rows;
    }
  }

  if (!CLIENT_SORTED.has(state.view)) return state.rows;
  const s = state.sort[state.view];
  const col = cols.find((c) => c.key === s.key);
  if (!col || !col.value) return state.rows;
  rows.sort((a, b) => {
    const av = col.value(a), bv = col.value(b);
    const r = (typeof av === 'string' || typeof bv === 'string')
      ? String(av).localeCompare(String(bv), 'ru')
      : (av - bv);
    return s.dir === 'asc' ? r : -r;
  });
  return rows;
}

// Percentile position of each value (0..1). Same logic as the server-side
// `_percentiles`, so the composite ranking looks and behaves identically
// regardless of whether it ran in Python or here.
function clientPercentiles(values, higherIsBetter) {
  const n = values.length;
  if (n <= 1) return values.map(() => 1.0);
  const indices = Array.from({ length: n }, (_, i) => i);
  indices.sort((a, b) => higherIsBetter ? values[b] - values[a] : values[a] - values[b]);
  const out = new Array(n);
  let i = 0;
  while (i < n) {
    let j = i;
    while (j + 1 < n && values[indices[j + 1]] === values[indices[i]]) j++;
    const avg = (i + j) / 2;
    const score = 1 - avg / (n - 1);
    for (let k = i; k <= j; k++) out[indices[k]] = score;
    i = j + 1;
  }
  return out;
}

const EMPTY_MSG = {
  flips: 'Под текущие фильтры выгодных флипов нет.<br>Снизь мин. прибыль, расширь период или сбрось фильтры.',
  plan: 'На этот бюджет нечего купить.<br>Увеличь бюджет, снизь мин. прибыль или выбери другой город.',
  cities: 'Нет данных по городам под эти фильтры.',
  stats: 'Нет данных по этим фильтрам. Если сервер только запустился — подожди, пока догрузится история.',
};

// An empty table has two very different causes, and blaming the filters when the
// real problem is stale data sends you chasing the wrong thing. If prices have
// not refreshed recently, every quote fails the freshness cut-off and nothing can
// match no matter how the filters are set — say that instead.
function emptyReason() {
  const s = state.status;
  if (!s || !s.current_refreshed_at) {
    return 'Цены ещё не загружены — идёт первичный сбор данных. Обычно 1–2 минуты.';
  }
  const mins = (Date.now() - new Date(s.current_refreshed_at).getTime()) / 60000;
  if (mins > 20) {
    return `<b>Цены не обновлялись ${Math.round(mins)} мин</b>, а котировки старше`
      + ' нескольких часов в расчёт не берутся — поэтому пусто, и фильтры тут не виноваты.'
      + '<br>Нажми «⟳ Обновить» и проверь логи: <code>/root/logs/shopalbi.log</code>.';
  }
  return EMPTY_MSG[state.view] || 'Нет данных.';
}

function renderRows(cols, rows) {
  const tb = $('#tbody');
  if (!rows.length) {
    tb.innerHTML = '';
    const e = $('#emptyState');
    e.hidden = false;
    e.innerHTML = emptyReason();
    return;
  }
  $('#emptyState').hidden = true;
  tb.innerHTML = rows.map((r) => '<tr>' + cols.map((c) => {
    const cls = [c.left ? 'left' : '', c.stick ? 'stick' : ''].filter(Boolean).join(' ');
    let html;
    try { html = c.render(r); } catch (err) { html = '<span class="dimc">—</span>'; }
    return `<td class="${cls}">${html}</td>`;
  }).join('') + '</tr>').join('');
}

function render() {
  const cols = buildColumns();
  renderHead(cols);
  renderRows(cols, sortedRows(cols));
  state.cols = cols;
}

// ---------------------------------------------------------------- KPIs + note

function kpi(label, val, sub, tone) {
  return `<div class="kpi ${tone || ''}"><div class="kpi-label">${label}</div>`
       + `<div class="kpi-val">${val}</div>${sub ? `<div class="kpi-sub">${sub}</div>` : ''}</div>`;
}

function renderKpis(d) {
  const box = $('#kpis');
  if (state.view === 'plan') {
    const b = d.best;
    if (!b || !b.items_count) { box.hidden = true; box.innerHTML = ''; return; }
    box.hidden = false;
    box.innerHTML =
      kpi('Город закупки', esc(cityRu(d.city)), `риск ${b.gank_rate}% · рейс ~${b.trip_hours} ч`, 'gold') +
      kpi('Вложить', compact(b.spent), `остаток ${compact(b.leftover)} · позиций ${b.items_count}`) +
      kpi('Прибыль (без потерь)', signed(b.profit), `ROI ${b.roi_pct}%`, 'good') +
      kpi('С учётом риска ганка', signed(b.ev_profit), `ROI ${b.ev_roi_pct}% · цена риска ${compact(b.risk_cost)}`, b.ev_profit > 0 ? 'good' : 'bad') +
      kpi('Прибыль в час', signed(b.profit_per_hour), 'закупка + дорога') +
      kpi('Данные', b.live_share_pct + '% live', `стакан ${fmt(d.orderbook.orders)} ордеров`, b.live_share_pct >= 50 ? 'good' : '');
    return;
  }
  if (state.view === 'flips') {
    const rows = state.rows;
    if (!rows.length) { box.hidden = true; box.innerHTML = ''; return; }
    box.hidden = false;
    const med = (arr) => { const a = arr.slice().sort((x, y) => x - y); return a[Math.floor(a.length / 2)]; };
    const good = rows.filter((r) => r.reliability >= 60).length;
    const upsell = rows.filter((r) => r.quality_upsell).length;
    box.innerHTML =
      kpi('Флипов найдено', fmt(d.total), `показано ${rows.length}`, 'gold') +
      kpi('Медианная прибыль', signed(med(rows.map((r) => r.profit))), 'на 1 шт, после налога') +
      kpi('Медианный %', med(rows.map((r) => r.profit_pct)) + '%', 'к затратам') +
      kpi('Надёжных (≥60)', fmt(good), `из ${rows.length}`, good ? 'good' : '') +
      kpi('Через низкое кач-во', fmt(upsell), 'ордер qN принимает q≥N', upsell ? 'good' : '') +
      kpi('Модель', `×${d.net}`, `налог ${(d.sales_tax * 100).toFixed(0)}%${d.sell_mode === 'order' ? ' + сбор 2.5%' : ''} · ганк ${(d.gank_rate * 100).toFixed(0)}%`);
    return;
  }
  box.hidden = true;
  box.innerHTML = '';
}

function renderNote(d) {
  const w = WIN_RU[state.window];
  const n = $('#note');
  if (state.view === 'flips') {
    const where = d.buy_city
      ? `закупка только в <b>${esc(cityRu(d.buy_city))}</b>`
      : 'по каждому предмету показан лучший город';
    n.innerHTML = `${where} · прибыль = <code>ставка ЧР × ${d.net} − цена закупки${d.cost_mult !== 1 ? ' × ' + d.cost_mult : ''}</code>`
      + ` · «По средней ЧР» — та же сделка по средней цене ЧР за <b>${esc(rangeRu(d.window_range) || w)}</b> (защита от разового скачка)`
      + ` · сортировка по столбцу — клик по заголовку, наведи на заголовок чтобы увидеть формулу.`;
  } else if (state.view === 'plan') {
    const b = d.best;
    if (!b || !b.items_count) {
      n.innerHTML = `На бюджет <b>${fmt(d.budget)}</b> подходящих закупок нет. Увеличь бюджет или снизь мин. прибыль в «⚙ Модель».`;
      return;
    }
    const cmp = (d.cities || []).slice(0, 6)
      .map((c) => `${esc(cityRu(c.city))} <b>${signed(c.ev_profit)}</b>`).join(' · ');
    const mode = d.has_depth
      ? `количества ограничены <b>реальным живым стаканом</b>`
      : `живого стакана пока нет — количества ограничены оборотом ЧР (<b>оценка</b>)`;
    const spentPct = d.budget ? Math.round(b.spent / d.budget * 100) : 100;
    const why = {
      budget: 'бюджет разложен полностью',
      depth: `<b>рынок кончился раньше бюджета</b>: по всем ${fmt(b.candidates)} подходящим позициям`
        + ` выбрано всё, что Чёрный рынок реально успевает выкупить. Остаток некуда деть, не переплачивая.`
        + ` Расширь период или ослабь фильтры в «⚙ Модель» — станет больше позиций.`,
      positions: `упёрлись в <b>лимит позиций (${b.max_items})</b> — столько разных наименований`
        + ` за рейс уже много. Поднять: <code>SHOPALBI_RECOMMEND_MAX_ITEMS</code> в docker-compose.yml.`,
      filters: 'под текущие фильтры нет ни одной подходящей позиции.',
    }[b.limit_reason] || '';
    n.innerHTML = `${mode}. Размещено <b>${spentPct}%</b> бюджета — ${why}`
      + `<br>Сравнение городов по прибыли с учётом риска: ${cmp}.`
      + ` Один предмет не получает больше 30% бюджета. Не всё скупил — впиши остаток и обнови.`;
  } else if (state.view === 'cities') {
    n.innerHTML = `Города отсортированы по сумме оценок лучших ${d.top_n} флипов за ${w}.`
      + ` Начинай с верхнего. <b>Карлеон</b> стоит особняком: цены там выше, зато риск потерять груз нулевой — до ЧР пара шагов.`;
  } else {
    n.innerHTML = `Показано <b>${fmt(d.rows.length)}</b> из <b>${fmt(d.total)}</b> · цены средневзвешенные по объёму сделок`
      + ` за <b>${esc(rangeRu(d.window_range) || w)}</b> (${d.window_days} полных суток UTC)`
      + ` · клик по названию города — сортировка по его цене · зелёным отмечен самый дешёвый город.`;
  }
}

// ---------------------------------------------------------------- data

function filters() {
  const p = new URLSearchParams();
  p.set('window', state.window);
  const add = (id, name) => { const v = $(id).value.trim(); if (v !== '') p.set(name, v); };
  add('#search', 'search');
  add('#category', 'category');
  add('#tier', 'tier');
  add('#enchant', 'enchant');
  add('#quality', 'quality');

  if (state.view !== 'stats') {
    add('#minProfit', 'min_profit');
    add('#minProfitPct', 'min_profit_pct');
    add('#minVolume', 'min_bm_volume');
    p.set('gank_rate', (Number($('#gank').value) / 100).toFixed(2));
    p.set('sell_mode', $('#sellMode').value);
    p.set('buy_mode', $('#buyMode').value);
  }

  if (state.view === 'plan') {
    p.set('budget', $('#budget').value || '0');
    const c = $('#buyCity').value; if (c) p.set('city', c);
    return p;
  }
  if (state.view === 'cities') { p.set('top_n', '100'); return p; }

  p.set('limit', '400');
  const s = state.sort[state.view];
  p.set('sort', s.key);
  p.set('direction', s.dir);
  if (state.view === 'flips') {
    const c = $('#buyCity').value; if (c) p.set('buy_city', c);
    // Composite ranking is applied server-side over ALL candidates, so the top
    // of the list is the best combination overall, not just within one page.
    if (state.rank.length) p.set('rank', state.rank.join(','));
  }
  return p;
}

const ENDPOINT = { flips: '/api/flips', plan: '/api/plan', cities: '/api/cities', stats: '/api/stats' };

async function load() {
  // Coalesce instead of dropping. This used to `return` while a request was in
  // flight, so clicking a column header during a load silently threw the new
  // sort away: the state changed, no fetch happened, and the arrow stayed on the
  // old column. Now the last request always wins.
  if (state.loading) {
    state.pending = true;
    return;
  }
  state.loading = true;
  state.pending = false;
  $('#loader').hidden = false;
  try {
    const d = await fetchJSON(`${ENDPOINT[state.view]}?${filters()}`);
    state.payload = d;
    state.rows = state.view === 'plan' ? ((d.best && d.best.items) || []) : (d.rows || []);
    render();
    renderKpis(d);
    renderNote(d);
  } catch (err) {
    const msg = err.name === 'AbortError' ? 'сервер не ответил вовремя' : err.message;
    toast('Ошибка загрузки: ' + msg, 'err');
    if (!state.rows.length) {
      $('#tbody').innerHTML = '';
      $('#emptyState').hidden = false;
      $('#emptyState').innerHTML = `Не удалось загрузить данные: ${esc(msg)}.<br>Проверь логи: <code>/root/logs/shopalbi.log</code>`;
    }
  } finally {
    state.loading = false;
    $('#loader').hidden = true;     // always cleared, even on a thrown render
    if (state.pending) load();      // a newer request arrived while we were busy
  }
}

// ---------------------------------------------------------------- CSV

function exportCsv() {
  const cols = state.cols || buildColumns();
  const rows = sortedRows(cols);
  if (!rows.length) { toast('Нечего экспортировать', 'err'); return; }
  const cell = (v) => {
    const s = v === null || v === undefined ? '' : String(v);
    return /[";\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  const lines = [cols.map((c) => cell(c.label)).join(';')];
  for (const r of rows) {
    lines.push(cols.map((c) => cell(c.csv ? c.csv(r) : '')).join(';'));
  }
  // BOM so Excel opens Cyrillic correctly; ';' because ru locale Excel expects it.
  const blob = new Blob(['\uFEFF' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `shopalbi-${state.view}-${state.window}-${new Date().toISOString().slice(0, 16).replace(':', '')}.csv`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  toast(`Выгружено строк: ${rows.length}`, 'ok');
}

// ---------------------------------------------------------------- my sets

async function setsLoadList(selectId) {
  const d = await fetchJSON('/api/sets');
  state.sets = d.rows || [];
  const box = $('#setsList');
  if (!state.sets.length) {
    box.innerHTML = '<div class="state">Пока нет ни одного сета.<br>Нажми «+ Новый».</div>';
  } else {
    box.innerHTML = state.sets.map((s) =>
      `<div class="set-item${s.set_id === state.setId ? ' active' : ''}" data-id="${s.set_id}">`
      + `<div class="set-item-name">${esc(s.name)}</div>`
      + `<div class="set-item-sub">позиций: ${s.lines}${s.note ? ' · ' + esc(s.note) : ''}</div></div>`
    ).join('');
    box.querySelectorAll('.set-item').forEach((el) => {
      el.addEventListener('click', () => setsOpen(Number(el.dataset.id)));
    });
  }
  if (selectId) await setsOpen(selectId);
  else if (!state.setId && state.sets.length) await setsOpen(state.sets[0].set_id);
  else setsTogglePanels();
}

function setsTogglePanels() {
  const has = state.setId != null;
  $('#setsEmpty').hidden = has;
  $('#setsEditor').hidden = !has;
  $('#setsResult').hidden = !has || !state.setPrice;
}

async function setsOpen(id) {
  state.setId = id;
  try {
    const s = await fetchJSON(`/api/sets/${id}`);
    $('#setName').value = s.name;
    $('#setNote').value = s.note || '';
    state.setLines = (s.lines || []).map((l) => ({
      item_id: l.item_id, quality: l.quality, qty: l.qty,
      name: l.name_disp || l.item_id,
      tier_ench: l.tier ? `Т${l.tier}.${l.enchant}` : '—',
    }));
    renderSetLines();
    $('#setsList').querySelectorAll('.set-item').forEach((el) => {
      el.classList.toggle('active', Number(el.dataset.id) === id);
    });
    setsTogglePanels();
    await setsPrice();
  } catch (e) {
    toast('Не удалось открыть сет: ' + e.message, 'err');
  }
}

function renderSetLines() {
  const t = $('#setLines');
  if (!state.setLines.length) {
    t.innerHTML = '<tbody><tr><td class="dimc" style="padding:14px 8px">'
      + 'Пусто. Найди предмет в поле выше и он появится здесь.</td></tr></tbody>';
    return;
  }
  const qOpts = (sel) => [1, 2, 3, 4, 5].map((q) =>
    `<option value="${q}"${q === sel ? ' selected' : ''}>${esc(qualityRu(q))}</option>`).join('');
  t.innerHTML = '<thead><tr><th>Предмет</th><th style="width:88px">Тир</th>'
    + '<th style="width:170px">Качество</th><th style="width:110px">Кол-во</th><th style="width:40px"></th>'
    + '</tr></thead><tbody>' + state.setLines.map((l, i) =>
      `<tr><td>${esc(l.name)}<span class="sub2">${esc(l.item_id)}`
      + (l.tier_ench === '—' && l.name === l.item_id
        ? ' <span class="pill spike">нет в каталоге</span>' : '') + '</span></td>'
      + `<td><span class="pill tier">${esc(l.tier_ench)}</span></td>`
      + `<td><select class="input" data-q="${i}">${qOpts(l.quality)}</select></td>`
      + `<td><input type="number" class="input mono" data-n="${i}" min="1" max="10000" value="${l.qty}"></td>`
      + `<td><button class="del" data-d="${i}" title="Убрать">×</button></td></tr>`
    ).join('') + '</tbody>';
  t.querySelectorAll('select[data-q]').forEach((el) => el.addEventListener('change', () => {
    state.setLines[Number(el.dataset.q)].quality = Number(el.value);
  }));
  t.querySelectorAll('input[data-n]').forEach((el) => el.addEventListener('input', () => {
    state.setLines[Number(el.dataset.n)].qty = Math.max(1, Number(el.value) || 1);
  }));
  t.querySelectorAll('button[data-d]').forEach((el) => el.addEventListener('click', () => {
    state.setLines.splice(Number(el.dataset.d), 1);
    renderSetLines();
  }));
}

async function setsSave(thenPrice) {
  if (state.setId == null) return;
  const body = {
    name: $('#setName').value.trim() || 'Мой сет',
    note: $('#setNote').value.trim(),
    lines: state.setLines.map((l) => ({ item_id: l.item_id, quality: l.quality, qty: l.qty })),
  };
  try {
    const r = await fetch(`/api/sets/${state.setId}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    if (!r.ok) {
      let d = 'HTTP ' + r.status;
      try { d = (await r.json()).detail || d; } catch (e) { /* ignore */ }
      throw new Error(d);
    }
    toast('Сет сохранён', 'ok');
    await setsLoadList(state.setId);
    if (thenPrice) await setsPrice();
  } catch (e) {
    toast('Не сохранилось: ' + e.message, 'err');
  }
}

async function setsPrice() {
  if (state.setId == null || !state.setLines.length) {
    state.setPrice = null;
    setsTogglePanels();
    return;
  }
  $('#loader').hidden = false;
  try {
    const subst = $('#setSubst').value === '1';
    const equiv = $('#setEquiv').value === '1';
    const d = await fetchJSON(
      `/api/sets/${state.setId}/price?allow_higher_quality=${subst}&use_equivalents=${equiv}`, 60000);
    state.setPrice = d;
    renderSetPrice(d);
    setsTogglePanels();
  } catch (e) {
    toast('Не удалось посчитать: ' + e.message, 'err');
  } finally {
    $('#loader').hidden = true;
  }
}

function renderSetPrice(d) {
  const b = d.best;
  if (!b) { $('#setsResult').hidden = true; return; }

  $('#setKpis').innerHTML =
    kpi('Дешевле всего целиком', esc(cityRu(b.city)),
        b.complete ? 'есть все позиции' : `не хватает ${b.missing}`, b.complete ? 'gold' : 'bad') +
    kpi('Стоимость сета', fmt(b.total), `позиций ${b.items_priced} из ${d.lines.length}`) +
    (d.split_total != null
      ? kpi('Если ездить по городам', fmt(d.split_total),
            d.one_stop_premium != null
              ? `одна поездка дороже на ${fmt(d.one_stop_premium)}`
              : 'не все позиции доступны', 'good')
      : kpi('Если ездить по городам', '—', 'какой-то позиции нет нигде')) +
    kpi('Городов с полным сетом', fmt(d.cities.filter((c) => c.complete).length),
        `из ${d.cities.length}`);

  const swapped = b.lines.filter((x) => x.equivalent).length;
  $('#setNoteLine').innerHTML =
    'Города, где есть <b>все</b> позиции, идут первыми — город дешевле, но без одной вещи, '
    + 'означает вторую поездку. Цены — минимальные ордера продажи на момент расчёта'
    + (d.allow_higher_quality
      ? '. Если нужного качества нет, подставляется лучшее (помечено «замена»)'
      : '. Подстановка лучшего качества выключена')
    + (d.use_equivalents
      ? `. Эквивалент по силе включён: рассмотрено ${fmt(d.variants_considered)} вариантов`
        + (swapped ? `, выгоднее взять другой тир в <b>${swapped}</b> позициях` : '')
        + '. Именные артефакты не подменяются.'
      : '. Эквивалент выключен — берётся ровно указанный тир.');

  $('#setCitiesHead').innerHTML = '<tr><th class="left">Город</th><th>Итого</th>'
    + '<th>Позиций</th><th class="left">Чего нет</th></tr>';
  $('#setCitiesBody').innerHTML = d.cities.map((c) => {
    const cls = [c.city === b.city ? 'best-city' : '', c.complete ? '' : 'incomplete'].filter(Boolean).join(' ');
    return `<tr class="${cls}"><td class="left"><span class="city">${esc(cityRu(c.city))}</span>`
      + (c.city === b.city ? '<span class="sub2">дешевле всего целиком</span>' : '') + '</td>'
      + `<td><span class="num big">${fmt(c.total)}</span></td>`
      + `<td><span class="num ${c.complete ? 'pos' : 'neg'}">${c.items_priced}/${d.lines.length}</span></td>`
      + `<td class="left"><span class="dimc" style="font-size:11.5px">`
      + (c.complete ? '—' : esc(c.missing_items.slice(0, 4).join(', ')
        + (c.missing_items.length > 4 ? ` и ещё ${c.missing_items.length - 4}` : ''))) + '</span></td></tr>';
  }).join('');

  $('#setDetailHead').innerHTML = `<tr><th class="left">Что купить в ${esc(cityIn(b.city))}</th>`
    + '<th>Кач-во</th><th>Шт</th><th>Цена</th><th>Итого</th></tr>';
  $('#setDetailBody').innerHTML = b.lines.map((e) => {
    if (!e.available) {
      const why = e.unknown
        ? 'такого предмета нет в каталоге — убери строку из сета'
        : 'нет в продаже в этом городе';
      return `<tr class="incomplete"><td class="left">${esc(e.name)}`
        + `<span class="sub2">${esc(e.category_label)}</span></td>`
        + `<td colspan="4"><span class="neg">${esc(why)}</span></td></tr>`;
    }
    // When an equal-power variant is cheaper, the row shows what to actually put
    // in the basket — the tier you asked for is only a power target.
    const what = e.equivalent
      ? `${esc(e.buy_name)} <span class="pill up" title="Тот же предмет другого тира с той же силой ${e.item_power} IP. Запрошено ${esc(e.tier_ench)}, дешевле взять ${esc(e.buy_tier_ench)}.">${esc(e.buy_tier_ench)} вместо ${esc(e.tier_ench)}</span>`
      : `${esc(e.name)}<span class="sub2"><span class="pill tier">${esc(e.tier_ench)}</span> ${esc(e.category_label)}${e.item_power ? ' · ' + e.item_power + ' IP' : ''}</span>`;
    return `<tr><td class="left">${what}</td>`
      + `<td><span class="q q${e.quality}"></span>${esc(e.quality_label)}`
      + (e.substituted
        ? `<span class="sub2" title="Запрошено «${esc(e.want_quality_label)}», но в продаже только лучше">замена</span>`
        : '') + '</td>'
      + `<td><span class="num">${fmt(e.qty)}</span></td>`
      + `<td><span class="num">${fmt(e.unit_price)}</span></td>`
      + `<td><span class="num big">${fmt(e.line_total)}</span></td></tr>`;
  }).join('');
}

// ---- item search -----------------------------------------------------------

const setsSearch = debounce(async () => {
  const q = $('#setSearch').value.trim();
  const cat = $('#setSearchCat').value;
  const box = $('#setSuggest');
  if (q.length < 2 && !cat) { box.hidden = true; return; }
  try {
    const p = new URLSearchParams({ limit: '25' });
    if (q) p.set('q', q);
    if (cat) p.set('category', cat);
    const d = await fetchJSON(`/api/shop/search?${p}`);
    if (!d.rows.length) {
      box.innerHTML = '<div class="state">Ничего не найдено. Попробуй часть названия из игры.</div>';
    } else {
      box.innerHTML = d.rows.map((r) =>
        `<div class="suggest-row" data-id="${esc(r.item_id)}" data-name="${esc(r.name)}"`
        + ` data-te="${esc(r.tier_ench)}"><span class="pill tier">${esc(r.tier_ench)}</span>`
        + `<span class="nm">${esc(r.name)}</span>`
        + `<span class="pill cat">${esc(r.category_label)}</span></div>`).join('');
      box.querySelectorAll('.suggest-row').forEach((el) => {
        el.addEventListener('click', () => {
          const ex = state.setLines.find((l) => l.item_id === el.dataset.id);
          if (ex) ex.qty += 1;
          else state.setLines.push({
            item_id: el.dataset.id, name: el.dataset.name,
            tier_ench: el.dataset.te, quality: 1, qty: 1,
          });
          renderSetLines();
          $('#setSearch').value = '';
          box.hidden = true;
          toast(ex ? `${el.dataset.name}: количество +1` : `Добавлено: ${el.dataset.name}`, 'ok');
        });
      });
    }
    box.hidden = false;
  } catch (e) {
    box.hidden = true;
  }
}, 260);

function wireSets() {
  $('#setSearch').addEventListener('input', setsSearch);
  $('#setSearchCat').addEventListener('change', setsSearch);
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.sets-add')) $('#setSuggest').hidden = true;
  });
  $('#setSubst').addEventListener('change', setsPrice);
  $('#setEquiv').addEventListener('change', setsPrice);
  $('#setSaveBtn').addEventListener('click', () => setsSave(true));

  $('#setNewBtn').addEventListener('click', async () => {
    try {
      const r = await fetch('/api/sets', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: 'Новый сет', note: '', lines: [] }),
      });
      const s = await r.json();
      state.setPrice = null;
      await setsLoadList(s.set_id);
      $('#setName').focus();
    } catch (e) {
      toast('Не удалось создать сет', 'err');
    }
  });

  $('#setDelBtn').addEventListener('click', async () => {
    if (state.setId == null) return;
    const name = $('#setName').value || 'сет';
    if (!confirm(`Удалить «${name}»? Отменить будет нельзя.`)) return;
    try {
      await fetch(`/api/sets/${state.setId}`, { method: 'DELETE' });
      state.setId = null;
      state.setPrice = null;
      state.setLines = [];
      await setsLoadList();
      toast('Сет удалён', 'ok');
    } catch (e) {
      toast('Не удалось удалить', 'err');
    }
  });
}

// ---------------------------------------------------------------- status

async function pollStatus() {
  try {
    const s = await fetchJSON('/api/status', 10000);
    state.status = s;
    const dot = $('#statusDot');
    const ob = s.orderbook || {};
    if (!s.current_refreshed_at || !s.current_rows) {
      dot.className = 'dot warn';
      $('#statusMain').textContent = `Каталог ${fmt(s.items)} предметов · идёт первичный сбор данных…`;
      $('#statusSub').textContent = 'обычно 1–2 минуты';
    } else {
      dot.className = s.refresh_running ? 'dot warn' : 'dot ok';
      $('#statusMain').textContent =
        `Цены ${timeAgo(s.current_refreshed_at)} · история ${timeAgo(s.history_refreshed_at)}`;
      $('#statusSub').textContent =
        `${fmt(s.items)} предметов · живой стакан ${fmt(ob.orders)} ордеров`
        + (ob.npc_orders ? ` (${fmt(ob.npc_orders)} ЧР)` : '')
        + (s.refresh_running ? ' · обновляется…' : '');
    }
    $('#taxNote').textContent =
      `Налог ЧР ${(s.sales_tax * 100).toFixed(0)}% (Premium) · сбор за ордер ${(s.setup_fee * 100).toFixed(1)}% · v${s.version}`;
  } catch (e) {
    $('#statusDot').className = 'dot err';
    $('#statusMain').textContent = 'Сервер недоступен';
    $('#statusSub').textContent = '';
  }
}

// ---------------------------------------------------------------- init

async function initMeta() {
  const m = await fetchJSON('/api/meta');
  state.meta = m;
  state.cities = m.cities;
  const opt = (el, v, label) => el.insertAdjacentHTML('beforeend', `<option value="${esc(v)}">${esc(label)}</option>`);
  for (const c of m.buy_cities) opt($('#buyCity'), c, cityRu(c));
  for (const c of m.categories) opt($('#category'), c.id, c.label);
  for (const t of m.tiers) opt($('#tier'), t, 'T' + t);
  for (const e of m.enchants) opt($('#enchant'), e, e === 0 ? 'Без зач. (.0)' : '.' + e);
  for (const q of m.qualities) {
    opt($('#quality'), q.id, q.label);
    QUALITY_RU[q.id] = q.label;          // backend is the source of truth
  }

  // Categories for the set item search come from the shopping catalog, which is
  // wider than the Black Market one (mounts, potions, food, tools).
  try {
    const sc = await fetchJSON('/api/shop/search?limit=1');
    for (const c of sc.categories || []) opt($('#setSearchCat'), c.id, c.label);
  } catch (e) { /* the sets tab will still work, just without the filter */ }

  // Window buttons come from the server so a new window needs no HTML edit.
  if (!m.windows.some((w) => w.id === state.window)) state.window = m.windows[0].id;
  $('#windowSwitch').innerHTML = m.windows.map((w) =>
    `<button class="seg-btn${w.id === state.window ? ' active' : ''}" data-window="${esc(w.id)}"`
    + ` title="последние ${w.days} полных суток UTC">${esc(w.label)}</button>`).join('');

  $('#gank').value = Math.round((m.gank_rate || 0.08) * 100);
  $('#gankVal').textContent = $('#gank').value + '%';
}

function syncControls() {
  const v = state.view;
  const isSets = v === 'sets';
  // The sets tab has its own layout and its own filters, so the shared control
  // row and results table step aside entirely rather than showing dead widgets.
  $('#setsView').hidden = !isSets;
  $('#tableWrap').hidden = isSets;
  $('#kpis').hidden = isSets || !$('#kpis').innerHTML;
  document.querySelector('.controls').hidden = isSets;
  $('#note').hidden = isSets;
  if (isSets) {
    renderRankBar();
    return;
  }
  $('#budgetWrap').hidden = v !== 'plan';
  $('#buyCity').hidden = !(v === 'flips' || v === 'plan');
  // Composite ranking is available on every tab EXCEPT sets (which has its own
  // concept of "best city" and a fixed ordering).
  $('#rankBtn').hidden = false;
  renderRankBar();
  const modelUsed = v !== 'stats';
  $('#advBtn').hidden = !modelUsed;
  if (!modelUsed) {
    $('#advRow').hidden = true;
    $('#advBtn').classList.remove('on');
  }
}

function wire() {
  $('#mainTabs').addEventListener('click', (e) => {
    const b = e.target.closest('.tab');
    if (!b || b.classList.contains('active')) return;
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
    b.classList.add('active');
    state.view = b.dataset.view;
    state.rows = [];
    $('#tbody').innerHTML = '';
    $('#kpis').hidden = true;
    syncControls();
    if (state.view === 'sets') setsLoadList(state.setId);
    else load();
  });

  $('#rankBtn').addEventListener('click', () => {
    state.rankMode = !state.rankMode;
    if (!state.rankMode && state.rank.length) {
      state.rank = [];             // leaving the mode clears the criteria
      renderRankBar();
      if (state.cols) renderHead(state.cols);
      load();
      return;
    }
    renderRankBar();
    if (state.cols) renderHead(state.cols);
  });

  $('#windowSwitch').addEventListener('click', (e) => {
    const b = e.target.closest('.seg-btn');
    if (!b || b.classList.contains('active')) return;
    $('#windowSwitch').querySelectorAll('.seg-btn').forEach((x) => x.classList.remove('active'));
    b.classList.add('active');
    state.window = b.dataset.window;
    load();
  });

  const deb = debounce(load, 350);
  $('#search').addEventListener('input', deb);
  ['#category', '#tier', '#enchant', '#quality', '#buyCity', '#sellMode', '#buyMode']
    .forEach((id) => $(id).addEventListener('change', load));
  ['#minProfit', '#minProfitPct', '#minVolume', '#budget']
    .forEach((id) => $(id).addEventListener('input', debounce(load, 450)));
  $('#gank').addEventListener('input', () => { $('#gankVal').textContent = $('#gank').value + '%'; });
  $('#gank').addEventListener('change', load);

  $('#advBtn').addEventListener('click', () => {
    const r = $('#advRow');
    r.hidden = !r.hidden;
    $('#advBtn').classList.toggle('on', !r.hidden);
  });
  $('#csvBtn').addEventListener('click', exportCsv);

  $('#resetBtn').addEventListener('click', () => {
    $('#search').value = '';
    ['#category', '#tier', '#enchant', '#quality', '#buyCity'].forEach((id) => { $(id).value = ''; });
    $('#minProfit').value = 1000;
    $('#minProfitPct').value = 0;
    $('#minVolume').value = 1;
    $('#sellMode').value = 'instant';
    $('#buyMode').value = 'instant';
    $('#gank').value = Math.round(((state.meta && state.meta.gank_rate) || 0.08) * 100);
    $('#gankVal').textContent = $('#gank').value + '%';
    load();
  });

  $('#refreshBtn').addEventListener('click', async () => {
    const b = $('#refreshBtn');
    b.classList.add('busy');
    try {
      await fetch('/api/refresh', { method: 'POST' });
      toast('Обновление запущено — цены через ~20 сек, история через ~1 мин.', 'ok');
      setTimeout(pollStatus, 3000);
      setTimeout(load, 30000);
    } catch (e) {
      toast('Не удалось запустить обновление', 'err');
    }
    setTimeout(() => b.classList.remove('busy'), 2500);
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === '/' && document.activeElement !== $('#search')) {
      e.preventDefault(); $('#search').focus();
    }
  });
}

(async function main() {
  wire();
  wireSets();
  syncControls();
  try { await initMeta(); } catch (e) { toast('Не удалось загрузить справочники: ' + e.message, 'err'); }
  pollStatus();
  await load();
  setInterval(pollStatus, 20000);
})();
