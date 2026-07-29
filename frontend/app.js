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

// Quality names, filled from /api/meta so the backend stays the single source
// of truth. Fallback matches the in-game RU client wording.
const QUALITY_RU = {
  1: 'Обычное', 2: 'Хорошее', 3: 'Выдающееся', 4: 'Отличное', 5: 'Шедевральное',
};
const qualityRu = (q) => QUALITY_RU[q] || ('q' + q);
const WIN_RU = { day: 'день', '3d': '3 дня', week: 'неделю', month: 'месяц', quarter: '90 дней' };

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

function colsFlips() {
  return [
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
}

function colsPlan() {
  return [
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
    { key: 'bm_buy_now', label: 'Ставка ЧР', value: (r) => r.bm_buy_now, hint: H.bmPrice,
      render: (r) => `<span class="num" style="color:var(--gold)">${fmt(r.bm_buy_now)}</span>`
        + (r.quality_upsell
            ? `<span class="sub2" title="Ордер выкупа принимает своё качество и выше, и платит больше именно за это.">в ордер «${esc(qualityRu(r.bm_quality))}»</span>`
            : ''),
      csv: (r) => r.bm_buy_now },
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
}

function colsCities() {
  return [
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
  return cols;
}

function buildColumns() {
  if (state.view === 'flips') return colsFlips();
  if (state.view === 'plan') return colsPlan();
  if (state.view === 'cities') return colsCities();
  return colsStats();
}

// ---------------------------------------------------------------- render

function renderHead(cols) {
  const s = state.sort[state.view];
  $('#thead').innerHTML = '<tr>' + cols.map((c) => {
    const sorted = c.key === s.key;
    const cls = [c.left ? 'left' : '', c.stick ? 'stick' : '', sorted ? 'sorted' : ''].filter(Boolean).join(' ');
    const arrow = sorted
      ? `<span class="arrow">${s.dir === 'asc' ? '▲' : '▼'}</span>`
      : '<span class="arrow" style="opacity:.22">▽</span>';
    const t = c.hint ? ` title="${esc(c.hint)}"` : '';
    return `<th class="${cls}" data-sort="${esc(c.key)}"${t}>${esc(c.label)}${arrow}</th>`;
  }).join('') + '</tr>';
  $('#thead').querySelectorAll('th[data-sort]').forEach((th) => {
    th.addEventListener('click', () => onSort(th.dataset.sort));
  });
}

function onSort(key) {
  const s = state.sort[state.view];
  if (s.key === key) s.dir = (s.dir === 'asc' ? 'desc' : 'asc');
  else { s.key = key; s.dir = ASC_FIRST.has(key) ? 'asc' : 'desc'; }
  // Client-sorted views re-render instantly; server-sorted views refetch.
  if (CLIENT_SORTED.has(state.view)) render();
  else load();
}

function sortedRows(cols) {
  if (!CLIENT_SORTED.has(state.view)) return state.rows;
  const s = state.sort[state.view];                 // <- per-view, not always `cities`
  const col = cols.find((c) => c.key === s.key);
  if (!col || !col.value) return state.rows;
  const rows = state.rows.slice();
  rows.sort((a, b) => {
    const av = col.value(a), bv = col.value(b);
    const r = (typeof av === 'string' || typeof bv === 'string')
      ? String(av).localeCompare(String(bv), 'ru')
      : (av - bv);
    return s.dir === 'asc' ? r : -r;
  });
  return rows;
}

const EMPTY_MSG = {
  flips: 'Под текущие фильтры выгодных флипов нет.<br>Снизь мин. прибыль, расширь период или сбрось фильтры.',
  plan: 'На этот бюджет нечего купить.<br>Увеличь бюджет, снизь мин. прибыль или выбери другой город.',
  cities: 'Нет данных по городам под эти фильтры.',
  stats: 'Нет данных по этим фильтрам. Если сервер только запустился — подожди, пока догрузится история.',
};

function renderRows(cols, rows) {
  const tb = $('#tbody');
  if (!rows.length) {
    tb.innerHTML = '';
    const e = $('#emptyState');
    e.hidden = false;
    e.innerHTML = EMPTY_MSG[state.view] || 'Нет данных.';
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
      + ` · «По средней ЧР» — та же сделка по средней цене ЧР за ${w} (защита от разового скачка)`
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
      positions: `упёрлись в лимит позиций (${b.max_items}). Больше наименований за один рейс не унести.`,
      filters: 'под текущие фильтры нет ни одной подходящей позиции.',
    }[b.limit_reason] || '';
    n.innerHTML = `${mode}. Размещено <b>${spentPct}%</b> бюджета — ${why}`
      + `<br>Сравнение городов по прибыли с учётом риска: ${cmp}.`
      + ` Один предмет не получает больше 30% бюджета. Не всё скупил — впиши остаток и обнови.`;
  } else if (state.view === 'cities') {
    n.innerHTML = `Города отсортированы по сумме оценок лучших ${d.top_n} флипов за ${w}.`
      + ` Начинай с верхнего. <b>Карлеон</b> стоит особняком: цены там выше, зато риск потерять груз нулевой — до ЧР пара шагов.`;
  } else {
    n.innerHTML = `Показано <b>${fmt(d.rows.length)}</b> из <b>${fmt(d.total)}</b> · цены средневзвешенные по объёму сделок за ${w}`
      + ` (последние ${d.window_days} полных суток UTC) · клик по названию города — сортировка по его цене · зелёным отмечен самый дешёвый город.`;
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
  if (state.view === 'flips') { const c = $('#buyCity').value; if (c) p.set('buy_city', c); }
  return p;
}

const ENDPOINT = { flips: '/api/flips', plan: '/api/plan', cities: '/api/cities', stats: '/api/stats' };

async function load() {
  if (state.loading) return;
  state.loading = true;
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

// ---------------------------------------------------------------- status

async function pollStatus() {
  try {
    const s = await fetchJSON('/api/status', 10000);
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
  $('#budgetWrap').hidden = v !== 'plan';
  $('#buyCity').hidden = !(v === 'flips' || v === 'plan');
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
    load();
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
  syncControls();
  try { await initMeta(); } catch (e) { toast('Не удалось загрузить справочники: ' + e.message, 'err'); }
  pollStatus();
  await load();
  setInterval(pollStatus, 20000);
})();
