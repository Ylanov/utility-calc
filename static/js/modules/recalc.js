// static/js/modules/recalc.js
//
// Полный перерасчёт периода (аккордеон-секция «Перерасчёт периода»
// во вкладке «Операции»).
//
// UX-контракт:
// 1. Админ выбирает период из селектора, жмёт «Предпросчёт».
// 2. В фоне Celery считает новые суммы по актуальным тарифам. Мы поллим
//    /admin/recalc-jobs/{id} раз в 2 секунды и показываем прогресс.
// 3. Когда preview готов — в модалке показываем таблицу «старое vs новое»
//    и три кнопки: «Применить к БД», «Отмена», «Закрыть».
// 4. «Применить к БД» доступна только админу (сервер отбивает 403 для
//    accountant) — запускает apply-таску, мы снова поллим прогресс.
// 5. После done — строка сохраняется в историю (кнопка «История»).

import { api } from '../core/api.js';
import { toast } from '../core/dom.js';

const POLL_INTERVAL_MS = 2000;

function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    const d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
}

function fmtMoney(v) {
    const n = Number(v);
    if (!isFinite(n)) return '—';
    return n.toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' ₽';
}

function fmtDelta(v) {
    const n = Number(v);
    if (!isFinite(n) || n === 0) return '<span style="color:#6b7280;">0,00</span>';
    const sign = n > 0 ? '+' : '';
    const color = n > 0 ? '#dc2626' : '#059669';
    return `<span style="color:${color}; font-weight:600;">${sign}${n.toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ₽</span>`;
}

