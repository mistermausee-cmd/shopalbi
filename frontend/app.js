'use strict';

// ---- helpers --------------------------------------------------------------

const $ = (sel) => document.querySelector(sel);
const nf = new Intl.NumberFormat('ru-RU');
const fmt = (n) => (n === null || n === undefined || n === '' ? '—' : nf.format(Math.round(n)));

const CITY_SHORT = {
  'Bridgewatch': 'Бриджуотч',
  'Fort Sterling': 'Форт-Стерлинг',
  'Lymhurst': 'Лимхёрст',
  'Martlock': 'Мартлок',
  'Thetford': 'Тетфорд',
  'Caerleon': 'Карлеон',
  'Brecilien': 'Бресилиан',
  'Black Market': 'Чёрный рынок',
};
const cityName = (c) => CITY_SHORT[c] || c;

function timeAgo(iso) {
  if (!iso) return 'нет данных';
  const then = new Date(iso).getTime();
  const diff = Math.max(0, (Date.now() - then) / 1000);
  if (diff < 60) return 'только что';
  if (diff < 3600) return `${Math.floor(diff / 60)} мин назад`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} ч назад`;
  return `${Math.floor(diff / 86400)} д назад`;
}

function estTime(hours) {
  if (hours === null || hours === undefined) return '—';
  if (hours < 1) return `~${Math.round(hours * 60)} мин`;
  if (hours < 48) return `~${hours.toFixed(1)} ч`;
  return `~${(hours / 24).toFixed(1)} дн`;
}

function scoreColor(v) {
  if (v >= 70) return 'var(--green)';
  if (v >= 45) return 'var(--gold)';
  return 'var(--red)';
}

function toast(msg, kind = '') {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast ' + kind;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.hidden = true; }, 3500);
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

// ---- state ----------------------------------------------------------------

const state = {
  view: 'flips',        // 'flips' | 'stats'
  window: 'day',
  sort: { flips: 'profit_pct', stats: 'bm_volume' },
  meta: null,
  cities: [],
  loading: false,
};

// ---- column definitions ---------------------------------------------------

const FLIP_COLS = [
  { key: 'name', label: 'Предмет', left: true, sortable: false, render: renderNameCell },
  { key: 'buy_city', label: 'Купить в', left: true, sortable: false,
    render: (r) => `<span class="city-tag">${cityName(r.buy_city)}</span>` },
  { key: 'buy_price', label: 'Цена покупки', sortable: false,
    render: (r) => `<span class="num">${fmt(r.buy_price)}</span>` },
  { key: 'bm_buy_now', label: 'Выкуп ЧР', sortable: false,
    render: (r) => `<span class="num">${fmt(r.bm_buy_now)}</span>` },
  { key: 'profit', label: 'Прибыль', sortable: true, render: renderProfitCell },
  { key: 'profit_pct', label: 'Прибыль %', sortable: true,
    render: (r) => profitPct(r.profit_pct) },
  { key: 'profit_window', label: 'Прибыль (за период)', sortable: false, render: renderWindowProfitCell },
  { key: 'bm_volume', label: 'ЧР шт/день', sortable: true,
    render: (r) => `<span class="num cell-dim">${r.bm_daily_volume}</span>` },
  { key: 'est', label: '~Время выкупа', sortable: false,
    render: (r) => `<span class="cell-dim">${estTime(r.est_sell_hours)}</span>` },
  { key: 'reliability', label: 'Надёжность', sortable: true, render: renderScoreCell },
];

function flipStatsColsBase() {
  return [
    { key: 'name', label: 'Предмет', left: true, sortable: false, render: renderNameCell },
    { key: 'bm_avg', label: 'ЧР средняя', sortable: true,
      render: (r) => `<span class="num">${fmt(r.bm_avg)}</span>` },
    { key: 'bm_volume', label: 'ЧР объём', sortable: true,
      render: (r) => `<span class="num cell-dim">${fmt(r.bm_volume)}</span>` },
  ];
}

function renderNameCell(r) {
  const q = `<span class="q-badge q${r.quality}" title="${r.quality_label}"></span>`;
  return `<div class="item-name">${q}${r.name}</div>` +
         `<div class="item-sub"><span class="pill tier">${r.tier_label}</span> ` +
         `<span class="pill cat">${r.category_label}</span> · ${r.quality_label}</div>`;
}
function profitPct(v) {
  const cls = v >= 0 ? 'profit-pos' : 'profit-neg';
  return `<span class="num ${cls}">${v >= 0 ? '+' : ''}${v}%</span>`;
}
function renderProfitCell(r) {
  const cls = r.profit >= 0 ? 'profit-pos' : 'profit-neg';
  return `<span class="num ${cls}">${r.profit >= 0 ? '+' : ''}${fmt(r.profit)}</span>`;
}
function renderWindowProfitCell(r) {
  const cls = r.profit_window >= 0 ? 'profit-pos' : 'profit-neg';
  return `<span class="num ${cls}">${r.profit_window >= 0 ? '+' : ''}${fmt(r.profit_window)}</span>` +
         ` <span class="pct cell-dim">(${r.profit_pct_window}%)</span>`;
}
function renderScoreCell(r) {
  const v = r.reliability;
  const col = scoreColor(v);
  return `<div class="score"><span class="score-bar"><i style="width:${v}%;background:${col}"></i></span>` +
         `<span class="score-val" style="color:${col}">${v}</span></div>`;
}

// ---- rendering ------------------------------------------------------------

function buildColumns() {
  if (state.view === 'flips') return FLIP_COLS;
  const cols = flipStatsColsBase();
  for (const c of state.cities) {
    cols.push({
      key: 'city:' + c, label: cityName(c), sortable: false,
      render: (r) => {
        const e = r.cities[c];
        if (!e || !e.avg) return '<span class="cell-dim">—</span>';
        return `<span class="num">${fmt(e.avg)}</span>`;
      },
    });
  }
  cols.push({
    key: 'cheapest', label: 'Дешевле всего', left: true, sortable: false,
    render: (r) => r.cheapest_city
      ? `<span class="city-tag">${cityName(r.cheapest_city)}</span> <span class="num cell-dim">${fmt(r.cheapest_city_avg)}</span>`
      : '<span class="cell-dim">—</span>',
  });
  return cols;
}

function renderHead(cols) {
  const sortKey = state.sort[state.view];
  $('#thead').innerHTML = '<tr>' + cols.map((c) => {
    const cls = [c.left ? 'left' : '', c.sortable && c.key === sortKey ? 'sorted' : ''].join(' ').trim();
    const arrow = c.sortable ? (c.key === sortKey ? ' <span class="arrow">▼</span>' : ' <span class="arrow" style="opacity:.25">▽</span>') : '';
    const attr = c.sortable ? ` data-sort="${c.key}"` : '';
    return `<th class="${cls}"${attr}>${c.label}${arrow}</th>`;
  }).join('') + '</tr>';

  $('#thead').querySelectorAll('th[data-sort]').forEach((th) => {
    th.addEventListener('click', () => {
      state.sort[state.view] = th.dataset.sort;
      load();
    });
  });
}

function renderRows(cols, rows) {
  if (!rows.length) {
    $('#tbody').innerHTML = '';
    const e = $('#emptyState');
    e.hidden = false;
    e.textContent = state.view === 'flips'
      ? 'Флипов по текущим фильтрам нет. Снизьте мин. прибыль или дождитесь обновления данных.'
      : 'Нет данных по этим фильтрам.';
    return;
  }
  $('#emptyState').hidden = true;
  $('#tbody').innerHTML = rows.map((r) =>
    '<tr>' + cols.map((c) => `<td class="${c.left ? 'left' : ''}">${c.render(r)}</td>`).join('') + '</tr>'
  ).join('');
}

// ---- data ----------------------------------------------------------------

function currentFilters() {
  const p = new URLSearchParams();
  p.set('window', state.window);
  p.set('limit', '300');
  const search = $('#search').value.trim();
  const category = $('#category').value;
  const tier = $('#tier').value;
  const quality = $('#quality').value;
  if (search) p.set('search', search);
  if (category) p.set('category', category);
  if (tier) p.set('tier', tier);
  if (quality) p.set('quality', quality);
  p.set('sort', state.sort[state.view]);
  if (state.view === 'flips') {
    const mp = $('#minProfit').value;
    if (mp !== '') p.set('min_profit', mp);
  }
  return p;
}

async function load() {
  if (state.loading) return;
  state.loading = true;
  $('#loader').hidden = false;
  const endpoint = state.view === 'flips' ? '/api/flips' : '/api/stats';
  try {
    const res = await fetch(`${endpoint}?${currentFilters().toString()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const cols = buildColumns();
    renderHead(cols);
    renderRows(cols, data.rows || []);
    updateMetaLine(data);
  } catch (err) {
    toast('Ошибка загрузки: ' + err.message, 'err');
    $('#emptyState').hidden = false;
    $('#emptyState').textContent = 'Не удалось загрузить данные.';
    $('#tbody').innerHTML = '';
  } finally {
    state.loading = false;
    $('#loader').hidden = true;
  }
}

