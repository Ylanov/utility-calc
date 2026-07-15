// static/js/modules/registry.js
// Единый реестр показаний (Фаза 2): ОДИН список из боевых MeterReading и буфера
// GSheetsImportRow в общем формате, с бейджем источника. Данные — /api/admin/registry.
// Вид и поведение — по образцу старого gsheets-реестра (бейджи STATUS_META,
// янтарная подсветка сомнительных строк, отклонение с уведомлением жильцу).

import { api } from '../core/api.js';
import { toast, showConfirm, showPrompt } from '../core/dom.js';
import { GSheetsModule } from './gsheets.js';

function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// Чистим технические коды из ошибки сервера — оставляем человеческое объяснение.
function cleanError(e) {
  let m = String(e && e.message ? e.message : (e || 'Ошибка'));
  m = m.replace(/manual_approve_blocked:\s*/gi, '')
       .replace(/high_delta_or_baseline_overflow:\s*/gi, '')
       .replace(/meter_decreased:\s*/gi, '')
       .replace(/total_cost_too_high:\s*/gi, '')
       .replace(/baseline_overflow:\s*/gi, '');
  return m.trim();
}

// Палитра статусов — как STATUS_META старого gsheets-реестра (привычна админу).
const STATUS = {
  draft:         { t: 'Черновик',       c: '#92400e', b: '#fef3c7' },
  pending:       { t: 'В ожидании',     c: '#3b82f6', b: '#dbeafe' },
  conflict:      { t: 'Конфликт',       c: '#f59e0b', b: '#fef3c7' },
  unmatched:     { t: 'Не найден',      c: '#ef4444', b: '#fee2e2' },
  auto_approved: { t: 'Авто-утв.',      c: '#8b5cf6', b: '#ede9fe' },
  approved:      { t: 'Утверждено',     c: '#10b981', b: '#d1fae5' },
  rejected:      { t: 'Отклонено',      c: '#6b7280', b: '#f3f4f6' },
};
// Источники — теперь ЧЕСТНЫЕ (колонка readings.source, reading_source_001):
// подача жильца по QR и правка/ввод админа — разные вещи.
const SRC = {
  qr:      { t: '📱 QR-портал',      c: '#1e40af', b: '#dbeafe' },
  admin:   { t: '✍️ Админ',          c: '#334155', b: '#e2e8f0' },
  gsheets: { t: '📄 Google Sheets',  c: '#166534', b: '#dcfce7' },
  buffer:  { t: '📄 Sheets (буфер)', c: '#92400e', b: '#fef3c7' },
  auto:    { t: '🤖 Норматив/авто',  c: '#6b21a8', b: '#f3e8ff' },
  excel:   { t: '📗 Excel',          c: '#0f766e', b: '#ccfbf1' },
  saldo:   { t: '₽ Сальдо 1С',       c: '#0e7490', b: '#cffafe' },
};

// Цвета сегментов день-графика (по источникам).
const DAY_COLORS = { qr: '#3b82f6', admin: '#475569', gsheets: '#16a34a', auto: '#8b5cf6', other: '#06b6d4' };

// Число колонок таблицы — ЕДИНСТВЕННАЯ точка правды для colspan
// (раньше «11» было захардкожено в 6 местах и ломалось при смене колонок).
const COLS = 10;

// Чипы-фильтры по статусу (порядок = порядок отображения).
const CHIPS = [
  ['', 'Все'],
  ['draft', 'Черновики'],
  ['pending', 'В ожидании'],
  ['conflict', 'Конфликты'],
  ['unmatched', 'Не найдены'],
  ['auto_approved', 'Авто-утв.'],
  ['approved', 'Утверждено'],
];

function badge(meta, fallback) {
  var m = meta || { t: fallback || '—', c: '#475569', b: '#f1f5f9' };
  return '<span style="display:inline-block; padding:2px 8px; border-radius:12px; font-size:11px; font-weight:600; background:' + m.b + '; color:' + m.c + '; white-space:nowrap;">' + esc(m.t) + '</span>';
}

// «00123.450» → «123,45» (как fmtNum в gsheets — без хвостовых нулей).
function fmtNum(v) {
  if (v === null || v === undefined || v === '') return '—';
  var n = parseFloat(v);
  if (isNaN(n)) return esc(v);
  return n.toLocaleString('ru-RU', { maximumFractionDigits: 3 });
}

// Янтарная/красная подсветка строки (по образцу gsheets: match_score<85 →
// едва заметный градиент; для боевых — по уровню риска anomaly_score).
function rowTint(r) {
  if (r.row_type === 'gsheets') {
    var ms = (r.matched && r.matched.score) || 0;
    if (ms > 0 && ms < 85) return 'background: linear-gradient(to right, rgba(245,158,11,0.07) 0%, transparent 100%);';
    if (r.status === 'unmatched' || r.status === 'conflict') return 'background: linear-gradient(to right, rgba(239,68,68,0.05) 0%, transparent 100%);';
    return '';
  }
  var sc = r.anomaly_score || 0;
  if (r.status !== 'approved' && sc >= 80) return 'background: linear-gradient(to right, rgba(239,68,68,0.07) 0%, transparent 100%);';
  if (r.status !== 'approved' && sc >= 30) return 'background: linear-gradient(to right, rgba(245,158,11,0.07) 0%, transparent 100%);';
  return '';
}