export const RecalcModule = {
    isInitialized: false,
    periods: [],
    currentJobId: null,
    pollTimer: null,

    async init() {
        this.cacheDOM();
        if (!this.isInitialized) {
            this.bindEvents();
            this.isInitialized = true;
        }
        await this.loadPeriods();
    },

    cacheDOM() {
        this.dom = {
            select: document.getElementById('recalcPeriodSelect'),
            btnStart: document.getElementById('btnRecalcStart'),
            btnHistory: document.getElementById('btnRecalcHistory'),
            activeJob: document.getElementById('recalcActiveJob'),
            modal: document.getElementById('recalcModal'),
            modalTitle: document.getElementById('recalcModalTitle'),
            modalBody: document.getElementById('recalcModalBody'),
            modalFooter: document.getElementById('recalcModalFooter'),
        };
    },

    bindEvents() {
        if (this.dom.btnStart) this.dom.btnStart.addEventListener('click', () => this.startPreview());
        if (this.dom.btnHistory) this.dom.btnHistory.addEventListener('click', () => this.showHistory());
        if (this.dom.modal) {
            this.dom.modal.addEventListener('click', (e) => {
                if (e.target.closest('[data-recalc-close]')) this.closeModal();
            });
        }
    },

    async loadPeriods() {
        try {
            this.periods = await api.get('/admin/recalc/periods');
            const sel = this.dom.select;
            if (!sel) return;
            sel.innerHTML = '';
            if (!this.periods.length) {
                sel.appendChild(new Option('Нет периодов', ''));
                return;
            }
            this.periods.forEach(p => {
                const label = `${p.name}${p.is_active ? ' (текущий)' : ''} — ${p.approved_readings} утв. показаний`;
                const opt = new Option(label, String(p.id));
                sel.appendChild(opt);
            });
        } catch (e) {
            toast('Не удалось загрузить периоды: ' + e.message, 'error');
        }
    },

    openModal(title) {
        if (title) this.dom.modalTitle.textContent = title;
        this.dom.modal.classList.add('open');
    },

    closeModal() {
        this._stopPoll();
        this.dom.modal.classList.remove('open');
        this.currentJobId = null;
    },

    _startPoll(jobId, onUpdate) {
        this._stopPoll();
        const tick = async () => {
            try {
                const job = await api.get(`/admin/recalc-jobs/${jobId}`);
                onUpdate(job);
                const terminal = ['preview_ready', 'done', 'failed', 'cancelled'];
                if (terminal.includes(job.status)) {
                    this._stopPoll();
                    return;
                }
            } catch (e) {
                console.warn('[RECALC] poll error:', e.message);
            }
        };
        tick();
        this.pollTimer = setInterval(tick, POLL_INTERVAL_MS);
    },

    _stopPoll() {
        if (this.pollTimer) {
            clearInterval(this.pollTimer);
            this.pollTimer = null;
        }
    },

    async startPreview() {
        const periodId = this.dom.select?.value;
        if (!periodId) return toast('Выберите период', 'error');

        const periodLabel = this.periods.find(p => String(p.id) === periodId)?.name || `#${periodId}`;
        if (!confirm(`Запустить предпросчёт для периода «${periodLabel}»?\nПоказания не будут изменены — только собран отчёт.`)) return;

        this.openModal(`Перерасчёт: ${periodLabel}`);
        this.dom.modalBody.innerHTML = this._progressBlock('Запускаем задачу…', 0);
        this.dom.modalFooter.innerHTML = `<button class="action-btn secondary-btn" data-recalc-close>Закрыть</button>`;

        try {
            const job = await api.post(`/admin/periods/${periodId}/recalc/start`);
            this.currentJobId = job.id;
            this._startPoll(job.id, (j) => this._renderJobState(j, periodLabel));
        } catch (e) {
            this.dom.modalBody.innerHTML = `<div style="padding:24px; color:var(--danger-color);">Не удалось запустить: ${escapeHtml(e.message)}</div>`;
        }
    },

    _progressBlock(label, progress) {
        return `
            <div style="text-align:center; padding:30px 20px;">
                <div style="font-size:14px; color:var(--text-secondary); margin-bottom:12px;">${escapeHtml(label)}</div>
                <div style="background:#e5e7eb; border-radius:10px; height:16px; overflow:hidden; max-width:500px; margin:0 auto;">
                    <div style="background:linear-gradient(90deg,#f59e0b,#d97706); height:100%; width:${progress}%; transition:width 0.4s;"></div>
                </div>
                <div style="margin-top:8px; font-weight:600; color:#92400e;">${progress}%</div>
            </div>
        `;
    },

    _renderJobState(job, periodLabel) {
        if (job.status === 'preview_pending' || job.status === 'apply_pending') {
            const label = job.status === 'preview_pending'
                ? `Анализируем показания… (${job.processed}/${job.total_readings})`
                : `Применяем изменения к БД… (${job.processed}/${job.total_readings})`;
            this.dom.modalBody.innerHTML = this._progressBlock(label, job.progress);
            this.dom.modalFooter.innerHTML = `
                <button class="action-btn danger-btn" data-recalc-cancel>
                    <i class="fa-solid fa-stop"></i> Отменить
                </button>
                <button class="action-btn secondary-btn" data-recalc-close>Свернуть</button>
            `;
            this.dom.modalFooter.querySelector('[data-recalc-cancel]')?.addEventListener('click', () => this.cancelJob(job.id));
            return;
        }

        if (job.status === 'preview_ready') {
            this.dom.modalBody.innerHTML = this._renderDiffReport(job, periodLabel, { readOnly: false });
            this.dom.modalFooter.innerHTML = `
                <button class="action-btn secondary-btn" data-recalc-close>Отмена</button>
                <button class="action-btn warning-btn" data-recalc-apply>
                    <i class="fa-solid fa-database"></i> Применить к БД (${job.total_readings})
                </button>
            `;
            this.dom.modalFooter.querySelector('[data-recalc-apply]')?.addEventListener('click', () => this.applyJob(job.id, periodLabel));
            return;
        }

        if (job.status === 'done') {
            this.dom.modalBody.innerHTML = this._renderDiffReport(job, periodLabel, { readOnly: true, success: true });
            this.dom.modalFooter.innerHTML = `<button class="action-btn primary-btn" data-recalc-close>Готово</button>`;
            toast('Перерасчёт применён', 'success');
            return;
        }

        if (job.status === 'failed') {
            this.dom.modalBody.innerHTML = `
                <div style="padding:24px; background:#fef2f2; border:1px solid #fecaca; border-radius:8px; color:#991b1b;">
                    <b><i class="fa-solid fa-xmark"></i> Задача упала</b>
                    <pre style="white-space:pre-wrap; margin-top:10px; font-size:12px;">${escapeHtml(job.error || '—')}</pre>
                </div>
            `;
            this.dom.modalFooter.innerHTML = `<button class="action-btn secondary-btn" data-recalc-close>Закрыть</button>`;
            return;
        }

        if (job.status === 'cancelled') {
            this.dom.modalBody.innerHTML = `
                <div style="padding:24px; background:#f3f4f6; border-radius:8px; text-align:center;">
                    <b style="color:#6b7280;">Задача отменена</b>
                </div>
            `;
            this.dom.modalFooter.innerHTML = `<button class="action-btn secondary-btn" data-recalc-close>Закрыть</button>`;
            return;
        }
    },

    _renderDiffReport(job, periodLabel, { readOnly, success } = {}) {
        const s = job.diff_summary || {};
        const top = s.top || [];
        const headerBg = success ? '#ecfdf5' : '#fef3c7';
        const headerBorder = success ? '#a7f3d0' : '#fde68a';
        const headerColor = success ? '#065f46' : '#92400e';
        const headerText = success
            ? `Готово! Обновлено ${s.total} показаний.`
            : `Предпросчёт готов. Будут обновлены ${s.total} показаний.`;

        const rowsHtml = top.length ? top.map(r => `
            <tr>
                <td style="padding:8px 10px; font-family:monospace; color:#6b7280;">#${r.reading_id}</td>
                <td style="padding:8px 10px; font-weight:600;">${escapeHtml(r.username)}</td>
                <td style="padding:8px 10px; color:var(--text-secondary); font-size:12px;">${escapeHtml(r.room || '')}</td>
                <td style="padding:8px 10px; text-align:right; font-family:monospace; color:#6b7280;">${fmtMoney(r.old_total)}</td>
                <td style="padding:8px 10px; text-align:right; font-family:monospace; font-weight:600;">${fmtMoney(r.new_total)}</td>
                <td style="padding:8px 10px; text-align:right;">${fmtDelta(r.delta)}</td>
            </tr>
        `).join('') : `<tr><td colspan="6" style="padding:16px; text-align:center; color:var(--text-secondary);">Сумма ни у кого не изменилась — тарифы уже актуальны.</td></tr>`;

        return `
            <div style="background:${headerBg}; border:1px solid ${headerBorder}; border-radius:8px; padding:14px 18px; margin-bottom:16px; color:${headerColor};">
                <b><i class="fa-solid fa-${success ? 'check' : 'magnifying-glass-chart'}"></i> ${escapeHtml(headerText)}</b>
                <div style="margin-top:6px; font-size:13px; color:inherit;">Период: <b>${escapeHtml(periodLabel)}</b></div>
            </div>

            <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(140px, 1fr)); gap:10px; margin-bottom:16px;">
                ${this._kpiCard('Всего', s.total ?? 0, '#2563eb')}
                ${this._kpiCard('Без изменений', s.unchanged ?? 0, '#6b7280')}
                ${this._kpiCard('↑ Увеличилось', s.increased ?? 0, '#dc2626')}
                ${this._kpiCard('↓ Уменьшилось', s.decreased ?? 0, '#059669')}
                ${this._kpiCard('Было суммарно', fmtMoney(s.sum_old), '#6b7280', true)}
                ${this._kpiCard('Станет', fmtMoney(s.sum_new), '#111827', true)}
                ${this._kpiCard('Δ', fmtDelta(s.delta), '#d97706', true, true)}
            </div>

            <h4 style="margin:0 0 10px;">
                <i class="fa-solid fa-list"></i>
                Топ-${top.length} изменений по абсолютной разнице
            </h4>
            <div style="max-height:280px; overflow:auto; border:1px solid var(--border-color); border-radius:6px; background:white;">
                <table style="width:100%; border-collapse:collapse; font-size:13px;">
                    <thead style="position:sticky; top:0; background:#f9fafb; z-index:1;">
                        <tr style="text-align:left;">
                            <th style="padding:10px; border-bottom:1px solid var(--border-color);">ID</th>
                            <th style="padding:10px; border-bottom:1px solid var(--border-color);">Жилец</th>
                            <th style="padding:10px; border-bottom:1px solid var(--border-color);">Комната</th>
                            <th style="padding:10px; border-bottom:1px solid var(--border-color); text-align:right;">Было</th>
                            <th style="padding:10px; border-bottom:1px solid var(--border-color); text-align:right;">Станет</th>
                            <th style="padding:10px; border-bottom:1px solid var(--border-color); text-align:right;">Δ</th>
                        </tr>
                    </thead>
                    <tbody>${rowsHtml}</tbody>
                </table>
            </div>

            ${!readOnly ? `
                <p style="margin:14px 0 0; font-size:12px; color:var(--text-secondary);">
                    <i class="fa-solid fa-circle-info"></i>
                    Показания изменятся в базе только после нажатия «Применить к БД». Пока это предпросмотр.
                </p>
            ` : ''}
        `;
    },

    _kpiCard(label, value, color, wide = false, isHtml = false) {
        return `
            <div style="background:white; border:1px solid var(--border-color); border-radius:8px; padding:10px 12px; ${wide ? '' : ''}">
                <div style="font-size:11px; color:var(--text-secondary); text-transform:uppercase; letter-spacing:0.3px;">${escapeHtml(label)}</div>
                <div style="font-size:16px; font-weight:700; color:${color}; margin-top:4px;">${isHtml ? value : escapeHtml(String(value))}</div>
            </div>
        `;
    },

    async applyJob(jobId, periodLabel) {
        if (!confirm('Применить пересчитанные значения к БД?\nСуммы утверждённых показаний будут обновлены. Действие необратимо.')) return;
        try {
            const job = await api.post(`/admin/recalc-jobs/${jobId}/apply`);
            this._startPoll(jobId, (j) => this._renderJobState(j, periodLabel));
        } catch (e) {
            toast('Ошибка запуска apply: ' + e.message, 'error');
        }
    },

    async cancelJob(jobId) {
        if (!confirm('Отменить выполнение задачи? Незавершённые изменения откатятся.')) return;
        try {
            await api.post(`/admin/recalc-jobs/${jobId}/cancel`);
            toast('Задача отменена', 'info');
        } catch (e) {
            toast('Не удалось отменить: ' + e.message, 'error');
        }
    },

    async showHistory() {
        const periodId = this.dom.select?.value;
        if (!periodId) return toast('Выберите период', 'error');
        const periodLabel = this.periods.find(p => String(p.id) === periodId)?.name || `#${periodId}`;

        this.openModal(`История перерасчётов: ${periodLabel}`);
        this.dom.modalBody.innerHTML = `<div style="text-align:center; padding:40px;"><i class="fa-solid fa-spinner fa-spin"></i> Загрузка…</div>`;
        this.dom.modalFooter.innerHTML = `<button class="action-btn secondary-btn" data-recalc-close>Закрыть</button>`;

        try {
            const jobs = await api.get(`/admin/periods/${periodId}/recalc-jobs`);
            if (!jobs.length) {
                this.dom.modalBody.innerHTML = `<div style="padding:40px; text-align:center; color:var(--text-secondary);">По этому периоду перерасчётов ещё не было.</div>`;
                return;
            }
            this.dom.modalBody.innerHTML = `
                <table style="width:100%; border-collapse:collapse; font-size:13px; background:white; border:1px solid var(--border-color); border-radius:6px; overflow:hidden;">
                    <thead style="background:#f9fafb;">
                        <tr style="text-align:left;">
                            <th style="padding:10px; border-bottom:1px solid var(--border-color);">ID</th>
                            <th style="padding:10px; border-bottom:1px solid var(--border-color);">Статус</th>
                            <th style="padding:10px; border-bottom:1px solid var(--border-color);">Кто запустил</th>
                            <th style="padding:10px; border-bottom:1px solid var(--border-color);">Создан</th>
                            <th style="padding:10px; border-bottom:1px solid var(--border-color);">Применён</th>
                            <th style="padding:10px; border-bottom:1px solid var(--border-color); text-align:right;">Всего</th>
                            <th style="padding:10px; border-bottom:1px solid var(--border-color); text-align:right;">Δ суммы</th>
                        </tr>
                    </thead>
                    <tbody>${jobs.map(j => this._historyRow(j)).join('')}</tbody>
                </table>
            `;
        } catch (e) {
            this.dom.modalBody.innerHTML = `<div style="padding:24px; color:var(--danger-color);">Ошибка: ${escapeHtml(e.message)}</div>`;
        }
    },

    _historyRow(j) {
        const s = j.diff_summary || {};
        const statusColors = {
            preview_pending: ['#3b82f6', '#dbeafe'],
            preview_ready:   ['#7c3aed', '#ede9fe'],
            apply_pending:   ['#d97706', '#fef3c7'],
            done:            ['#059669', '#d1fae5'],
            failed:          ['#dc2626', '#fee2e2'],
            cancelled:       ['#6b7280', '#f3f4f6'],
        };
        const [fg, bg] = statusColors[j.status] || ['#6b7280', '#f3f4f6'];
        const fmtDt = (iso) => iso ? new Date(iso).toLocaleString('ru-RU') : '—';

        return `
            <tr>
                <td style="padding:10px; border-bottom:1px solid #f3f4f6; font-family:monospace; color:#6b7280;">#${j.id}</td>
                <td style="padding:10px; border-bottom:1px solid #f3f4f6;">
                    <span style="background:${bg}; color:${fg}; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600;">${escapeHtml(j.status)}</span>
                </td>
                <td style="padding:10px; border-bottom:1px solid #f3f4f6;">${escapeHtml(j.started_by_username || '—')}</td>
                <td style="padding:10px; border-bottom:1px solid #f3f4f6; font-size:12px;">${escapeHtml(fmtDt(j.created_at))}</td>
                <td style="padding:10px; border-bottom:1px solid #f3f4f6; font-size:12px;">${escapeHtml(fmtDt(j.applied_at))}</td>
                <td style="padding:10px; border-bottom:1px solid #f3f4f6; text-align:right;">${j.total_readings || 0}</td>
                <td style="padding:10px; border-bottom:1px solid #f3f4f6; text-align:right;">${s.delta ? fmtDelta(s.delta) : '—'}</td>
            </tr>
        `;
    },
};