function updateMetaLine(data) {
  const wl = { day: 'день', '3d': '3 дня', week: 'неделю', month: 'месяц' }[state.window];
  if (state.view === 'flips') {
    $('#metaLine').innerHTML =
      `Найдено флипов: <b>${data.total}</b> · выручка после налога <b>${((1 - data.sales_tax) * 100).toFixed(0)}%</b> ` +
      `· «Прибыль (за период)» считается по средней цене ЧР за ${wl} — стабильность против разового скачка.`;
  } else {
    $('#metaLine').innerHTML =
      `Показано предметов: <b>${data.rows.length}</b> из <b>${data.total}</b> · ` +
      `цены — средневзвешенные по объёму за ${wl}.`;
  }
}

// ---- status --------------------------------------------------------------

async function pollStatus() {
  try {
    const res = await fetch('/api/status');
    if (!res.ok) throw new Error();
    const s = await res.json();
    const dot = $('#statusDot');
    const cur = s.current_refreshed_at;
    if (!cur || s.current_rows === 0) {
      dot.className = 'dot warn';
      $('#statusText').textContent = `Каталог: ${fmt(s.items)} предметов · сбор данных…`;
    } else {
      dot.className = 'dot ok';
      $('#statusText').textContent =
        `Цены: ${timeAgo(cur)} · история: ${timeAgo(s.history_refreshed_at)} · ${fmt(s.items)} предметов`;
    }
    $('#taxNote').textContent = `Налог продажи ЧР (премиум): ${(s.sales_tax * 100).toFixed(0)}%`;
  } catch {
    $('#statusDot').className = 'dot err';
    $('#statusText').textContent = 'Сервер недоступен';
  }
}

