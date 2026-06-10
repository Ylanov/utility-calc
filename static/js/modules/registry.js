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
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
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
const SRC = {
  user:    { t: '📱 QR-портал',      c: '#1e40af', b: '#dbeafe' },
  gsheets: { t: '📄 Google Sheets',  c: '#166534', b: '#dcfce7' },
  buffer:  { t: '📄 Sheets (буфер)', c: '#92400e', b: '#fef3c7' },
  auto:    { t: '🤖 Норматив/авто',  c: '#6b21a8', b: '#f3e8ff' },
  manual:  { t: '✍️ Вручную',        c: '#475569', b: '#f1f5f9' },
};

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
  state: { source: '', search: '', page: 1, limit: 50 },

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
    };
  },

  bind() {
    this.dom.src?.addEventListener('change', () => { this.state.source = this.dom.src.value; this.state.page = 1; this.load(); });
    this.dom.refresh?.addEventListener('click', () => this.load());
    let t = null;
    this.dom.search?.addEventListener('input', () => {
      clearTimeout(t);
      t = setTimeout(() => { this.state.search = this.dom.search.value.trim(); this.state.page = 1; this.load(); }, 350);
    });
  },

  async load() {
    if (!this.dom.body) return;
    this.dom.body.innerHTML = '<tr><td colspan="11" style="padding:30px; text-align:center; color:var(--text-secondary);"><i class="fa-solid fa-spinner fa-spin"></i> Загрузка…</td></tr>';
    var q = '/admin/registry?page=' + this.state.page + '&limit=' + this.state.limit +
      (this.state.source ? '&source=' + encodeURIComponent(this.state.source) : '') +
      (this.state.search ? '&search=' + encodeURIComponent(this.state.search) : '');
    let data;
    try { data = await api.get(q); }
    catch (e) { this.dom.body.innerHTML = '<tr><td colspan="11" style="padding:24px; text-align:center; color:var(--danger-color);">Ошибка: ' + esc(e.message || e) + '</td></tr>'; return; }
    this.render(data);
  },

  render(data) {
    var items = (data && data.items) || [];
    if (this.dom.total) this.dom.total.textContent = (data.period ? 'Период «' + data.period + '» · ' : '') + 'всего: ' + (data.total || 0);
    if (!items.length) {
      this.dom.body.innerHTML = '<tr><td colspan="11" style="padding:30px; text-align:center; color:var(--text-secondary);">Нет записей по фильтру.</td></tr>';
      return;
    }
    this.dom.body.innerHTML = items.map((r) => {
      var when = r.timestamp ? new Date(r.timestamp).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' }) : '—';

      // ФИО + под ним: общага серым; для буфера — итог сопоставления.
      var fioCell = '<b>' + esc(r.fio || '—') + '</b>';
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
      var canApprove = (r.status === 'draft' || r.status === 'pending' || r.status === 'auto_approved');
      var canReject = (r.row_type === 'gsheets')
        ? (r.status !== 'approved' && r.status !== 'rejected')
        : (r.status === 'draft');
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

      return '<tr style="' + rowTint(r) + '">' +
        '<td style="font-size:12px; color:var(--text-secondary); white-space:nowrap;">' + esc(when) + '</td>' +
        '<td>' + badge(SRC[r.source], r.source) + '</td>' +
        '<td>' + fioCell + '</td>' +
        '<td style="font-size:13px;">' + esc(r.room || '—') + '</td>' +
        '<td style="font-size:11px; color:var(--text-secondary);">' + esc(r.tariff || '—') + '</td>' +
        '<td style="text-align:right; font-family:monospace;">' + fmtNum(r.hot) + '</td>' +
        '<td style="text-align:right; font-family:monospace;">' + fmtNum(r.cold) + '</td>' +
        '<td style="text-align:right; font-family:monospace;">' + fmtNum(r.elect) + '</td>' +
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

  async reassign(id) {
    // Делегируем проверенной модалке буфера (она сама ищет строку в своём
    // state, грузит кандидатов, шлёт /reassign и помнит алиас).
    if (!GSheetsModule || typeof GSheetsModule.reassignPrompt !== 'function') {
      toast('Переназначение — в детальном блоке ниже', 'warning');
      return;
    }
    try { await GSheetsModule.reassignPrompt(Number(id)); }
    catch (e) { /* модалка показывает свою ошибку */ }
    this.load();   // обновить единый список после переназначения
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
      this.load();
    } catch (e) {
      btn.disabled = false;
      toast('Не удалось отклонить: ' + (e.message || e), 'error');
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
      this.load();
    } catch (e) {
      btn.disabled = false;
      toast('Не удалось утвердить: ' + (e.message || e) + '. Для конфликтов — блок «Детальная работа» ниже.', 'error');
    }
  },
};
