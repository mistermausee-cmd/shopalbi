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
  const diff = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
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
async function fetchJSON(url, timeoutMs = 25000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, { signal: ctrl.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

// ---- state ----------------------------------------------------------------

const state = {
  view: 'flips',                 // 'flips' | 'cities' | 'stats'
  window: 'day',
  sort: {
    flips: { key: 'opportunity', dir: 'desc' },
    stats: { key: 'bm_volume', dir: 'desc' },
    cities: { key: 'score', dir: 'desc' },
    recommend: { key: 'total_profit', dir: 'desc' },
  },
  meta: null,
  cities: [],
  rows: [],                      // last loaded rows (for client-side sort of the cities tab)
  loading: false,
};

// columns whose natural first-click direction is ascending (lower = better / text)
const ASC_FIRST = new Set(['name', 'buy_city', 'city', 'best_item', 'buy_price', 'est', 'cheapest', 'unit_price', 'avg_price']);

// ---- cell renderers -------------------------------------------------------

function renderNameCell(r) {
  const q = `<span class="q-badge q${r.quality}" title="${r.quality_label}"></span>`;
  return `<div class="item-name">${q}${r.name}</div>` +
    `<div class="item-sub"><span class="pill tier">${r.tier_ench}</span> ` +
    `<span class="pill cat">${r.category_label}</span> · ${r.quality_label}</div>`;
}
function profitCls(v) { return v >= 0 ? 'profit-pos' : 'profit-neg'; }
function signed(v) { return `${v >= 0 ? '+' : ''}${fmt(v)}`; }
function renderScoreCell(v) {
  const col = scoreColor(v);
  return `<div class="score"><span class="score-bar"><i style="width:${Math.max(0, v)}%;background:${col}"></i></span>` +
    `<span class="score-val" style="color:${col}">${v}</span></div>`;
}

// ---- column definitions ---------------------------------------------------

function flipColumns() {
  return [
    { key: 'name', label: 'Предмет', left: true, render: renderNameCell },
    { key: 'opportunity', label: 'Выгодность', hint: 'Совокупная оценка: прибыль × надёжность',
      render: (r) => `<span class="num" style="color:var(--gold);font-weight:700">${fmt(r.opportunity)}</span>` },
    { key: 'buy_city', label: 'Купить в', left: true,
      render: (r) => `<span class="city-tag">${cityName(r.buy_city)}</span>` },
    { key: 'buy_price', label: 'Цена покупки', render: (r) => `<span class="num">${fmt(r.buy_price)}</span>` },
    { key: 'bm_buy_now', label: 'Выкуп ЧР', render: (r) => `<span class="num">${fmt(r.bm_buy_now)}</span>` },
    { key: 'profit', label: 'Прибыль',
      render: (r) => `<span class="num ${profitCls(r.profit)}">${signed(r.profit)}</span>` },
    { key: 'profit_pct', label: 'Прибыль %',
      render: (r) => `<span class="num ${profitCls(r.profit_pct)}">${r.profit_pct >= 0 ? '+' : ''}${r.profit_pct}%</span>` },
    { key: 'profit_window', label: 'Прибыль (за период)',
      render: (r) => `<span class="num ${profitCls(r.profit_window)}">${signed(r.profit_window)}</span>` +
        ` <span class="pct cell-dim">(${r.profit_pct_window}%)</span>` },
    { key: 'bm_volume', label: 'ЧР шт/день', render: (r) => `<span class="num cell-dim">${r.bm_daily_volume}</span>` },
    { key: 'est', label: '~Время выкупа', render: (r) => `<span class="cell-dim">${estTime(r.est_sell_hours)}</span>` },
    { key: 'reliability', label: 'Надёжность', render: (r) => renderScoreCell(r.reliability) },
  ];
}

function statsColumns() {
  const cols = [
    { key: 'name', label: 'Предмет', left: true, render: renderNameCell },
    { key: 'bm_avg', label: 'ЧР средняя', render: (r) => `<span class="num">${fmt(r.bm_avg)}</span>` },
    { key: 'bm_volume', label: 'ЧР объём', render: (r) => `<span class="num cell-dim">${fmt(r.bm_volume)}</span>` },
  ];
  for (const c of state.cities) {
    cols.push({
      key: 'city:' + c, label: cityName(c),
      render: (r) => {
        const e = r.cities[c];
        return e && e.avg ? `<span class="num">${fmt(e.avg)}</span>` : '<span class="cell-dim">—</span>';
      },
    });
  }
  cols.push({
    key: 'cheapest', label: 'Дешевле всего', left: true,
    render: (r) => r.cheapest_city
      ? `<span class="city-tag">${cityName(r.cheapest_city)}</span> <span class="num cell-dim">${fmt(r.cheapest_city_avg)}</span>`
      : '<span class="cell-dim">—</span>',
  });
  return cols;
}

function cityColumns() {
  return [
    { key: 'city', label: 'Город закупки', left: true, value: (r) => cityName(r.city),
      render: (r) => `<span class="city-tag" style="font-size:14px">${cityName(r.city)}</span>` },
    { key: 'score', label: 'Выгодность (сумма)', value: (r) => r.score, hint: 'Сумма выгодности по лучшим флипам города',
      render: (r) => `<span class="num" style="color:var(--gold);font-weight:700">${fmt(r.score)}</span>` },
    { key: 'flips_count', label: 'Флипов доступно', value: (r) => r.flips_count,
      render: (r) => `<span class="num">${fmt(r.flips_count)}</span>` },
    { key: 'top_profit_sum', label: 'Прибыль топ-100', value: (r) => r.top_profit_sum,
      render: (r) => `<span class="num profit-pos">${signed(r.top_profit_sum)}</span>` },
    { key: 'avg_profit_pct', label: 'Средний %', value: (r) => r.avg_profit_pct,
      render: (r) => `<span class="num">${r.avg_profit_pct}%</span>` },
    { key: 'avg_reliability', label: 'Ср. надёжность', value: (r) => r.avg_reliability,
      render: (r) => renderScoreCell(r.avg_reliability) },
    { key: 'best_item', label: 'Лучший предмет', left: true, value: (r) => r.best_item,
      render: (r) => `<div class="item-name">${r.best_item || '—'}</div>` +
        (r.best_item ? `<div class="item-sub profit-pos">${signed(r.best_profit)} (${r.best_profit_pct}%)</div>` : '') },
  ];
}

function recommendColumns() {
  return [
    { key: 'name', label: 'Предмет', left: true, value: (r) => r.name, render: renderNameCell },
    { key: 'qty', label: 'Купить, шт', value: (r) => r.qty,
      render: (r) => `<span class="num" style="color:var(--gold);font-weight:700">${fmt(r.qty)}</span>` },
    { key: 'unit_price', label: 'Мин. цена', value: (r) => r.unit_price,
      render: (r) => `<span class="num">${fmt(r.unit_price)}</span>` },
    { key: 'avg_price', label: 'Ср. цена скупки', value: (r) => r.avg_price, hint: 'С учётом роста цены при скупке нескольких лотов',
      render: (r) => `<span class="num">${fmt(r.avg_price)}</span>` },
    { key: 'total_cost', label: 'Затраты', value: (r) => r.total_cost,
      render: (r) => `<span class="num">${fmt(r.total_cost)}</span>` },
    { key: 'total_profit', label: 'Прибыль', value: (r) => r.total_profit,
      render: (r) => `<span class="num profit-pos">${signed(r.total_profit)}</span>` },
    { key: 'profit_pct', label: 'Прибыль %', value: (r) => r.profit_pct,
      render: (r) => `<span class="num ${profitCls(r.profit_pct)}">${r.profit_pct >= 0 ? '+' : ''}${r.profit_pct}%</span>` },
    { key: 'bm_daily_volume', label: 'ЧР шт/день', value: (r) => r.bm_daily_volume,
      render: (r) => `<span class="num cell-dim">${r.bm_daily_volume}</span>` },
    { key: 'reliability', label: 'Надёжность', value: (r) => r.reliability, render: (r) => renderScoreCell(r.reliability) },
  ];
}

function buildColumns() {
  if (state.view === 'flips') return flipColumns();
  if (state.view === 'cities') return cityColumns();
  if (state.view === 'recommend') return recommendColumns();
  return statsColumns();
}

// ---- rendering ------------------------------------------------------------

function renderHead(cols) {
  const s = state.sort[state.view];
  $('#thead').innerHTML = '<tr>' + cols.map((c) => {
    const sorted = c.key === s.key;
    const cls = [c.left ? 'left' : '', sorted ? 'sorted' : ''].join(' ').trim();
    const arrow = sorted
      ? ` <span class="arrow">${s.dir === 'asc' ? '▲' : '▼'}</span>`
      : ' <span class="arrow" style="opacity:.25">▽</span>';
    const title = c.hint ? ` title="${c.hint}"` : '';
    return `<th class="${cls}" data-sort="${c.key}"${title}>${c.label}${arrow}</th>`;
  }).join('') + '</tr>';

  $('#thead').querySelectorAll('th[data-sort]').forEach((th) => {
    th.addEventListener('click', () => onSort(th.dataset.sort));
  });
}

function onSort(key) {
  const s = state.sort[state.view];
  if (s.key === key) {
    s.dir = s.dir === 'asc' ? 'desc' : 'asc';
  } else {
    s.key = key;
    s.dir = ASC_FIRST.has(key) ? 'asc' : 'desc';
  }
  if (state.view === 'cities') {
    renderCurrent();          // client-side sort, no refetch
  } else {
    load();                   // server-side sort
  }
}

function sortedRows(cols) {
  if (state.view !== 'cities' && state.view !== 'recommend') return state.rows;  // server-sorted
  const s = state.sort.cities;
  const col = cols.find((c) => c.key === s.key);
  const rows = state.rows.slice();
  if (col && col.value) {
    rows.sort((a, b) => {
      const av = col.value(a), bv = col.value(b);
      let r = typeof av === 'string' ? av.localeCompare(bv, 'ru') : (av - bv);
      return s.dir === 'asc' ? r : -r;
    });
  }
  return rows;
}

function renderRows(cols, rows) {
  if (!rows.length) {
    $('#tbody').innerHTML = '';
    const e = $('#emptyState');
    e.hidden = false;
    e.textContent = state.view === 'flips'
      ? 'Флипов по текущим фильтрам нет. Снизьте мин. прибыль или дождитесь обновления данных.'
      : state.view === 'cities' ? 'Нет данных по городам под эти фильтры.'
      : state.view === 'recommend' ? 'Под этот бюджет и фильтры нечего рекомендовать. Увеличьте бюджет.'
      : 'Нет данных по этим фильтрам.';
    return;
  }
  $('#emptyState').hidden = true;
  $('#tbody').innerHTML = rows.map((r) =>
    '<tr>' + cols.map((c) => `<td class="${c.left ? 'left' : ''}">${c.render(r)}</td>`).join('') + '</tr>'
  ).join('');
}

function renderCurrent() {
  const cols = buildColumns();
  renderHead(cols);
  renderRows(cols, sortedRows(cols));
}

// ---- data ----------------------------------------------------------------

function currentFilters() {
  const p = new URLSearchParams();
  p.set('window', state.window);
  const search = $('#search').value.trim();
  const category = $('#category').value;
  const tier = $('#tier').value;
  const quality = $('#quality').value;
  if (search) p.set('search', search);
  if (category) p.set('category', category);
  if (tier) p.set('tier', tier);
  if (quality) p.set('quality', quality);

  if (state.view === 'cities') {
    p.set('top_n', '100');
    const mp = $('#minProfit').value;
    if (mp !== '') p.set('min_profit', mp);
    return p;
  }

  if (state.view === 'recommend') {
    p.set('budget', $('#budget').value || '0');
    const bc = $('#buyCity').value;
    if (bc) p.set('city', bc);
    const mp = $('#minProfit').value;
    if (mp !== '') p.set('min_profit', mp);
    return p;
  }

  p.set('limit', '300');
  const s = state.sort[state.view];
  p.set('sort', s.key);
  p.set('direction', s.dir);
  if (state.view === 'flips') {
    const mp = $('#minProfit').value;
    if (mp !== '') p.set('min_profit', mp);
    const bc = $('#buyCity').value;
    if (bc) p.set('buy_city', bc);
  }
  return p;
}

const ENDPOINT = { flips: '/api/flips', cities: '/api/cities', stats: '/api/stats', recommend: '/api/recommend' };

async function load() {
  if (state.loading) return;
  state.loading = true;
  const showOverlay = !$('#tbody').children.length;
  if (showOverlay) $('#loader').hidden = false;
  try {
    const data = await fetchJSON(`${ENDPOINT[state.view]}?${currentFilters().toString()}`);
    state.rows = state.view === 'recommend' ? ((data.best && data.best.items) || []) : (data.rows || []);
    renderCurrent();
    updateMetaLine(data);
  } catch (err) {
    const msg = err.name === 'AbortError' ? 'сервер не ответил вовремя' : err.message;
    toast('Ошибка загрузки: ' + msg, 'err');
    if (!$('#tbody').children.length) {
      $('#emptyState').hidden = false;
      $('#emptyState').textContent = 'Не удалось загрузить данные.';
    }
  } finally {
    state.loading = false;
    $('#loader').hidden = true;
  }
}

function updateMetaLine(data) {
  const wl = { day: 'день', '3d': '3 дня', week: 'неделю', month: 'месяц' }[state.window];
  if (state.view === 'flips') {
    const where = data.buy_city ? ` · закупка в <b>${cityName(data.buy_city)}</b>` : ' · лучший город по каждому предмету';
    $('#metaLine').innerHTML =
      `Найдено флипов: <b>${data.total}</b>${where} · выручка после налога <b>${((1 - data.sales_tax) * 100).toFixed(0)}%</b>` +
      ` · «Выгодность» = прибыль × надёжность, «Прибыль (за период)» — по средней цене ЧР за ${wl}.`;
  } else if (state.view === 'cities') {
    $('#metaLine').innerHTML =
      `Города отсортированы по совокупной выгодности (сумма по лучшим 100 флипам города) за ${wl}. ` +
      `Начинай с верхнего — там больше всего выгодных вещей.`;
  } else if (state.view === 'recommend') {
    const b = data.best;
    if (!b || !b.items_count) {
      $('#metaLine').innerHTML =
        `Под бюджет <b>${fmt(data.budget)}</b> выгодных закупок не найдено. Увеличь бюджет, снизь мин. прибыль или выбери другой город.`;
    } else {
      const cmp = data.cities.slice(0, 6)
        .map((c) => `${cityName(c.city)}: <b>+${fmt(c.expected_profit)}</b>`).join(' · ');
      const taxPct = ((data.sales_tax + data.setup_fee) * 100).toFixed(1);
      $('#metaLine').innerHTML =
        `Лучший город: <b>${cityName(data.city)}</b> · закупка на <b>${fmt(b.spent)}</b> → ожидаемая прибыль ` +
        `<b class="profit-pos">+${fmt(b.expected_profit)}</b> (ROI ${b.roi_pct}%) · остаток <b>${fmt(b.leftover)}</b> · позиций ${b.items_count}.` +
        `<br><span class="cell-dim">Сравнение городов: ${cmp}. Учтён налог+сбор ${taxPct}% и рост цены при скупке. ` +
        `Остался бюджет — впиши остаток и обнови рынок, пересчитаю по актуальным ценам.</span>`;
    }
  } else {
    $('#metaLine').innerHTML =
      `Показано предметов: <b>${data.rows.length}</b> из <b>${data.total}</b> · ` +
      `цены — средневзвешенные по объёму за ${wl}. Клик по городу — сортировка по его цене.`;
  }
}

// ---- status --------------------------------------------------------------

async function pollStatus() {
  try {
    const s = await fetchJSON('/api/status', 8000);
    const dot = $('#statusDot');
    if (!s.current_refreshed_at || s.current_rows === 0) {
      dot.className = 'dot warn';
      $('#statusText').textContent = `Каталог: ${fmt(s.items)} предметов · сбор данных…`;
    } else {
      dot.className = 'dot ok';
      $('#statusText').textContent =
        `Цены: ${timeAgo(s.current_refreshed_at)} · история: ${timeAgo(s.history_refreshed_at)} · ${fmt(s.items)} предметов`;
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
    const m = await fetchJSON('/api/meta');
    state.meta = m;
    state.cities = m.cities;
    const buyCity = $('#buyCity');
    for (const c of (m.buy_cities || m.cities)) buyCity.insertAdjacentHTML('beforeend', `<option value="${c}">${cityName(c)}</option>`);
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

function updateControlsVisibility() {
  const v = state.view;
  // buy-city applies to flips and the recommender (there it picks / fixes the city)
  $('#buyCity').style.display = (v === 'flips' || v === 'recommend') ? '' : 'none';
  $('#minProfitWrap').style.display = v === 'stats' ? 'none' : '';
  $('#budgetWrap').style.display = v === 'recommend' ? '' : 'none';
}

function wireEvents() {
  $('#mainTabs').addEventListener('click', (e) => {
    const btn = e.target.closest('.tab');
    if (!btn) return;
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
    btn.classList.add('active');
    state.view = btn.dataset.view;
    state.rows = [];
    $('#tbody').innerHTML = '';
    updateControlsVisibility();
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
  $('#buyCity').addEventListener('change', load);
  $('#category').addEventListener('change', load);
  $('#tier').addEventListener('change', load);
  $('#quality').addEventListener('change', load);
  $('#minProfit').addEventListener('input', debounce(load, 500));
  $('#budget').addEventListener('input', debounce(load, 500));

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
  updateControlsVisibility();
  await initMeta();
  await pollStatus();
  await load();
  setInterval(pollStatus, 20000);
})();