// ---- init -----------------------------------------------------------------

async function initMeta() {
  try {
    const res = await fetch('/api/meta');
    const m = await res.json();
    state.meta = m;
    state.cities = m.cities;
    const cat = $('#category');
    for (const c of m.categories) cat.insertAdjacentHTML('beforeend', `<option value="${c.id}">${c.label}</option>`);
    const tier = $('#tier');
    for (const t of m.tiers) tier.insertAdjacentHTML('beforeend', `<option value="${t}">T${t}</option>`);
    const q = $('#quality');
    for (const qq of m.qualities) q.insertAdjacentHTML('beforeend', `<option value="${qq.id}">${qq.label}</option>`);
  } catch {
    toast('Не удалось загрузить справочники', 'err');
  }
}

function wireEvents() {
  $('#mainTabs').addEventListener('click', (e) => {
    const btn = e.target.closest('.tab');
    if (!btn) return;
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
    btn.classList.add('active');
    state.view = btn.dataset.view;
    $('#minProfitWrap').style.display = state.view === 'flips' ? '' : 'none';
    load();
  });

  $('#windowSwitch').addEventListener('click', (e) => {
    const btn = e.target.closest('.win');
    if (!btn) return;
    document.querySelectorAll('.win').forEach((w) => w.classList.remove('active'));
    btn.classList.add('active');
    state.window = btn.dataset.window;
    load();
  });

  const deb = debounce(load, 350);
  $('#search').addEventListener('input', deb);
  $('#category').addEventListener('change', load);
  $('#tier').addEventListener('change', load);
  $('#quality').addEventListener('change', load);
  $('#minProfit').addEventListener('input', debounce(load, 500));

  $('#refreshBtn').addEventListener('click', async () => {
    const btn = $('#refreshBtn');
    btn.classList.add('spinning');
    try {
      await fetch('/api/refresh', { method: 'POST' });
      toast('Обновление данных запущено. Это займёт 1–2 минуты.', 'ok');
    } catch {
      toast('Не удалось запустить обновление', 'err');
    }
    setTimeout(() => btn.classList.remove('spinning'), 2000);
  });
}

(async function main() {
  wireEvents();
  await initMeta();
  await pollStatus();
  await load();
  setInterval(pollStatus, 20000);
})();
