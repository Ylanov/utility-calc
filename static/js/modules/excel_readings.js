// static/js/modules/excel_readings.js
// Импорт показаний из Excel (2026-06-15). Кнопка «Загрузить из Excel» в реестре
// → модалка: выбор месяца (+создать), загрузка файла, повердиктная таблица по
// каждому жильцу (матч ФИО + все анализаторы + предв. сумма), поштучный разбор,
// «Утвердить» → создаются утверждённые показания сразу в финотчётность.
//
// Бэкенд: POST /api/admin/readings/excel/{preview,commit,ensure-period}.

import { api } from '../core/api.js';
import { toast, showConfirm, showPrompt, setLoading } from '../core/dom.js';

function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function banner(cls, html) {
  const col = cls === 'b-err' ? '#991b1b' : '#166534';
  const bg = cls === 'b-err' ? '#fee2e2' : '#dcfce7';
  return '<div style="background:' + bg + '; color:' + col + '; padding:8px 10px; border-radius:8px; font-size:13px; margin-top:6px;">' + html + '</div>';
}

const VERDICT = {
  ok:        { t: '✅ ОК',          c: '#166534', b: '#dcfce7' },
  warning:   { t: '⚠️ Проверить',   c: '#92400e', b: '#fef3c7' },
  error:     { t: '❌ Ошибка',      c: '#991b1b', b: '#fee2e2' },
  unmatched: { t: '🔍 Не найден',   c: '#991b1b', b: '#fee2e2' },
};
const RES = ['hot', 'cold', 'elect'];
const RES_LABEL = { hot: 'ГВС', cold: 'ХВС', elect: 'Свет' };

function badge(meta) {
  return '<span style="display:inline-block; padding:2px 8px; border-radius:12px; font-size:11px; font-weight:600; background:' +
    meta.b + '; color:' + meta.c + '; white-space:nowrap;">' + meta.t + '</span>';
}
function fmtNum(v) {
  if (v === null || v === undefined || v === '') return '—';
  var n = Number(v);
  return isNaN(n) ? esc(v) : n.toLocaleString('ru-RU', { maximumFractionDigits: 3 });
}
function fmtMoney(v) {
  if (v === null || v === undefined) return '—';
  return Number(v).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' ₽';
}