export const RegistryModule = {
  isInitialized: false,
  dom: {},
  state: { source: '', status: '', search: '', period_id: null, show_saldo: false, day: '', page: 1, limit: 50 },

  init() {
    this.cacheDom();
    if (!this.isInitialized) { this.bind(); this.isInitialized = true; }
    this.load();
  },
  refresh() { this.load(); },

  cacheDom() {
    this.dom = {
      body: document.getElementById('registryBody'),
      src: document.getElementById('registrySource'),
      search: document.getElementById('registrySearch'),
      total: document.getElementById('registryTotal'),
      refresh: document.getElementById('btnRegistryRefresh'),
      manualEntry: document.getElementById('btnManualEntry'),
      period: document.getElementById('registryPeriod'),
      days: document.getElementById('registryDays'),
      chips: document.getElementById('registryStatusChips'),
      showSaldo: document.getElementById('registryShowSaldo'),
      pager: document.getElementById('registryPager'),
      pagePrev: document.getElementById('registryPrev'),
      pageNext: document.getElementById('registryNext'),
      pageLabel: document.getElementById('registryPageLabel'),
    };
  },

  bind() {
    this.dom.src?.addEventListener('change', () => { this.state.source = this.dom.src.value; this.state.page = 1; this.load(); });
    this.dom.period?.addEventListener('change', () => { this.state.period_id = this.dom.period.value || null; this.state.page = 1; this.load(); });
    this.dom.showSaldo?.addEventListener('change', () => { this.state.show_saldo = !!this.dom.showSaldo.checked; this.state.page = 1; this.load(); });
    this.dom.refresh?.addEventListener('click', () => this.load());
    // «➕ Ручной ввод» — модалка мульти-ввода (бывшая секция «Операций»,
    // объединение 2026-07-15): создание показаний с нуля и доввод задним
    // числом. Открывается на периоде, выбранном в реестре; после
    // утверждений список тихо обновляется.
    this.dom.manualEntry?.addEventListener('click', async () => {
      // Периодов нет (первый load упал/ещё идёт) — пробуем догрузить сами,
      // а не отправляем админа гадать.
      if (!this._periods || !this._periods.length) await this.load();
      if (!this._periods || !this._periods.length) {
        toast('Не удалось загрузить периоды — проверьте связь и нажмите ⟳', 'error');
        return;
      }
      try {
        const { ManualEntryModal } = await import('./manual_entry.js');
        ManualEntryModal.open({
          periods: this._periods,
          defaultPeriodId: this.state.period_id || this._activePeriodId,
          onSaved: () => this.softReload(null),
        });
      } catch (e) { toast('Не удалось открыть ручной ввод: ' + (e.message || e), 'error'); }
    });
    this.dom.pagePrev?.addEventListener('click', () => { if (this.state.page > 1) { this.state.page--; this.load(); } });
    this.dom.pageNext?.addEventListener('click', () => { this.state.page++; this.load(); });
    // Чипы статусов — делегировано на контейнер.
    this.dom.chips?.addEventListener('click', (e) => {
      var chip = e.target.closest('[data-reg-chip]');
      if (!chip) return;
      this.state.status = chip.dataset.regChip || '';
      this.state.page = 1;
      this.load();
    });
    // День-график: клик по дню = фильтр «что прислали в этот день»;
    // повторный клик по активному дню — сброс.
    this.dom.days?.addEventListener('click', (e) => {
      var cell = e.target.closest('[data-reg-day]');
      if (!cell) return;
      var d = cell.dataset.regDay;
      this.state.day = (this.state.day === d) ? '' : d;
      this.state.page = 1;
      this.load();
    });
    // Делегированный клик: инлайн-правка ячеек счётчиков + история по ФИО.
    this.dom.body?.addEventListener('click', (e) => this._onBodyClick(e));
    let t = null;
    this.dom.search?.addEventListener('input', () => {
      clearTimeout(t);
      t = setTimeout(() => { this.state.search = this.dom.search.value.trim(); this.state.page = 1; this.load(); }, 350);
    });
  },

  _query() {
    return '/admin/registry?page=' + this.state.page + '&limit=' + this.state.limit +
      (this.state.source ? '&source=' + encodeURIComponent(this.state.source) : '') +
      (this.state.status ? '&status=' + encodeURIComponent(this.state.status) : '') +
      (this.state.period_id ? '&period_id=' + encodeURIComponent(this.state.period_id) : '') +
      (this.state.show_saldo ? '&show_saldo=true' : '') +
      (this.state.day ? '&day=' + encodeURIComponent(this.state.day) : '') +
      (this.state.search ? '&search=' + encodeURIComponent(this.state.search) : '');
  },

  // Полная загрузка (первый вход/смена фильтров): с плейсхолдером.
  async load() {
    if (!this.dom.body) return;
    this.dom.body.innerHTML = '<tr><td colspan="' + COLS + '" style="padding:30px; text-align:center; color:var(--text-secondary);"><i class="fa-solid fa-spinner fa-spin"></i> Загрузка…</td></tr>';
    let data;
    try { data = await api.get(this._query()); }
    catch (e) { this.dom.body.innerHTML = '<tr><td colspan="' + COLS + '" style="padding:24px; text-align:center; color:var(--danger-color);">Ошибка: ' + esc(e.message || e) + '</td></tr>'; return; }
    this.renderControls(data);
    this.render(data);
  },

  // ТИХОЕ обновление после точечного действия (правка ячейки/утверждение/
  // отклонение): без «Загрузка…», с сохранением прокрутки и подсветкой
  // обновлённой строки — админ не теряет место в списке (жалоба 2026-07-14:
  // «одну ГВС обновил — страница обновилась, ищи того, кого редактировал»).
  async softReload(rowKey) {
    if (!this.dom.body) return this.load();
    var scroller = this.dom.body.closest('.table-responsive');
    var winY = window.scrollY;
    var st = scroller ? scroller.scrollTop : 0;
    let data;
    try { data = await api.get(this._query()); }
    catch (e) {
      // Точечное действие уже показало свой тост об УСПЕХЕ — но список
      // остался старым, об этом честно предупреждаем (иначе админ решит,
      // что сохранение не прошло, и введёт повторно).
      toast('Список не обновился (сеть?) — нажмите ⟳', 'warning');
      return;
    }
    this.renderControls(data);
    this.render(data);
    if (scroller) scroller.scrollTop = st;
    window.scrollTo(0, winY);
    if (rowKey && rowKey.id != null) {
      var tr = this.dom.body.querySelector('tr[data-rt="' + rowKey.rt + '"][data-id="' + rowKey.id + '"]');
      if (tr) {
        tr.style.transition = 'background-color 1.2s ease';
        tr.style.backgroundColor = 'rgba(16,185,129,0.20)';
        setTimeout(function () { tr.style.backgroundColor = ''; }, 900);
      }
    }
  },

  // Период-селектор + чипы статусов + пагинация.
  renderControls(data) {
    // Периоды и активный период — для модалки «➕ Ручной ввод».
    this._periods = data.periods || this._periods || [];
    this._activePeriodId = data.period_id != null ? data.period_id : this._activePeriodId;
    if (this.dom.period && data.periods && this.dom.period.options.length !== data.periods.length) {
      this.dom.period.innerHTML = data.periods.map(function (p) {
        return '<option value="' + p.id + '"' + (p.id === data.period_id ? ' selected' : '') + '>' +
          esc(p.name) + (p.is_active ? ' · актив' : '') + '</option>';
      }).join('');
    } else if (this.dom.period && data.period_id != null) {
      this.dom.period.value = String(data.period_id);
    }

    // ГРАФИК МЕСЯЦА: колонки-дни (стек по источникам), клик = фильтр дня.
    if (this.dom.days) {
      var daysMap = data.days || {};
      var dates = Object.keys(daysMap).sort();
      if (!dates.length) {
        this.dom.days.innerHTML = '';
        this.dom.days.style.display = 'none';
      } else {
        var maxN = 1;
        dates.forEach(function (d) { if (daysMap[d].total > maxN) maxN = daysMap[d].total; });
        var activeDay = this.state.day;
        var BAR_H = 52;
        var cells = dates.map(function (d) {
          var b = daysMap[d];
          var h = Math.max(3, Math.round(BAR_H * b.total / maxN));
          var segs = '';
          ['qr', 'admin', 'gsheets', 'auto', 'other'].forEach(function (k) {
            if (!b[k]) return;
            var sh = Math.max(2, Math.round(h * b[k] / b.total));
            segs += '<div style="height:' + sh + 'px; background:' + DAY_COLORS[k] + ';"></div>';
          });
          var parts = d.split('-');
          var label = parts[2] + '.' + parts[1];
          var isActive = (activeDay === d);
          var tip = label + ': всего ' + b.total +
            (b.qr ? ' · QR ' + b.qr : '') + (b.admin ? ' · админ ' + b.admin : '') +
            (b.gsheets ? ' · Sheets ' + b.gsheets : '') + (b.auto ? ' · авто ' + b.auto : '') +
            (b.other ? ' · прочее ' + b.other : '');
          return '<button type="button" data-reg-day="' + d + '" title="' + esc(tip) + '" ' +
            'style="display:flex; flex-direction:column; align-items:center; justify-content:flex-end; gap:3px; ' +
            'border:1.5px solid ' + (isActive ? 'var(--primary-color)' : 'transparent') + '; cursor:pointer; ' +
            'background:' + (isActive ? 'rgba(59,130,246,0.10)' : 'transparent') + '; border-radius:8px; padding:5px 4px 3px; min-width:38px;">' +
            '<span style="font-size:10px; color:var(--text-secondary);">' + b.total + '</span>' +
            '<div style="width:22px; height:' + h + 'px; display:flex; flex-direction:column; justify-content:flex-end; border-radius:3px; overflow:hidden;">' + segs + '</div>' +
            '<span style="font-size:10px; font-weight:' + (isActive ? '700' : '400') + '; color:' + (isActive ? 'var(--primary-color)' : 'var(--text-secondary)') + ';">' + label + '</span>' +
            '</button>';
        }).join('');
        var legend = ['qr|QR-портал', 'admin|Админ', 'gsheets|Sheets', 'auto|Авто', 'other|Прочее'].map(function (p) {
          var kv = p.split('|');
          return '<span style="display:inline-flex; align-items:center; gap:4px; font-size:11px; color:var(--text-secondary);">' +
            '<span style="width:9px; height:9px; border-radius:2px; background:' + DAY_COLORS[kv[0]] + ';"></span>' + kv[1] + '</span>';
        }).join('');
        this.dom.days.style.display = 'block';
        this.dom.days.innerHTML =
          '<div style="display:flex; align-items:flex-end; gap:2px; overflow-x:auto; padding:4px 2px;">' + cells + '</div>' +
          '<div style="display:flex; gap:12px; margin-top:4px; align-items:center;">' + legend +
          (activeDay ? '<button type="button" data-reg-day="' + esc(activeDay) + '" style="margin-left:auto; border:none; background:none; color:var(--primary-color); cursor:pointer; font-size:12px;">✕ сбросить день</button>' : '') +
          '</div>';
      }
    }

    if (this.dom.chips) {
      var st = this.state.status;
      var counts = data.counts || {};
      var totalAll = Object.values(counts).reduce(function (a, b) { return a + b; }, 0);
      this.dom.chips.innerHTML = CHIPS.map(function (c) {
        var key = c[0], label = c[1];
        var n = key === '' ? totalAll : (counts[key] || 0);
        if (key !== '' && !n && st !== key) return '';   // пустые чипы не показываем
        var active = (st === key);
        var meta = STATUS[key];
        var color = active ? '#fff' : (meta ? meta.c : 'var(--text-secondary)');
        var bg = active ? 'var(--primary-color)' : (meta ? meta.b : 'var(--bg-secondary, #f1f5f9)');
        return '<button type="button" data-reg-chip="' + key + '" style="border:none; cursor:pointer; ' +
          'padding:4px 10px; border-radius:14px; font-size:12px; font-weight:600; background:' + bg + '; color:' + color + ';">' +
          esc(label) + ' <span style="opacity:.75;">' + n + '</span></button>';
      }).join('');
    }

    if (this.dom.pager) {
      var pages = Math.max(1, Math.ceil((data.total || 0) / this.state.limit));
      if (this.state.page > pages) this.state.page = pages;
      this.dom.pager.style.display = pages > 1 ? 'flex' : 'none';
      if (this.dom.pageLabel) this.dom.pageLabel.textContent = 'стр. ' + (data.page || 1) + ' из ' + pages;
      if (this.dom.pagePrev) this.dom.pagePrev.disabled = (data.page || 1) <= 1;
      if (this.dom.pageNext) this.dom.pageNext.disabled = (data.page || 1) >= pages;
    }
  },

  render(data) {
    var items = (data && data.items) || [];
    if (this.dom.total) this.dom.total.textContent = (data.period ? 'Период «' + data.period + '» · ' : '') + 'всего: ' + (data.total || 0);
    if (!items.length) {
      this.dom.body.innerHTML = '<tr><td colspan="' + COLS + '" style="padding:30px; text-align:center; color:var(--text-secondary);">Нет записей по фильтру.</td></tr>';
      return;
    }
    this.dom.body.innerHTML = items.map((r) => {
      var when = r.timestamp ? new Date(r.timestamp).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' }) : '—';

      // user_id для инлайн-правки и истории: у боевых — r.user_id, у буфера — matched.
      var uid = (r.row_type === 'reading') ? r.user_id : (r.matched && r.matched.user_id);

      // ФИО + под ним: общага серым; для буфера — итог сопоставления.
      // Клик по ФИО → история показаний жильца (если есть user_id).
      var fioCell = uid
        ? '<b data-reg-fio="' + uid + '" style="cursor:pointer; border-bottom:1px dashed var(--primary-color);" title="Показать прошлые показания">' + esc(r.fio || '—') + '</b>'
        : '<b>' + esc(r.fio || '—') + '</b>';
      if (r.dormitory) fioCell += '<br><span style="font-size:11px; color:var(--text-secondary);">' + esc(r.dormitory) + '</span>';
      if (r.row_type === 'gsheets' && r.matched) {
        if (r.matched.user_id) {
          fioCell += '<br><span style="font-size:11px; color:var(--primary-color);">→ ' + esc(r.matched.fio || ('жилец #' + r.matched.user_id)) +
            (r.matched.room ? ' (к. ' + esc(r.matched.room) + ')' : '') + ' · ' + (r.matched.score || 0) + '%</span>';
        } else {
          fioCell += '<br><span style="font-size:11px; color:#ef4444;"><i class="fa-solid fa-user-xmark"></i> жилец не найден</span>';
        }
        if (r.status === 'conflict' && r.matched.reason) {
          fioCell += '<br><span style="font-size:11px; color:#f59e0b;"><i class="fa-solid fa-triangle-exclamation"></i> ' + esc(r.matched.reason) + '</span>';
        }
      }

      var statusCell = badge(STATUS[r.status], r.status);
      if (r.anomaly_score) {
        statusCell += '<br><span style="font-size:10px; color:' + (r.anomaly_score >= 80 ? '#991b1b' : '#92400e') + ';" title="Риск-балл аномальности">риск ' + r.anomaly_score + '</span>';
      }

      var sum = (r.sum != null && r.sum !== 0) ? Number(r.sum).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' ₽' : '—';
      // Сальдо-заглушки 1С (носители долга без показаний): утверждать/
      // отклонять/править нечего — read-only строка.
      var isSaldo = (r.source === 'saldo');
      var canApprove = !isSaldo && (r.status === 'draft' || r.status === 'pending' || r.status === 'auto_approved');
      var canReject = !isSaldo && ((r.row_type === 'gsheets')
        ? (r.status !== 'approved' && r.status !== 'rejected')
        : (r.status === 'draft'));
      var act = '';
      if (canApprove) {
        act += '<button class="action-btn success-btn" style="padding:3px 8px; font-size:11px;" data-reg-approve data-rt="' + r.row_type + '" data-id="' + r.id + '" title="Утвердить как есть"><i class="fa-solid fa-check"></i></button> ';
      }
      if (r.row_type === 'gsheets' && r.status !== 'approved' && r.status !== 'rejected') {
        act += '<button class="action-btn secondary-btn" style="padding:3px 8px; font-size:11px;" data-reg-reassign data-id="' + r.id + '" title="Переназначить жильца"><i class="fa-solid fa-user-pen"></i></button> ';
      }
      if (canReject) {
        act += '<button class="action-btn danger-btn" style="padding:3px 8px; font-size:11px;" data-reg-reject data-rt="' + r.row_type + '" data-id="' + r.id + '" title="Отклонить — жилец получит уведомление на QR-портале"><i class="fa-solid fa-xmark"></i></button>';
      }

      // Инлайн-правка счётчиков: боевые показания (все 3) ИЛИ строка буфера
      // GSheets. В буфере электричества НЕТ (гугл-форма шлёт только воду) —
      // но если жилец сопоставлен, клик по «Свет» пишет электричество боевым
      // показанием этого жильца в активный месяц (тот же путь, что «Ручной
      // ввод») — админ правит всё из одного места.
      var rawAttr = function (v) { return v == null ? '' : esc(String(v)); };
      var editReading = (r.row_type === 'reading' && r.user_id != null && !isSaldo);
      var editBuffer = (r.row_type === 'gsheets' && r.status !== 'approved' && r.status !== 'rejected');
      var bufMuid = (editBuffer && r.matched && r.matched.user_id) ? r.matched.user_id : null;
      var editable = editReading || editBuffer;
      var trAttrs = editable
        ? ' data-rt="' + r.row_type + '" data-id="' + r.id + '"' +
          (editReading ? ' data-uid="' + r.user_id + '" data-pid="' + (r.period_id || '') + '"' : '') +
          (bufMuid ? ' data-muid="' + bufMuid + '"' : '') +
          ' data-hot="' + rawAttr(r.hot) + '" data-cold="' + rawAttr(r.cold) + '" data-elect="' + rawAttr(r.elect) + '"'
        : '';

      // Δ к прошлому месяцу — мелкой строкой под значением (зелёная норма /
      // серый ноль; отрицательная — красным, счётчик не может уменьшаться).
      var deltaHtml = function (d) {
        if (d == null) return '';
        var v = Number(d);
        var col = v < 0 ? '#dc2626' : (v === 0 ? '#9ca3af' : '#16a34a');
        var txt = (v > 0 ? '+' : '') + v.toLocaleString('ru-RU', { maximumFractionDigits: 2 });
        return '<br><span style="font-size:10px; color:' + col + ';" title="Расход за месяц (к прошлому показанию)">Δ ' + txt + '</span>';
      };
      // Оснащённость помещения счётчиками (бэкенд отдаёт r.meters для боевых
      // строк): в клетку несуществующего счётчика значение не впишешь —
      // раньше inline-правка это позволяла (перенос проверки из ручного
      // ввода). Вода гейтится КАК ПАРА (ГВС+ХВС подаются вместе): клетки
      // открыты, пока есть хотя бы один водяной счётчик — иначе комната с
      // одним счётчиком попадает в тупик «заполните оба».
      var meterOn = { hot: true, cold: true, elect: true };
      if (r.row_type === 'reading' && r.meters) {
        var waterOn = !!(r.meters.hw || r.meters.cw);
        meterOn = { hot: waterOn, cold: waterOn, elect: !!r.meters.el };
      }
      var meterTd = function (field, val, delta) {
        // Свет у буфера редактируем только при сопоставленном жильце.
        var cellEditable = editable && meterOn[field] && !(editBuffer && field === 'elect' && !bufMuid);
        var hint = !meterOn[field]
          ? 'У помещения нет этого счётчика'
          : (editBuffer && field === 'elect')
            ? 'В гугл-форме света нет — значение запишется показанием жильца в активный месяц'
            : 'Нажми, чтобы изменить показание';
        var inner = fmtNum(val) + deltaHtml(delta);
        return cellEditable
          ? '<td class="reg-edit" data-field="' + field + '" data-raw="' + rawAttr(val) + '" style="text-align:right; font-family:monospace; cursor:text; line-height:1.25;" title="' + hint + '">' + inner + '</td>'
          : '<td style="text-align:right; font-family:monospace; line-height:1.25;"' + (!meterOn[field] ? ' title="' + hint + '"' : '') + '>' + inner + '</td>';
      };

      // Тариф из колонки переехал в тултип ФИО (меньше шума в таблице).
      var fioTitle = r.tariff ? ' title="Тариф: ' + esc(r.tariff) + '"' : '';

      // ✏️ — админ правил запись после создания (честная метка admin_edited).
      var srcCell = badge(SRC[r.source], r.source);
      if (r.admin_edited && r.source !== 'admin') {
        srcCell += ' <span title="Админ правил эту запись" style="font-size:12px; cursor:help;">✏️</span>';
      }

      return '<tr style="' + rowTint(r) + '"' + trAttrs + '>' +
        '<td style="font-size:12px; color:var(--text-secondary); white-space:nowrap;">' + esc(when) + '</td>' +
        '<td>' + srcCell + '</td>' +
        '<td' + fioTitle + '>' + fioCell + '</td>' +
        '<td style="font-size:13px;">' + esc(r.room || '—') + '</td>' +
        meterTd('hot', r.hot, r.delta_hot) +
        meterTd('cold', r.cold, r.delta_cold) +
        meterTd('elect', r.elect, r.delta_elect) +
        '<td>' + statusCell + '</td>' +
        '<td style="text-align:right; color:#15803d; font-weight:600; white-space:nowrap;">' + sum + '</td>' +
        '<td style="text-align:right; white-space:nowrap;">' + act + '</td>' +
        '</tr>';
    }).join('');

    this.dom.body.querySelectorAll('[data-reg-approve]').forEach((btn) => {
      btn.addEventListener('click', () => this.approve(btn.dataset.rt, btn.dataset.id, btn));
    });
    this.dom.body.querySelectorAll('[data-reg-reject]').forEach((btn) => {
      btn.addEventListener('click', () => this.reject(btn.dataset.rt, btn.dataset.id, btn));
    });
    this.dom.body.querySelectorAll('[data-reg-reassign]').forEach((btn) => {
      btn.addEventListener('click', () => this.reassign(btn.dataset.id));
    });
  },

  // ---- Инлайн-правка ячеек + история по ФИО (делегировано на tbody) ----
  _onBodyClick(e) {
    var cell = e.target.closest('td.reg-edit');
    if (cell && !cell.querySelector('input')) { this._editCell(cell); return; }
    var fio = e.target.closest('[data-reg-fio]');
    if (fio) { this._toggleHistory(fio.dataset.regFio, fio); return; }
  },

  _editCell(td) {
    var tr = td.closest('tr');
    var field = td.dataset.field;
    var raw = td.dataset.raw || '';
    var old = td.innerHTML;
    td.innerHTML = '<input type="text" inputmode="decimal" value="' + esc(raw) + '" ' +
      'style="width:92px; text-align:right; font-family:monospace; padding:3px 5px; border:1px solid var(--primary-color); border-radius:4px;">';
    var inp = td.querySelector('input');
    inp.focus(); inp.select();
    var done = false;
    var self = this;
    var cancel = function () { if (done) return; done = true; td.innerHTML = old; };
    var save = function () { if (done) return; done = true; self._saveCell(tr, td, field, inp.value, old); };
    inp.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); save(); }
      else if (ev.key === 'Escape') { ev.preventDefault(); cancel(); }
    });
    inp.addEventListener('blur', save);
  },

  async _saveCell(tr, td, field, valueStr, oldHtml) {
    var num = function (s) { var v = parseFloat(String(s).replace(',', '.')); return Number.isFinite(v) ? v : null; };

    // Строка буфера GSheets — правим её значение (ГВС/ХВС), без пересчёта;
    // конфликт сбрасывается, статус → pending (переоценится при утверждении).
    // «Свет»: в буфере электричества НЕТ — пишем его боевым показанием
    // сопоставленного жильца в активный месяц (save_manual_entry, элект.
    // подаётся независимо от воды).
    if (tr.dataset.rt === 'gsheets') {
      var bv = num(valueStr);
      if (bv == null || bv < 0) { td.innerHTML = oldHtml; return; }
      if (field === 'elect') {
        var muid = Number(tr.dataset.muid);
        if (!muid) { td.innerHTML = oldHtml; return; }
        td.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
        try {
          await api.post('/admin/readings/manual', { user_id: muid, electricity: bv });
          toast('Свет записан показанием жильца (активный месяц)', 'success');
          this.softReload({ rt: 'gsheets', id: tr.dataset.id });
        } catch (e) {
          toast('Ошибка: ' + (e.message || e), 'error');
          this.softReload(null);
        }
        return;
      }
      var body = {};
      if (field === 'hot') body.hot_water = bv;
      else if (field === 'cold') body.cold_water = bv;
      else { td.innerHTML = oldHtml; return; }
      td.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
      try {
        await api.post('/admin/gsheets/rows/' + tr.dataset.id + '/edit-values', body);
        toast('Значение исправлено', 'success');
        this.softReload({ rt: 'gsheets', id: tr.dataset.id });
      } catch (e) {
        toast('Ошибка: ' + cleanError(e), 'error');
        this.softReload(null);
      }
      return;
    }

    // Боевое показание — пересчёт через save_manual_entry (вода парой).
    var uid = Number(tr.dataset.uid);
    var pid = Number(tr.dataset.pid);
    var cur = { hot: tr.dataset.hot, cold: tr.dataset.cold, elect: tr.dataset.elect };
    cur[field] = valueStr;
    var hot = num(cur.hot), cold = num(cur.cold), elect = num(cur.elect);
    var payload = { user_id: uid, period_id: pid };
    if (hot != null && cold != null) { payload.hot_water = hot; payload.cold_water = cold; }
    else if (field === 'hot' || field === 'cold') {
      toast('Вода (ГВС+ХВС) подаётся парой — заполните оба', 'warning');
      td.innerHTML = oldHtml; return;
    }
    if (elect != null) payload.electricity = elect;
    if (payload.hot_water == null && payload.electricity == null) { td.innerHTML = oldHtml; return; }
    td.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
    try {
      var res = await api.post('/admin/readings/manual', payload);
      // Закрытый месяц save_manual_entry утверждает сразу — говорим честно
      // (хинт из ручного ввода: раньше это заставало админа врасплох).
      toast(res && res.auto_approved
        ? 'Записано и сразу утверждено (закрытый месяц)'
        : 'Показание обновлено и пересчитано', 'success');
      this.softReload({ rt: 'reading', id: tr.dataset.id });
    } catch (e) {
      toast('Ошибка: ' + (e.message || e), 'error');
      this.softReload(null);
    }
  },

  async _toggleHistory(uidStr, el) {
    var uid = Number(uidStr);
    if (!uid) return;
    var tr = el.closest('tr');
    var next = tr.nextElementSibling;
    if (next && next.classList.contains('reg-hist-row') && next.dataset.uid === String(uid)) {
      next.remove(); return;
    }
    this.dom.body.querySelectorAll('.reg-hist-row').forEach(function (n) { n.remove(); });
    var detail = document.createElement('tr');
    detail.className = 'reg-hist-row';
    detail.dataset.uid = String(uid);
    detail.innerHTML = '<td colspan="' + COLS + '" style="background:#fafafa; padding:10px 16px;"><i class="fa-solid fa-spinner fa-spin"></i> Загрузка истории…</td>';
    tr.after(detail);
    try {
      var data = await api.get('/admin/residents/' + uid + '/finance-detail?history_periods=6');
      detail.querySelector('td').innerHTML = this._histHtml(data);
      var self = this;
      detail.querySelectorAll('[data-hist-approve]').forEach(function (btn) {
        btn.addEventListener('click', function () { self.approve('reading', btn.dataset.histApprove, btn); });
      });
    } catch (e) {
      detail.querySelector('td').innerHTML = '<span style="color:var(--danger-color);">Ошибка: ' + esc(e.message || e) + '</span>';
    }
  },

  _histHtml(data) {
    var hist = (data && data.history) || [];
    if (!hist.length) return '<span style="color:var(--text-secondary);">Нет истории показаний.</span>';
    var rows = hist.map(function (h) {
      var st = h.reading_id
        ? (h.is_approved ? '<span style="color:#15803d;">утв.</span>' : '<span style="color:#92400e;">черновик</span>')
        : '<span style="color:var(--text-tertiary);">нет</span>';
      var approveBtn = (h.reading_id && !h.is_approved)
        ? '<button class="action-btn success-btn" style="padding:2px 7px; font-size:11px;" data-hist-approve="' + h.reading_id + '"><i class="fa-solid fa-check"></i> Утвердить</button>'
        : '';
      var sum = (h.total_cost != null) ? Number(h.total_cost).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' ₽' : '—';
      return '<tr style="border-bottom:1px solid #eef2f7;">' +
        '<td style="padding:4px 8px;">' + esc(h.period_name) + '</td>' +
        '<td style="padding:4px 8px; text-align:right; font-family:monospace;">' + fmtNum(h.hot_water) + '</td>' +
        '<td style="padding:4px 8px; text-align:right; font-family:monospace;">' + fmtNum(h.cold_water) + '</td>' +
        '<td style="padding:4px 8px; text-align:right; font-family:monospace;">' + fmtNum(h.electricity) + '</td>' +
        '<td style="padding:4px 8px; text-align:right;">' + sum + '</td>' +
        '<td style="padding:4px 8px;">' + st + '</td>' +
        '<td style="padding:4px 8px; text-align:right;">' + approveBtn + '</td>' +
        '</tr>';
    }).join('');
    return '<div style="font-size:12px;"><b><i class="fa-solid fa-clock-rotate-left"></i> Показания за последние периоды:</b>' +
      '<table style="width:100%; margin-top:6px; border-collapse:collapse; font-size:12px;">' +
      '<thead><tr style="color:var(--text-secondary); font-size:11px; text-align:left;">' +
      '<th style="padding:3px 8px;">Период</th><th style="padding:3px 8px; text-align:right;">ГВС</th>' +
      '<th style="padding:3px 8px; text-align:right;">ХВС</th><th style="padding:3px 8px; text-align:right;">Свет</th>' +
      '<th style="padding:3px 8px; text-align:right;">Итог</th><th style="padding:3px 8px;">Статус</th><th></th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table></div>';
  },

  async reassign(id) {
    // Делегируем проверенной модалке буфера (она сама ищет строку в своём
    // state, грузит кандидатов, шлёт /reassign и помнит алиас).
    if (!GSheetsModule || typeof GSheetsModule.reassignPrompt !== 'function') {
      toast('Переназначение — в детальном блоке ниже', 'warning');
      return;
    }
    try { await GSheetsModule.reassignPrompt(Number(id)); }
    catch (e) { /* модалка показывает свою ошибку */ }
    this.softReload({ rt: 'gsheets', id: Number(id) });   // тихо, не теряя место
  },

  // Отклонение: жилец получает уведомление на QR-портале и подаёт заново.
  //  - боевой черновик → /admin/registry/readings/{id}/reject (запись удаляется);
  //  - строка буфера   → /admin/gsheets/rows/{id}/reject (статус rejected).
  async reject(rowType, id, btn) {
    var isReading = (rowType === 'reading');
    var reason = null;
    if (isReading) {
      reason = await showPrompt('Отклонить показание', 'Причина (увидит жилец в уведомлении; можно оставить пустым):', '', 'например: цифры не совпадают со счётчиком');
      if (reason === null) return;   // отмена
    } else {
      if (!await showConfirm('Отклонить эту строку импорта? Жилец получит уведомление на QR-портале.', { title: 'Отклонить', confirmText: 'Отклонить' })) return;
    }
    btn.disabled = true;
    try {
      if (isReading) {
        await api.post('/admin/registry/readings/' + id + '/reject', { reason: (reason || '').trim() || null });
      } else {
        await api.post('/admin/gsheets/rows/' + id + '/reject', {});
      }
      toast('Отклонено — жилец увидит уведомление', 'success');
      this.softReload(null);
    } catch (e) {
      btn.disabled = false;
      toast('Не удалось отклонить. ' + cleanError(e), 'error');
    }
  },

  async approve(rowType, id, btn) {
    if (!await showConfirm('Утвердить эту запись? Будет создана/зафиксирована квитанция.', { title: 'Утверждение', confirmText: 'Утвердить' })) return;
    btn.disabled = true;
    try {
      if (rowType === 'reading') {
        // approve as-is (без корректировок). Детальная правка — в блоке ниже.
        await api.post('/admin/approve/' + id, {});
      } else {
        await api.post('/admin/gsheets/rows/' + id + '/approve', {});
      }
      toast('Утверждено', 'success');
      this.softReload({ rt: rowType, id: Number(id) });
    } catch (e) {
      // Конфликт: за период у жильца уже есть начисление (норматив/наём/авто/
      // прошлая подача). Показываем его и предлагаем ЗАМЕНИТЬ этой подачей.
      if (rowType === 'gsheets' && e.status === 409 && e.data && e.data.conflict) {
        const c = e.data.conflict;
        const ex = c.existing || {};
        const inc = c.incoming || {};
        const sum = ex.total_cost != null ? Number(ex.total_cost).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' ₽' : '—';
        const ok = await showConfirm(
          `За «${c.period_name}» у жильца УЖЕ есть начисление (${ex.kind || 'показание'}) на ${sum}:\n` +
          `  ГВС ${fmtNum(ex.hot_water)} · ХВС ${fmtNum(ex.cold_water)} · свет ${fmtNum(ex.electricity)}\n\n` +
          `Эта подача из таблицы: ГВС ${fmtNum(inc.hot_water)} · ХВС ${fmtNum(inc.cold_water)}.\n\n` +
          `Заменить существующее начисление этой подачей?\n` +
          `«Отмена» — оставить как есть (лишнюю подачу можно отклонить ✗).`,
          { title: 'За период уже есть начисление', confirmText: 'Заменить' }
        );
        if (ok) {
          try {
            await api.post('/admin/gsheets/rows/' + id + '/approve', { replace: true });
            toast('Заменено и утверждено', 'success');
            this.softReload({ rt: rowType, id: Number(id) });
            return;
          } catch (e2) {
            btn.disabled = false;
            toast('Не удалось заменить. ' + cleanError(e2), 'error');
            return;
          }
        }
        btn.disabled = false;
        return;
      }
      btn.disabled = false;
      toast('Не удалось утвердить. ' + cleanError(e), 'error');
    }
  },
};
