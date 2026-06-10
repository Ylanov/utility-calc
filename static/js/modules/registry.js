// static/js/modules/registry.js
// Единый реестр показаний (Фаза 2): ОДИН список из боевых MeterReading и буфера
// GSheetsImportRow в общем формате, с бейджем источника. Данные — /api/admin/registry.
// Действия делегируются существующим эндпоинтам по row_type+id.

import { api } from '../core/api.js';
import { toast, showConfirm } from '../core/dom.js';

function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

const SRC = {
  user:    { t: '📱 QR/приложение', c: '#1e40af', b: '#dbeafe' },
  gsheets: { t: '📄 Google Sheets', c: '#166534', b: '#dcfce7' },
  buffer:  { t: '📄 Sheets (буфер)', c: '#92400e', b: '#fef3c7' },
  auto:    { t: '🤖 Норматив/авто', c: '#6b21a8', b: '#f3e8ff' },
  manual:  { t: '✍️ Вручную',       c: '#475569', b: '#f1f5f9' },
};
const STATUS = {
  draft: { t: 'Черновик', c: '#92400e', b: '#fef3c7' },
  approved: { t: 'Утверждено', c: '#166534', b: '#dcfce7' },
  pending: { t: 'Ожидает', c: '#92400e', b: '#fef3c7' },
  conflict: { t: 'Конфликт', c: '#991b1b', b: '#fee2e2' },
  unmatched: { t: 'Жилец не найден', c: '#991b1b', b: '#fee2e2' },
  auto_approved: { t: 'Авто-утв.', c: '#1e40af', b: '#dbeafe' },
};

function badge(meta, fallback) {
  var m = meta || { t: fallback || '—', c: '#475569', b: '#f1f5f9' };
  return '<span style="display:inline-block; padding:2px 8px; border-radius:6px; font-size:11px; font-weight:600; background:' + m.b + '; color:' + m.c + '; white-space:nowrap;">' + esc(m.t) + '</span>';
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
    this.dom.body.innerHTML = '<tr><td colspan="8" style="padding:30px; text-align:center; color:var(--text-secondary);"><i class="fa-solid fa-spinner fa-spin"></i> Загрузка…</td></tr>';
    var q = '/admin/registry?page=' + this.state.page + '&limit=' + this.state.limit +
      (this.state.source ? '&source=' + encodeURIComponent(this.state.source) : '') +
      (this.state.search ? '&search=' + encodeURIComponent(this.state.search) : '');
    let data;
    try { data = await api.get(q); }
    catch (e) { this.dom.body.innerHTML = '<tr><td colspan="8" style="padding:24px; text-align:center; color:var(--danger-color);">Ошибка: ' + esc(e.message || e) + '</td></tr>'; return; }
    this.render(data);
  },

  render(data) {
    var items = (data && data.items) || [];
    if (this.dom.total) this.dom.total.textContent = 'Всего: ' + (data.total || 0) + (data.period ? ' · период «' + esc(data.period) + '»' : '');
    if (!items.length) {
      this.dom.body.innerHTML = '<tr><td colspan="8" style="padding:30px; text-align:center; color:var(--text-secondary);">Нет записей по фильтру.</td></tr>';
      return;
    }
    this.dom.body.innerHTML = items.map((r) => {
      var when = r.timestamp ? new Date(r.timestamp).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' }) : '—';
      var matched = '';
      if (r.row_type === 'gsheets' && r.matched) {
        matched = r.matched.user_id
          ? '<span style="font-size:11px; color:var(--text-secondary);">матч ' + (r.matched.score || 0) + '%</span>'
          : '<span style="font-size:11px; color:#991b1b;">не сопоставлен</span>';
      }
      var place = (r.dormitory ? esc(r.dormitory) + ' / ' : '') + esc(r.room || '—');
      var sum = (r.sum != null) ? Number(r.sum).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' ₽' : '—';
      var canApprove = (r.status === 'draft' || r.status === 'pending' || r.status === 'auto_approved');
      var act = canApprove
        ? '<button class="action-btn success-btn" style="padding:4px 10px; font-size:12px;" data-reg-approve data-rt="' + r.row_type + '" data-id="' + r.id + '"><i class="fa-solid fa-check"></i> Утвердить</button>'
        : (r.status === 'conflict' || r.status === 'unmatched'
            ? '<span style="font-size:11px; color:var(--text-secondary);">разбор ниже ↓</span>' : '');
      return '<tr>' +
        '<td>' + badge(SRC[r.source], r.source) + '</td>' +
        '<td style="font-size:12px; color:var(--text-secondary);">' + esc(when) + '</td>' +
        '<td><b>' + esc(r.fio || '—') + '</b>' + (matched ? '<br>' + matched : '') + '</td>' +
        '<td style="font-size:13px;">' + place + '</td>' +
        '<td style="text-align:right; font-family:monospace;">' + esc(r.hot || '—') + ' / ' + esc(r.cold || '—') + (r.elect ? ' / ' + esc(r.elect) : '') + '</td>' +
        '<td>' + badge(STATUS[r.status], r.status) + (r.anomaly_score ? ' <span style="font-size:11px; color:' + (r.anomaly_score >= 80 ? '#991b1b' : '#92400e') + ';">' + r.anomaly_score + '</span>' : '') + '</td>' +
        '<td style="text-align:right; color:#15803d; font-weight:600;">' + sum + '</td>' +
        '<td style="text-align:right;">' + act + '</td>' +
        '</tr>';
    }).join('');

    this.dom.body.querySelectorAll('[data-reg-approve]').forEach((btn) => {
      btn.addEventListener('click', () => this.approve(btn.dataset.rt, btn.dataset.id, btn));
    });
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