export const ExcelReadingsModule = {
  isInitialized: false,
  state: { periods: [], periodId: null, file: null, preview: null, filter: 'all' },

  init() {
    if (this.isInitialized) return;
    this.isInitialized = true;
    document.getElementById('btnExcelReadings')?.addEventListener('click', () => this.open());
    // Старая битая кнопка реестра (POST /admin/readings/import → 404) —
    // перенаправляем на новую рабочую модалку.
    document.getElementById('btnImportReadings')?.addEventListener('click', (e) => {
      e.preventDefault(); e.stopPropagation(); this.open();
    });
  },

  async open() {
    this.state.file = null; this.state.preview = null; this.state.filter = 'all';
    this._removed = [];
    this.renderModal();
    await this.loadPeriods();
    // Есть сохранённый черновик? Предложить продолжить.
    try {
      const d = await api.get('/admin/readings/excel/draft');
      if (d && !d.empty && (d.rows || []).length) {
        const when = d.saved_at ? new Date(d.saved_at).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' }) : '';
        if (await showConfirm('Найден сохранённый черновик' + (when ? ' от ' + when : '') + ' (' + d.rows.length + ' строк). Продолжить с того места?', { title: 'Черновик импорта', confirmText: 'Продолжить', cancelText: 'Начать заново' })) {
          await this.resumeDraft(d);
        }
      }
    } catch (e) { /* нет черновика — обычный старт */ }
  },

  close() { document.getElementById('excelReadingsModal')?.remove(); },

  // ── Период ──────────────────────────────────────────────────
  async loadPeriods() {
    try {
      const list = await api.get('/admin/periods/history');
      this.state.periods = list || [];
      const active = this.state.periods.find((p) => p.is_active);
      this.state.periodId = active ? active.id : (this.state.periods[0]?.id || null);
    } catch (e) { this.state.periods = []; }
    this.renderPeriodSelect();
  },

  renderPeriodSelect() {
    const sel = document.getElementById('xlPeriod');
    if (!sel) return;
    sel.innerHTML = this.state.periods.map((p) =>
      '<option value="' + p.id + '"' + (p.id === this.state.periodId ? ' selected' : '') + '>' +
      esc(p.name) + (p.is_active ? ' (активный)' : '') + '</option>').join('') ||
      '<option value="">— нет периодов —</option>';
  },

  async createPeriod() {
    const name = await showPrompt('Создать период', 'Месяц квитанций в формате «Месяц ГГГГ»:', 'Май 2026', 'Например: Май 2026');
    if (!name) return;
    try {
      const r = await api.post('/admin/readings/excel/ensure-period', { name: name.trim() });
      if (!this.state.periods.some((p) => p.id === r.id)) {
        this.state.periods.unshift({ id: r.id, name: r.name, is_active: r.is_active });
      }
      this.state.periodId = r.id;
      this.renderPeriodSelect();
      toast(r.created ? ('Период «' + r.name + '» создан') : ('Период «' + r.name + '» уже есть — выбран'), 'success');
    } catch (e) { toast('Не удалось: ' + (e.message || e), 'error'); }
  },

  // ── Загрузка + превью ───────────────────────────────────────
  async runPreview() {
    const fileInput = document.getElementById('xlFile');
    const file = fileInput?.files?.[0];
    if (!file) { toast('Сначала выберите файл Excel', 'info'); return; }
    const sel = document.getElementById('xlPeriod');
    this.state.periodId = sel?.value ? Number(sel.value) : null;
    if (!this.state.periodId) { toast('Выберите или создайте месяц квитанций', 'warning'); return; }

    const btn = document.getElementById('xlPreviewBtn');
    setLoading(btn, true, 'Анализ…');
    const fd = new FormData();
    fd.append('file', file);
    fd.append('period_id', String(this.state.periodId));
    try {
      const res = await api.post('/admin/readings/excel/preview', fd);
      this.state.preview = res;
      // Каждой строке — флаг «утверждать» (по умолчанию всё кроме unmatched/error).
      res.items.forEach((it) => { it._approve = (it.verdict === 'ok' || it.verdict === 'warning'); });
      this.renderPreview();
    } catch (e) {
      toast('Ошибка разбора: ' + (e.message || e), 'error');
    } finally { setLoading(btn, false, 'Разобрать'); }
  },

  filteredItems() {
    const f = this.state.filter;
    const items = this.state.preview?.items || [];
    if (f === 'all') return items;
    if (f === 'approve') return items.filter((x) => x._approve);
    return items.filter((x) => x.verdict === f);
  },

  renderPreview() {
    const p = this.state.preview;
    const body = document.getElementById('xlBody');
    const summary = document.getElementById('xlSummary');
    if (!p || !body) return;
    const c = p.counts || {};
    const approveCnt = p.items.filter((x) => x._approve).length;

    if (summary) {
      const chip = (label, n, col) => '<span style="display:inline-block; padding:4px 10px; border-radius:8px; background:' + col +
        '; font-size:12px; font-weight:600; margin-right:6px;">' + label + ': ' + (n || 0) + '</span>';
      summary.innerHTML =
        '<div style="margin-bottom:10px;">' +
        chip('Всего', p.total_people, '#eef2ff') +
        chip('✅ ОК', c.ok, '#dcfce7') +
        chip('⚠️ Проверить', c.warning, '#fef3c7') +
        chip('❌ Ошибка', c.error, '#fee2e2') +
        chip('🔍 Не найдены', c.unmatched, '#fee2e2') +
        chip('📊 По нормативу', c.norm, '#f3e8ff') +
        '</div>' +
        this.gsheetsLine(p.gsheets) +
        '<div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-bottom:8px;">' +
        '<span style="font-size:12px; color:var(--text-secondary);">Фильтр:</span>' +
        ['all|Все', 'approve|К утверждению', 'warning|Проверить', 'error|Ошибки', 'unmatched|Не найдены', 'ok|ОК']
          .map((x) => { const [v, t] = x.split('|'); return '<button class="action-btn ' + (this.state.filter === v ? 'primary-btn' : 'secondary-btn') +
            '" style="padding:4px 10px; font-size:12px;" data-xl-filter="' + v + '">' + t + '</button>'; }).join('') +
        '<span style="margin-left:auto; font-size:13px; font-weight:600;">К утверждению: ' + approveCnt + '</span>' +
        '</div>';
      summary.querySelectorAll('[data-xl-filter]').forEach((b) =>
        b.addEventListener('click', () => { this.state.filter = b.dataset.xlFilter; this.renderPreview(); }));
    }

    const meters = (p.meters_present && p.meters_present.length) ? p.meters_present : ['hot', 'cold'];
    // Динамические заголовки колонок показаний под реальные листы (вкл. электричество).
    const headRow = document.getElementById('xlHeadRow');
    if (headRow) headRow.innerHTML = this.headRowHtml(meters);
    const colspan = 7 + meters.length;   // чекбокс+ФИО+объект+тариф+счётчики+GSheets+вердикт+действия
    const rows = this.filteredItems().map((it) => this.rowHtml(it, p.items.indexOf(it), meters)).join('');
    body.innerHTML = rows ||
      '<tr><td colspan="' + colspan + '" style="padding:24px; text-align:center; color:var(--text-secondary);">Нет строк по фильтру.</td></tr>';

    body.querySelectorAll('[data-xl-toggle]').forEach((cb) =>
      cb.addEventListener('change', () => {
        const i = Number(cb.dataset.xlToggle); p.items[i]._approve = cb.checked; this.renderPreview();
      }));
    body.querySelectorAll('[data-xl-reassign]').forEach((b) =>
      b.addEventListener('click', () => this.reassign(Number(b.dataset.xlReassign))));
    body.querySelectorAll('[data-xl-add]').forEach((b) =>
      b.addEventListener('click', () => this.createOrBind(Number(b.dataset.xlAdd), b.dataset.mode)));
    body.querySelectorAll('[data-xl-del]').forEach((b) =>
      b.addEventListener('click', () => this.deleteRow(Number(b.dataset.xlDel))));
    body.querySelectorAll('[data-xl-editfio]').forEach((b) =>
      b.addEventListener('click', () => this.editFio(Number(b.dataset.xlEditfio))));
    // Живой ввод показаний — обновляем модель и расход БЕЗ перерисовки (фокус
    // не теряется); сумма пересчитается по кнопке «Пересчитать» / при утверждении.
    body.querySelectorAll('[data-xl-val]').forEach((inp) =>
      inp.addEventListener('input', () => {
        const i = Number(inp.dataset.xlVal), res = inp.dataset.res, field = inp.dataset.field;
        const raw = inp.value.trim().replace(',', '.');
        const it = p.items[i];
        if (!it[res]) it[res] = {};
        it[res][field] = raw === '' ? null : (isNaN(Number(raw)) ? raw : Number(raw));
        it._dirty = true;
        const d = it[res];
        const consEl = body.querySelector('[data-xl-cons="' + i + '-' + res + '"]');
        if (consEl && d.cur != null && d.prev != null && !isNaN(Number(d.cur)) && !isNaN(Number(d.prev))) {
          const dec = Number(d.cur) < Number(d.prev);
          consEl.style.color = dec ? '#dc2626' : '#15803d';
          consEl.textContent = dec ? '↓ счётчик упал' : '+' + fmtNum(Math.max(0, Number(d.cur) - Number(d.prev)));
        } else if (consEl) { consEl.textContent = ''; }
      }));

    const commitBtn = document.getElementById('xlCommitBtn');
    if (commitBtn) commitBtn.disabled = approveCnt === 0;
    const recalcBtn = document.getElementById('xlRecalcBtn');
    if (recalcBtn) recalcBtn.disabled = !p.items.length;
    const printBtn = document.getElementById('xlPrintBtn');
    if (printBtn) printBtn.disabled = !p.items.length;
    const saveBtn = document.getElementById('xlSaveBtn');
    if (saveBtn) saveBtn.disabled = !p.items.length;
  },

  // ── Черновик: сохранить / продолжить ───────────────────────
  draftRows() {
    const p = this.state.preview;
    if (!p) return [];
    return p.items.map((it) => ({
      fio: it.fio,
      user_id: (it.matched && it.matched.user_id) || null,
      status: it.status === 'norm' ? 'norm' : 'submitted',
      approve: !!it._approve,
      hot: it.hot || null, cold: it.cold || null, elect: it.elect || null,
    }));
  },

  async saveDraft() {
    const p = this.state.preview;
    if (!p || !p.items.length) { toast('Нечего сохранять', 'info'); return; }
    const btn = document.getElementById('xlSaveBtn');
    setLoading(btn, true, 'Сохраняю…');
    try {
      await api.post('/admin/readings/excel/draft', {
        period_id: this.state.periodId, rows: this.draftRows(),
      });
      toast('Черновик сохранён — можно закрыть и продолжить позже', 'success');
    } catch (e) {
      toast('Не удалось сохранить: ' + (e.message || e), 'error');
    } finally { setLoading(btn, false, 'Сохранить черновик'); }
  },

  // Продолжить из черновика: пере-матч + пересчёт (подхватит созданные
  // за это время квартиры/жильцов), восстановить отметки «утверждать».
  async resumeDraft(draft) {
    this.state.periodId = draft.period_id || this.state.periodId;
    this.renderPeriodSelect();
    const approveByIdx = (draft.rows || []).map((r) => r.approve !== false);
    const rows = (draft.rows || []).map((r) => ({
      fio: r.fio, user_id: r.user_id || null,
      hot: r.hot || null, cold: r.cold || null, elect: r.elect || null,
    }));
    if (!rows.length) return;
    const body = document.getElementById('xlBody');
    if (body) body.innerHTML = '<tr><td colspan="9" style="padding:24px; text-align:center;"><i class="fa-solid fa-spinner fa-spin"></i> Загрузка черновика…</td></tr>';
    try {
      const res = await api.post('/admin/readings/excel/recompute', { period_id: this.state.periodId, rows });
      res.items.forEach((it, i) => { it._approve = approveByIdx[i] !== undefined ? approveByIdx[i] : (it.verdict === 'ok' || it.verdict === 'warning'); });
      this.state.preview = res;
      this.renderPreview();
      toast('Черновик загружен' + (draft.saved_at ? ' (от ' + new Date(draft.saved_at).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' }) + ')' : ''), 'success');
    } catch (e) {
      toast('Не удалось загрузить черновик: ' + (e.message || e), 'error');
    }
  },

  rowHtml(it, idx, meters) {
    const m = it.matched;
    const place = m ? ((m.dormitory ? esc(m.dormitory) + ' / ' : '') + esc(m.room || '—')) : '—';
    // ФИО редактируемое (карандаш). Под ним — найденный жилец.
    let who = '<div style="display:flex; align-items:flex-start; gap:6px;">' +
      '<b style="word-break:break-word;">' + esc(it.fio) + '</b>' +
      '<button class="action-btn secondary-btn" style="padding:1px 6px; font-size:10px; flex-shrink:0;" data-xl-editfio="' + idx + '" title="Исправить ФИО"><i class="fa-solid fa-pen"></i></button></div>';
    if (m) {
      who += '<span style="font-size:11px; color:var(--primary-color);">→ ' + esc(m.username) +
        ' · ' + (m.score || 0) + '%' + (m.conflict ? ' ⚠️' : '') + '</span>';
    }
    // Редактируемые показания: прев → тек, два инпута. Расход считается живо.
    const meterCells = meters.map((r) => {
      const d = it[r] || {};
      const prev = (d.prev != null ? d.prev : '');
      const cur = (d.cur != null ? d.cur : '');
      const cons = (d.cur != null && d.prev != null) ? Math.max(0, Number(d.cur) - Number(d.prev)) : null;
      const decd = (d.cur != null && d.prev != null && Number(d.cur) < Number(d.prev));
      const inp = (field, val) => '<input data-xl-val="' + idx + '" data-res="' + r + '" data-field="' + field + '" value="' + esc(val) +
        '" inputmode="decimal" style="width:62px; text-align:right; font-family:monospace; font-size:12px; padding:2px 4px; border:1px solid #cbd5e1; border-radius:5px;">';
      return '<td style="text-align:right; white-space:nowrap;">' +
        inp('prev', prev) + ' <span style="color:#94a3b8;">→</span> ' + inp('cur', cur) +
        '<br><span data-xl-cons="' + idx + '-' + r + '" style="font-size:11px; color:' + (decd ? '#dc2626' : '#15803d') + ';">' +
        (cons != null ? (decd ? '↓ счётчик упал' : '+' + fmtNum(cons)) : '') + '</span></td>';
    }).join('');

    // Ячейка сверки с Google Sheets — с понятными подписями и датой подачи.
    const g = it.gsheets || {};
    let gsCell;
    if (!g.present) {
      gsCell = '<span style="font-size:12px; color:var(--text-secondary);">в гугл таблицах нет</span>';
    } else {
      const line = (label, val, mis) => '<div style="font-size:12px;">' + label + ': ' +
        (mis ? '<b style="color:#dc2626;">' + fmtNum(val) + '</b>' : '<span style="font-family:monospace;">' + fmtNum(val) + '</span>') + '</div>';
      gsCell = (g.mismatch ? '<div style="color:#dc2626; font-size:11px; font-weight:600;">⚠️ расходится с Excel</div>' : '') +
        line('ГВС', g.hot, g.mismatch_hot) + line('ХВС', g.cold, g.mismatch_cold) +
        '<div style="font-size:11px; color:var(--text-secondary); margin-top:2px;">подано ' + esc(g.date || '—') +
        (g.count > 1 ? ' · ' + g.count + ' подач' : '') + '</div>';
    }

    const reasons = (it.reasons || []).length
      ? '<div style="font-size:11px; color:var(--text-secondary); margin-top:3px;">' +
        it.reasons.map(esc).join('<br>') + '</div>' : '';
    const canApprove = it.verdict !== 'unmatched' && it.verdict !== 'error' && m && m.user_id;
    const checkbox = canApprove
      ? '<input type="checkbox" data-xl-toggle="' + idx + '"' + (it._approve ? ' checked' : '') + '>'
      : '';
    const reassign = (it.verdict === 'unmatched' || (m && m.conflict))
      ? '<button class="action-btn secondary-btn" style="padding:3px 8px; font-size:11px;" data-xl-reassign="' + idx + '" title="Найти существующего жильца"><i class="fa-solid fa-user-pen"></i></button>'
      : '';
    // Кнопка создать/привязать: нет привязки к комнате (не найден ИЛИ
    // найден без помещения). У нас QR-коды → жилец = ФИО + квартира,
    // без логина/пароля. Найден-без-комнаты → привязка; не найден → создание.
    const needRoom = (m && m.user_id && !m.room) ? 'bind' : ((!m || !m.user_id) ? 'create' : null);
    const addBtn = needRoom
      ? '<button class="action-btn primary-btn" style="padding:3px 8px; font-size:11px;" data-xl-add="' + idx + '" data-mode="' + needRoom + '" title="' +
        (needRoom === 'bind' ? 'Привязать жильца к квартире' : 'Создать жильца и привязать к квартире') + '"><i class="fa-solid fa-house-user"></i></button>'
      : '';
    // Удалить строку (лишний / неправильный человек) — не попадёт в утверждение.
    const delBtn = '<button class="action-btn" style="padding:3px 8px; font-size:11px; background:#fee2e2; color:#991b1b; border:1px solid #fecaca;" data-xl-del="' + idx + '" title="Убрать из импорта"><i class="fa-solid fa-trash"></i></button>';

    return '<tr style="' + (it.verdict === 'error' || it.verdict === 'unmatched' ? 'background:rgba(239,68,68,0.04);' : (it.verdict === 'warning' ? 'background:rgba(245,158,11,0.05);' : '')) + '">' +
      '<td style="text-align:center;">' + checkbox + '</td>' +
      '<td>' + who + reasons + '</td>' +
      '<td style="font-size:12px;">' + place + '</td>' +
      '<td style="font-size:11px; color:var(--text-secondary);">' + (m ? esc(m.tariff || '—') : '—') + '</td>' +
      meterCells +
      '<td style="text-align:right;">' + gsCell + '</td>' +
      '<td>' + badge(VERDICT[it.verdict] || VERDICT.error) +
        (it.status === 'norm' ? '<br><span style="font-size:10px; color:#6b21a8;">норматив</span>' : '') +
        (it._dirty ? '<br><span style="font-size:10px; color:#92400e;">изменено</span>' : '') + '</td>' +
      '<td style="text-align:center; white-space:nowrap;"><div style="display:flex; gap:3px; justify-content:center; flex-wrap:wrap;">' + addBtn + reassign + delBtn + '</div></td>' +
      '</tr>';
  },

  // Удалить строку из импорта (лишний/неправильный человек).
  async deleteRow(idx) {
    const it = this.state.preview?.items[idx];
    if (!it) return;
    if (!await showConfirm('Убрать «' + esc(it.fio) + '» из импорта? Эта строка не попадёт в утверждение.', { title: 'Удалить строку', confirmText: 'Убрать' })) return;
    this._removed = this._removed || [];
    this._removed.push({ fio: it.fio, reason: 'удалён вручную' });
    this.state.preview.items.splice(idx, 1);
    this.renderPreview();
  },

  // Исправить ФИО (опечатки/неправильное написание) → авто-пересчёт (пере-матч).
  async editFio(idx) {
    const it = this.state.preview?.items[idx];
    if (!it) return;
    const v = await showPrompt('Исправить ФИО', 'Правильное ФИО (Фамилия Имя Отчество):', it.fio, 'Фамилия Имя Отчество');
    if (v === null) return;
    const fio = v.trim();
    if (!fio || fio === it.fio) return;
    it.fio = fio;
    it._dirty = true;
    // Сбрасываем ручную привязку — пусть пере-матчит по новому ФИО.
    it.matched = null;
    await this.recompute();
  },

  // Пересчёт по текущему (отредактированному) состоянию таблицы — без файла.
  async recompute() {
    const p = this.state.preview;
    if (!p || !p.items.length) return;
    const rows = p.items.map((it) => ({
      fio: it.fio,
      user_id: (it.matched && it.matched.user_id) || null,
      hot: it.hot || null, cold: it.cold || null, elect: it.elect || null,
    }));
    const approveByIdx = p.items.map((it) => !!it._approve);
    const btn = document.getElementById('xlRecalcBtn');
    setLoading(btn, true, 'Считаю…');
    try {
      const res = await api.post('/admin/readings/excel/recompute', { period_id: this.state.periodId, rows });
      // Сохраняем выбор «утверждать» по позиции (порядок строк стабилен).
      res.items.forEach((it, i) => { it._approve = (approveByIdx[i] !== undefined ? approveByIdx[i] : (it.verdict === 'ok' || it.verdict === 'warning')); it._dirty = false; });
      this.state.preview = res;
      this.renderPreview();
      toast('Пересчитано', 'success');
    } catch (e) {
      toast('Не удалось пересчитать: ' + (e.message || e), 'error');
    } finally { setLoading(btn, false, 'Пересчитать'); }
  },

  // Печать / сохранение в PDF: чистый отчёт в новом окне → window.print().
  printReport() {
    const p = this.state.preview;
    if (!p) return;
    const meters = (p.meters_present && p.meters_present.length) ? p.meters_present : ['hot', 'cold'];
    const periodName = (this.state.periods.find((x) => x.id === this.state.periodId) || {}).name || '';
    const head = '<th>ФИО / жилец</th><th>Объект</th><th>Тариф</th>' +
      meters.map((r) => '<th>' + RES_LABEL[r] + ' (пред→тек)</th>').join('') +
      '<th>Google Sheets</th><th>Вердикт</th>';
    const rowsHtml = p.items.map((it) => {
      const m = it.matched || {};
      const mc = meters.map((r) => {
        const d = it[r] || {};
        return '<td style="text-align:right;">' + fmtNum(d.prev) + ' → ' + fmtNum(d.cur) + '</td>';
      }).join('');
      const g = it.gsheets || {};
      const gsTxt = !g.present ? 'нет' :
        ('ГВС ' + fmtNum(g.hot) + ' / ХВС ' + fmtNum(g.cold) + (g.date ? ' (' + g.date + ')' : '') + (g.mismatch ? ' ⚠' : ''));
      const v = (VERDICT[it.verdict] || VERDICT.error).t;
      return '<tr' + (it._approve ? '' : ' style="color:#999;"') + '><td><b>' + esc(it.fio) + '</b>' +
        (m.username ? '<br><small>→ ' + esc(m.username) + '</small>' : '') + '</td>' +
        '<td>' + esc((m.dormitory ? m.dormitory + ' / ' : '') + (m.room || '—')) + '</td>' +
        '<td>' + esc(m.tariff || '—') + '</td>' + mc +
        '<td>' + esc(gsTxt) + '</td><td>' + esc(v) + (it.status === 'norm' ? ' (норматив)' : '') + '</td></tr>';
    }).join('');
    const removed = (this._removed || []);
    const removedHtml = removed.length
      ? '<h3>Убраны из импорта (' + removed.length + ')</h3><ul>' + removed.map((r) => '<li>' + esc(r.fio) + '</li>').join('') + '</ul>'
      : '';
    const c = p.counts || {};
    const win = window.open('', '_blank', 'width=1100,height=760');
    if (!win) { toast('Разрешите всплывающие окна для печати', 'warning'); return; }
    // Без inline-обработчиков (их блокирует CSP script-src 'self'). Печать
    // запускаем из РОДИТЕЛЬСКОГО контекста — это не inline-скрипт дочернего окна.
    win.document.write(
      '<html><head><meta charset="utf-8"><title>Импорт показаний — ' + esc(periodName) + '</title>' +
      '<style>body{font-family:sans-serif;padding:24px;color:#1e293b;}h2{margin:0 0 4px;}table{width:100%;border-collapse:collapse;font-size:12px;margin-top:12px;}' +
      'th,td{border:1px solid #cbd5e1;padding:5px 7px;text-align:left;}th{background:#f1f5f9;}</style></head><body>' +
      '<h2>Импорт показаний за «' + esc(periodName) + '»</h2>' +
      '<div style="font-size:13px;color:#475569;">Всего: ' + (p.total_people || p.items.length) + ' · ОК: ' + (c.ok || 0) +
      ' · Проверить: ' + (c.warning || 0) + ' · Ошибка: ' + (c.error || 0) + ' · Не найдены: ' + (c.unmatched || 0) +
      ' · По нормативу: ' + (c.norm || 0) + '</div>' +
      '<div style="font-size:12px;color:#94a3b8;margin-top:6px;">Если окно печати не открылось автоматически — нажмите Ctrl+P.</div>' +
      '<table><thead><tr>' + head + '</tr></thead><tbody>' + rowsHtml + '</tbody></table>' + removedHtml +
      '</body></html>');
    win.document.close();
    win.focus();
    setTimeout(() => { try { win.print(); } catch (e) { /* пользователь нажмёт Ctrl+P */ } }, 400);
  },

  // Ручное назначение жильца для не найденных / конфликтов (поиск по ФИО).
  async reassign(idx) {
    const it = this.state.preview?.items[idx];
    if (!it) return;
    const q = await showPrompt('Назначить жильца', 'Введите ФИО или часть для поиска:', it.fio, 'Фамилия Имя');
    if (!q) return;
    let found;
    try { found = await api.get('/admin/gsheets/search-users?q=' + encodeURIComponent(q.trim())); }
    catch (e) { toast('Поиск не удался: ' + (e.message || e), 'error'); return; }
    const list = (found && found.items) || [];
    if (!list.length) { toast('Никого не найдено', 'warning'); return; }
    // Простой выбор: если один — берём, иначе предлагаем список номерами.
    let chosen = list[0];
    if (list.length > 1) {
      const opts = list.slice(0, 9).map((u, i) => (i + 1) + ') ' + u.username +
        (u.room ? ' (' + u.room + ')' : '')).join('\n');
      const pick = await showPrompt('Несколько совпадений', 'Введите номер нужного жильца:\n' + opts, '1', '1');
      const n = parseInt(pick, 10);
      if (!n || n < 1 || n > list.length) return;
      chosen = list[n - 1];
    }
    it.matched = {
      user_id: chosen.id, username: chosen.username,
      room: chosen.room, dormitory: null,
      tariff: it.matched?.tariff || null, score: 100, conflict: false,
      residents: chosen.residents_count || 1,
    };
    // Пересчёт суммы под нового жильца — на бэке при commit; в превью помечаем.
    it.verdict = (it.status === 'norm') ? 'warning' : 'ok';
    it.reasons = (it.reasons || []).filter((r) => !/не найден|несколько похожих/i.test(r));
    it.reasons.push('Назначен вручную: ' + chosen.username);
    it._approve = true; it._dirty = true;
    this.renderPreview();
  },

  // ── Создать жильца / привязать к квартире ───────────────────
  // У нас QR-коды: жилец = ФИО + квартира, без логина/пароля. mode:
  //  'create' — нет в базе → создаём нового (POST /users) и привязываем;
  //  'bind'   — есть в базе без помещения → привязываем (POST /users/{id}/relocate).
  async createOrBind(idx, mode) {
    const it = this.state.preview?.items[idx];
    if (!it) return;
    // Справочники для пикера комнат (кешируем на модуль).
    if (!this._dorms) {
      try { this._dorms = await api.get('/rooms/dormitories'); } catch (e) { this._dorms = []; }
    }
    const isBind = mode === 'bind';
    const who = isBind ? (it.matched && it.matched.username) : it.fio;

    const ov = document.createElement('div');
    ov.id = 'xlAddModal';
    ov.style.cssText = 'position:fixed; inset:0; background:rgba(0,0,0,.45); display:flex; align-items:center; justify-content:center; z-index:10001; padding:20px;';
    ov.innerHTML =
      '<div class="modal-window" style="max-width:460px; width:100%;">' +
      '  <div class="modal-header"><h3><i class="fa-solid fa-house-user"></i> ' +
      (isBind ? 'Привязать к квартире' : 'Создать жильца') + '</h3>' +
      '    <button class="close-btn close-icon" data-cb-close>&times;</button></div>' +
      '  <div class="modal-body" style="display:flex; flex-direction:column; gap:12px;">' +
      '    <div><label style="font-size:12px; color:var(--text-secondary);">ФИО</label>' +
      '      <input type="text" id="cbFio" value="' + esc(who || '') + '"' + (isBind ? ' disabled' : '') + ' style="width:100%; box-sizing:border-box;"></div>' +
      '    <div><label style="font-size:12px; color:var(--text-secondary);">Тип помещения</label>' +
      '      <select id="cbPlace" style="width:100%;"><option value="dormitory">Общежитие</option><option value="house">Дом / квартира</option></select></div>' +
      '    <div id="cbDormWrap"><label style="font-size:12px; color:var(--text-secondary);">Общежитие</label>' +
      '      <select id="cbDorm" style="width:100%;"><option value="">— выберите —</option>' +
      (this._dorms || []).map((d) => '<option value="' + esc(d) + '">' + esc(d) + '</option>').join('') + '</select></div>' +
      '    <div><label style="font-size:12px; color:var(--text-secondary);">Квартира / комната</label>' +
      '      <select id="cbRoom" style="width:100%;" disabled><option value="">— сначала выберите общежитие —</option></select></div>' +
      (isBind ? '' :
        '    <div style="display:flex; gap:10px;">' +
        '      <div style="flex:1;"><label style="font-size:12px; color:var(--text-secondary);">Жильцов (платит за)</label>' +
        '        <input type="number" id="cbResidents" value="1" min="1" max="20" style="width:100%; box-sizing:border-box;"></div>' +
        '      <div style="flex:1;"><label style="font-size:12px; color:var(--text-secondary);">Тип жильца</label>' +
        '        <select id="cbType" style="width:100%;"><option value="family">Семья</option><option value="single">Холостяк</option></select></div>' +
        '    </div>') +
      '    <div id="cbMsg"></div>' +
      '  </div>' +
      '  <div class="modal-footer" style="display:flex; justify-content:flex-end; gap:8px;">' +
      '    <button class="action-btn secondary-btn" data-cb-close>Отмена</button>' +
      '    <button class="action-btn success-btn" id="cbSave">' + (isBind ? 'Привязать' : 'Создать и привязать') + '</button>' +
      '  </div></div>';
    document.body.appendChild(ov);
    const closeCb = () => ov.remove();
    ov.addEventListener('click', (e) => { if (e.target === ov || e.target.closest('[data-cb-close]')) closeCb(); });

    const placeSel = ov.querySelector('#cbPlace');
    const dormWrap = ov.querySelector('#cbDormWrap');
    const dormSel = ov.querySelector('#cbDorm');
    const roomSel = ov.querySelector('#cbRoom');

    const loadRooms = async (query, label) => {
      roomSel.innerHTML = '<option value="">Загрузка…</option>'; roomSel.disabled = true;
      try {
        const res = await api.get(query);
        const rooms = res.items || res || [];
        roomSel.innerHTML = '<option value="">— выберите —</option>' +
          rooms.map((r) => '<option value="' + r.id + '">' + esc(label(r)) + '</option>').join('');
        roomSel.disabled = false;
      } catch (e) { roomSel.innerHTML = '<option value="">Ошибка загрузки</option>'; }
    };
    placeSel.addEventListener('change', () => {
      if (placeSel.value === 'house') {
        dormWrap.style.display = 'none';
        loadRooms('/rooms?place_type=house&limit=1000',
          (r) => [r.street, r.house_number, r.apartment_number ? 'кв.' + r.apartment_number : ''].filter(Boolean).join(' '));
      } else {
        dormWrap.style.display = '';
        roomSel.innerHTML = '<option value="">— сначала выберите общежитие —</option>'; roomSel.disabled = true;
      }
    });
    dormSel.addEventListener('change', () => {
      if (!dormSel.value) { roomSel.innerHTML = '<option value="">— выберите общежитие —</option>'; roomSel.disabled = true; return; }
      loadRooms('/rooms?dormitory=' + encodeURIComponent(dormSel.value) + '&limit=1000', (r) => r.room_number);
    });

    ov.querySelector('#cbSave').addEventListener('click', async () => {
      const msg = ov.querySelector('#cbMsg');
      const roomId = roomSel.value ? Number(roomSel.value) : null;
      if (!roomId) { msg.innerHTML = banner('b-err', 'Выберите квартиру.'); return; }
      const roomLabel = roomSel.options[roomSel.selectedIndex]?.text || '';
      const dorm = (placeSel.value === 'dormitory') ? dormSel.value : null;
      const saveBtn = ov.querySelector('#cbSave');
      setLoading(saveBtn, true, '…');
      try {
        let userId, username;
        if (isBind) {
          userId = it.matched.user_id; username = it.matched.username;
          await api.post('/users/' + userId + '/relocate', { new_room_id: roomId, is_eviction: false });
        } else {
          const fio = ov.querySelector('#cbFio').value.trim();
          if (fio.length < 3) { msg.innerHTML = banner('b-err', 'Введите ФИО.'); setLoading(saveBtn, false, 'Создать и привязать'); return; }
          const res = await api.post('/users', {
            username: fio, role: 'user', room_id: roomId,
            residents_count: Number(ov.querySelector('#cbResidents').value) || 1,
            resident_type: ov.querySelector('#cbType').value || 'family',
          });
          userId = res.id; username = res.username || fio;
        }
        it.matched = {
          user_id: userId, username,
          room: roomLabel, dormitory: dorm,
          tariff: it.matched?.tariff || null, score: 100, conflict: false,
          residents: isBind ? (it.matched?.residents || 1) : (Number(ov.querySelector('#cbResidents').value) || 1),
        };
        it.verdict = (it.status === 'norm') ? 'warning' : 'ok';
        it.reasons = (it.reasons || []).filter((r) => !/не найден|без помещения|несколько похожих/i.test(r));
        it.reasons.push(isBind ? ('Привязан к квартире: ' + roomLabel) : ('Создан жилец: ' + username));
        it._approve = true; it._dirty = true;
        closeCb();
        toast(isBind ? 'Жилец привязан к квартире' : 'Жилец создан и привязан', 'success');
        this.renderPreview();
      } catch (e) {
        setLoading(saveBtn, false, isBind ? 'Привязать' : 'Создать и привязать');
        msg.innerHTML = banner('b-err', esc(e.message || e));
      }
    });
  },

  // ── Утверждение ─────────────────────────────────────────────
  async commit() {
    const p = this.state.preview;
    if (!p) return;
    const chosen = p.items.filter((x) => x._approve && x.matched && x.matched.user_id);
    if (!chosen.length) { toast('Нет отмеченных к утверждению', 'info'); return; }
    const periodName = (this.state.periods.find((x) => x.id === this.state.periodId) || {}).name || '';
    if (!await showConfirm(
      'Утвердить ' + chosen.length + ' показаний за «' + esc(periodName) + '»? Они сразу попадут в финансовую отчётность. Действие применяется массово.',
      { title: 'Утверждение', confirmText: 'Утвердить', danger: false })) return;

    const decisions = chosen.map((it) => ({
      user_id: it.matched.user_id,
      status: it.status === 'norm' ? 'norm' : 'submitted',
      hot: it.hot || null, cold: it.cold || null, elect: it.elect || null,
    }));
    const btn = document.getElementById('xlCommitBtn');
    setLoading(btn, true, 'Утверждаю…');
    try {
      const res = await api.post('/admin/readings/excel/commit', { period_id: this.state.periodId, decisions });
      let msg = 'Создано квитанций: ' + res.created;
      if (res.skipped_existing) msg += ' · пропущено (уже были): ' + res.skipped_existing;
      if (res.failed) msg += ' · ошибок: ' + res.failed;
      toast(msg, res.failed ? 'warning' : 'success');
      // Утвердили — черновик больше не нужен, чистим.
      try { await api.delete('/admin/readings/excel/draft'); } catch (e) { /* не критично */ }
      this.close();
      // Обновить реестр, если открыт.
      try {
        const mod = (await import('./registry.js')).RegistryModule;
        if (mod && typeof mod.refresh === 'function') mod.refresh();
      } catch (e) { /* ничего */ }
    } catch (e) {
      toast('Не удалось утвердить: ' + (e.message || e), 'error');
    } finally { setLoading(btn, false, 'Утвердить отмеченные'); }
  },

  // ── Разметка модалки ────────────────────────────────────────
  renderModal() {
    this.close();
    const ov = document.createElement('div');
    ov.id = 'excelReadingsModal';
    ov.className = 'modal-overlay open';
    ov.style.cssText = 'position:fixed; inset:0; background:rgba(0,0,0,.5); display:flex; align-items:center; justify-content:center; z-index:9999; padding:28px;';
    ov.innerHTML =
      // width:100% от padded-оверлея → ровные отступы 28px по бокам (не упирается в края).
      '<div class="modal-window" style="max-width:1500px; width:100%; max-height:94vh; display:flex; flex-direction:column;">' +
      '  <div class="modal-header"><h3><i class="fa-solid fa-file-excel" style="color:#16a34a;"></i> Импорт показаний из Excel</h3>' +
      '    <button class="close-btn close-icon" data-xl-close>&times;</button></div>' +
      '  <div class="modal-body" style="overflow:auto; flex:1;">' +
      '    <div style="display:flex; gap:12px; flex-wrap:wrap; align-items:flex-end; background:var(--primary-bg); padding:12px 14px; border-radius:8px; margin-bottom:14px;">' +
      '      <div><label style="font-size:12px; color:var(--text-secondary); display:block; margin-bottom:4px;">Месяц квитанций (текущий)</label>' +
      '        <div style="display:flex; gap:6px;"><select id="xlPeriod" style="min-width:170px;"></select>' +
      '        <button class="action-btn secondary-btn" id="xlNewPeriod" style="white-space:nowrap;"><i class="fa-solid fa-plus"></i> Создать</button></div></div>' +
      '      <div><label style="font-size:12px; color:var(--text-secondary); display:block; margin-bottom:4px;">Файл Excel (.xlsx)</label>' +
      '        <input type="file" id="xlFile" accept=".xlsx,.xls"></div>' +
      '      <button class="action-btn primary-btn" id="xlPreviewBtn"><i class="fa-solid fa-magnifying-glass-chart"></i> Разобрать</button>' +
      '    </div>' +
      '    <div style="font-size:12px; color:var(--text-secondary); margin-bottom:12px;">Формат: листы «горячая» / «холодная» / «электричество», колонки <b>ФИО | Предыдущий месяц | Текущий месяц</b>. Предыдущий — база (апрель), текущий — расчётный (май). Не подавшим начислится норматив.</div>' +
      '    <div id="xlSummary"></div>' +
      '    <div class="table-responsive" style="max-height:56vh;">' +
      '      <table class="sticky-header-table" style="min-width:1240px;">' +
      '        <thead><tr id="xlHeadRow">' + this.headRowHtml(['hot', 'cold']) + '</tr></thead>' +
      '        <tbody id="xlBody"><tr><td colspan="9" style="padding:30px; text-align:center; color:var(--text-secondary);">Выберите месяц и файл, затем «Разобрать».</td></tr></tbody>' +
      '      </table>' +
      '    </div>' +
      '  </div>' +
      '  <div class="modal-footer" style="display:flex; justify-content:space-between; gap:10px; flex-wrap:wrap;">' +
      '    <button class="action-btn secondary-btn" data-xl-close>Закрыть</button>' +
      '    <div style="display:flex; gap:8px; flex-wrap:wrap;">' +
      '      <button class="action-btn secondary-btn" id="xlPrintBtn" disabled><i class="fa-solid fa-print"></i> Печать / PDF</button>' +
      '      <button class="action-btn secondary-btn" id="xlSaveBtn" disabled><i class="fa-solid fa-floppy-disk"></i> Сохранить черновик</button>' +
      '      <button class="action-btn secondary-btn" id="xlRecalcBtn" disabled><i class="fa-solid fa-rotate"></i> Пересчитать</button>' +
      '      <button class="action-btn success-btn" id="xlCommitBtn" disabled><i class="fa-solid fa-check-double"></i> Утвердить отмеченные</button>' +
      '    </div>' +
      '  </div>' +
      '</div>';
    document.body.appendChild(ov);
    ov.addEventListener('click', (e) => { if (e.target === ov || e.target.closest('[data-xl-close]')) this.close(); });
    document.getElementById('xlNewPeriod').addEventListener('click', () => this.createPeriod());
    document.getElementById('xlPreviewBtn').addEventListener('click', () => this.runPreview());
    document.getElementById('xlCommitBtn').addEventListener('click', () => this.commit());
    document.getElementById('xlRecalcBtn').addEventListener('click', () => this.recompute());
    document.getElementById('xlPrintBtn').addEventListener('click', () => this.printReport());
    document.getElementById('xlSaveBtn').addEventListener('click', () => this.saveDraft());
  },

  gsheetsLine(gs) {
    if (!gs || !gs.checked) {
      return '<div style="font-size:12px; color:var(--text-secondary); margin-bottom:8px;">📄 Сверка с Google Sheets: выберите месяц с распознаваемым именем («Май 2026»).</div>';
    }
    const w = gs.window;
    return '<div style="font-size:12px; margin-bottom:8px; padding:6px 10px; background:#f0fdf4; border-radius:6px;">' +
      '📄 Сверено с буфером Google Sheets за ' + (w ? esc(w.start) + '–' + esc(w.end) : 'окно') + ': ' +
      'найдено <b>' + (gs.present || 0) + '</b>' +
      (gs.mismatch ? ', из них <b style="color:#dc2626;">' + gs.mismatch + ' с расхождением ⚠️</b>' : '') +
      '. Остальным — «в гугл таблицах нет».</div>';
  },

  headRowHtml(meters) {
    return '<th style="width:34px;"></th><th>ФИО / жилец</th><th style="width:150px;">Объект</th><th style="width:120px;">Тариф</th>' +
      meters.map((r) => '<th style="text-align:right; width:120px;">' + RES_LABEL[r] + ' (пред→тек)</th>').join('') +
      '<th style="width:130px; text-align:right;" title="Сверка с буфером Google Sheets за окно вокруг месяца">Google Sheets<br><span style="font-weight:400; font-size:10px;">ГВС / ХВС</span></th>' +
      '<th style="width:130px;">Вердикт</th><th style="width:56px;"></th>';
  },
};
