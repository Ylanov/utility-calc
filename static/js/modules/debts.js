// static/js/modules/debts.js
//
// Вкладка «Долги 1С» — единая панель для финансиста:
//   * KPI-панель сверху (суммарный долг/переплата, должники, ср. долг, последний импорт)
//   * Импорт Excel с живым прогрессом задачи
//   * История импортов с откатом и просмотром «не найденных» ФИО
//   * Таблица с фильтрами (должники/переплаты/общежитие/мин. долг), сортировкой по колонкам,
//     цветовыми чипами уровней долга
//   * Модалка корректировки (замена prompt-цепочки): счёт, сумма, шаблон причины + комментарий
//   * Экспорт текущей выборки в Excel

import { api } from '../core/api.js';
import { el, toast, setLoading, showConfirm } from '../core/dom.js';

function esc(s) {
    if (s == null) return '';
    return String(s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtMoney(v) {
    const n = Number(v || 0);
    return n.toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtDateTime(iso) {
    if (!iso) return '—';
    try { return new Date(iso).toLocaleString('ru-RU'); } catch { return iso; }
}

export const DebtsModule = {
    isInitialized: false,
    state: {
        page: 1, limit: 50, total: 0, search: '',
        mode: 'users',  // 'users' | 'rooms' — учёт по жильцам или по квартирам
        viewPeriodId: '',  // период ПРОСМОТРА долгов ('' = авто: активный/последний импорт)
        filterType: '', dormitory: '', minDebt: '',
        hideEmpty: true,  // Bug AB: по умолчанию скрываем пустые
        sortBy: 'room', sortDir: 'asc',
        importTaskId: null, pollTimer: null, isUploading: false, lastRequestId: 0,
        currentPollId: null,
    },

    init() {
        this.cacheDOM();
        // Bug AB: синхронизируем стартовое состояние чекбокса с state
        // (HTML может прийти с другим default'ом).
        if (this.dom.hideEmpty) {
            this.state.hideEmpty = !!this.dom.hideEmpty.checked;
        }
        if (!this.isInitialized) {
            this.bindEvents();
            this.isInitialized = true;
        }
        // Регистрируем глобальный хэндлер — аналитик из «Центр анализа»
        // («Сверка 1С») может позвать модалку «не найдены» по log_id.
        // Живёт на window, чтобы не плодить event-bus.
        window.__openDebtsNotFound = (logId) => {
            const tabBtn = document.querySelector('.tab-btn[data-tab="debts"]');
            tabBtn?.click();
            setTimeout(() => this.openNotFoundModal(logId), 120);
        };
        this.loadViewPeriods();
        this.loadStats();
        this.loadUnassigned();
        this.loadDormitories();
        this.loadDebtPeriods();
        this.loadUsers();
        this.loadImportHistory();
        this.loadGisgmpStatus();
    },

    // ─── Авто-подгрузка ГИС ГМП (мост-расширение) ──────────────────────────
    async loadGisgmpStatus() {
        const box = this.dom.gisgmpStatus;
        if (!box) return;
        try {
            const s = await api.get('/financier/gisgmp/status');
            if (!s.configured) {
                box.innerHTML = '<span style="color:#b91c1c;">⚠ Токен GISGMP_SYNC_TOKEN не задан в .env сервера — авто-подгрузка выключена.</span>';
                return;
            }
            if (!s.last_sync_at) {
                box.innerHTML = '<span style="color:#92400e;">Токен задан, синхронизаций ещё не было. Установите расширение и нажмите в нём «Синхр. сейчас».</span>';
                return;
            }
            const when = new Date(s.last_sync_at).toLocaleString('ru-RU');
            box.innerHTML =
                `Последняя синхронизация: <b>${when}</b><br>` +
                `Обновлено жильцов: <b>${s.last_updated ?? 0}</b>, ` +
                `создано: <b>${s.last_created ?? 0}</b>, ` +
                `не найдено ФИО: <b>${s.last_not_found ?? 0}</b>.`;
        } catch (e) {
            box.textContent = 'Не удалось загрузить статус ГИС ГМП.';
        }
    },

    async downloadGisgmpExtension() {
        try {
            await api.download('/financier/gisgmp/bridge.zip', 'gisgmp-bridge.zip');
        } catch (e) {
            toast('Не удалось скачать расширение: ' + (e?.message || e), 'error');
        }
    },

    cacheDOM() {
        this.dom = {
            // Таблица
            tableBody: document.getElementById('debtsTableBody'),
            btnRefresh: document.getElementById('btnRefreshDebts'),
            btnExport: document.getElementById('btnExportDebts'),
            btnZombieCheck: document.getElementById('btnZombieCheck'),
            btnIntegrityCheck: document.getElementById('btnIntegrityCheck'),
            btnPrev: document.getElementById('btnPrevDebts'),
            btnNext: document.getElementById('btnNextDebts'),
            pageInfo: document.getElementById('debtsPageInfo'),
            searchInput: document.getElementById('debtsSearchInput'),
            // Фильтры
            filterType: document.getElementById('debtsFilterType'),
            filterDorm: document.getElementById('debtsFilterDormitory'),
            minDebt: document.getElementById('debtsMinDebt'),
            hideEmpty: document.getElementById('debtsHideEmpty'),
            // Импорт
            btnUpload: document.getElementById('btnUploadDebts'),
            // Парный импорт v2 — два отдельных file-input. Старый
            // #debtFile1C оставлен fallback'ом если HTML где-то ещё
            // содержит legacy-шаблон, но в актуальном tab_debts.html
            // его нет.
            inputUpload209: document.getElementById('debtFile209'),
            inputUpload205: document.getElementById('debtFile205'),
            inputUpload: document.getElementById('debtFile1C'),
            uploadResult: document.getElementById('uploadResult'),
            periodSelect: document.getElementById('debtPeriodSelect'),
            viewPeriod: document.getElementById('debtsViewPeriod'),
            unassignedCard: document.getElementById('debtsUnassignedCard'),
            unassignedMeta: document.getElementById('debtsUnassignedMeta'),
            unassignedBody: document.getElementById('debtsUnassignedBody'),
            btnToggleUnassigned: document.getElementById('btnToggleUnassigned'),
            // KPI
            stats: document.getElementById('debtsStats'),
            // История
            importHistoryList: document.getElementById('importHistoryList'),
            btnRefreshImportHistory: document.getElementById('btnRefreshImportHistory'),
            // Авто-подгрузка ГИС ГМП (мост-расширение)
            gisgmpStatus: document.getElementById('gisgmpStatus'),
            btnDownloadGisgmpExt: document.getElementById('btnDownloadGisgmpExt'),
            // Модалка корректировки
            adjustModal: document.getElementById('debtAdjustModal'),
            adjustForm: document.getElementById('debtAdjustForm'),
            adjustUserId: document.getElementById('adjustUserId'),
            adjustUserName: document.getElementById('adjustUserName'),
            adjustAccount: document.getElementById('adjustAccount'),
            adjustAmount: document.getElementById('adjustAmount'),
            adjustTemplate: document.getElementById('adjustTemplate'),
            adjustDescription: document.getElementById('adjustDescription'),
            // Модалка «не найдено»
            notFoundModal: document.getElementById('notFoundModal'),
            notFoundList: document.getElementById('notFoundList'),
            notFoundLogMeta: document.getElementById('notFoundLogMeta'),
        };
    },

    bindEvents() {
        // Переключатель режима учёта: жильцы (ФИО) / квартиры (адрес).
        document.getElementById('debtsModeUsers')?.addEventListener('click', () => this.setMode('users'));
        document.getElementById('debtsModeRooms')?.addEventListener('click', () => this.setMode('rooms'));
        // Период просмотра долгов (когда активного нет — выбрать май/апрель).
        this.dom.viewPeriod?.addEventListener('change', () => {
            this.state.viewPeriodId = this.dom.viewPeriod.value || '';
            this.state.page = 1;
            this.loadStats();
            this.loadUsers();
            this.loadUnassigned();
        });
        this.dom.btnToggleUnassigned?.addEventListener('click', () => {
            const b = this.dom.unassignedBody;
            if (!b) return;
            const show = b.style.display === 'none';
            b.style.display = show ? 'block' : 'none';
            this.dom.btnToggleUnassigned.textContent = show ? 'Скрыть список' : 'Показать список';
        });
        this.dom.btnRefresh?.addEventListener('click', () => this.reload());
        this.dom.btnExport?.addEventListener('click', () => this.exportExcel());
        this.dom.btnZombieCheck?.addEventListener('click', () => this.openZombieModal());
        this.dom.btnIntegrityCheck?.addEventListener('click', () => this.openIntegrityModal());
        this.dom.btnDownloadGisgmpExt?.addEventListener('click', () => this.downloadGisgmpExtension());
        this.dom.btnUpload?.addEventListener('click', () => this.handleUpload());

        // Авто-предпросмотр при выборе файла (Bug T)
        this.dom.inputUpload209?.addEventListener('change', () => this.previewFile('209'));
        this.dom.inputUpload205?.addEventListener('change', () => this.previewFile('205'));
        // Сохраним признак дубликата для блокировки upload.
        this._lastPreview209 = null;
        this._lastPreview205 = null;
        this.dom.btnPrev?.addEventListener('click', () => this.changePage(-1));
        this.dom.btnNext?.addEventListener('click', () => this.changePage(1));

        const refilter = () => {
            this.state.filterType = this.dom.filterType?.value || '';
            this.state.dormitory = this.dom.filterDorm?.value || '';
            this.state.minDebt = this.dom.minDebt?.value || '';
            this.state.page = 1;
            this.loadUsers();
        };
        this.dom.filterType?.addEventListener('change', refilter);
        this.dom.filterDorm?.addEventListener('change', refilter);

        let minDebtTimer;
        this.dom.minDebt?.addEventListener('input', () => {
            clearTimeout(minDebtTimer);
            minDebtTimer = setTimeout(refilter, 400);
        });

        // Bug AB: «Скрыть пустых» — пользователи без данных из 1С
        this.dom.hideEmpty?.addEventListener('change', (e) => {
            this.state.hideEmpty = e.target.checked;
            this.state.page = 1;
            this.loadUsers();
        });

        let searchTimer;
        this.dom.searchInput?.addEventListener('input', (e) => {
            clearTimeout(searchTimer);
            searchTimer = setTimeout(() => {
                this.state.search = e.target.value || '';
                this.state.page = 1;
                this.loadUsers();
            }, 400);
        });

        // Сортировка по клику на заголовок
        document.querySelectorAll('[data-debt-sort]').forEach(th => {
            th.addEventListener('click', () => {
                const field = th.dataset.debtSort;
                if (this.state.sortBy === field) {
                    this.state.sortDir = this.state.sortDir === 'asc' ? 'desc' : 'asc';
                } else {
                    this.state.sortBy = field;
                    this.state.sortDir = field === 'debt' ? 'desc' : 'asc';
                }
                this.updateSortIcons();
                this.state.page = 1;
                this.loadUsers();
            });
        });

        // Модалка корректировки
        this.dom.adjustModal?.addEventListener('click', (e) => {
            if (e.target.closest('[data-adjust-close]')) this.closeAdjustModal();
        });
        this.dom.adjustForm?.addEventListener('submit', (e) => this.submitAdjust(e));
        this.dom.adjustTemplate?.addEventListener('change', (e) => {
            const val = e.target.value;
            if (val && this.dom.adjustDescription && !this.dom.adjustDescription.value.trim()) {
                this.dom.adjustDescription.value = val;
            }
        });

        // Модалка «не найдено»
        this.dom.notFoundModal?.addEventListener('click', (e) => {
            if (e.target.closest('[data-nf-close]')) this.closeNotFoundModal();
        });

        // История импортов
        this.dom.btnRefreshImportHistory?.addEventListener('click', () => this.loadImportHistory());
        this.dom.importHistoryList?.addEventListener('click', (e) => {
            const btn = e.target.closest('button[data-history-action]');
            if (!btn) return;
            const action = btn.dataset.historyAction;
            const logId = Number(btn.dataset.logId);
            if (action === 'view-not-found') this.openNotFoundModal(logId);
            else if (action === 'undo') this.undoImport(logId);
            else if (action === 'diff') this.openDiffModal(logId);
            else if (action === 'diagnose') this.openDiagnoseModal(logId);
            else if (action === 'reparse') this.reparseImport(logId);  // Bug AE
            else if (action === 'delete') this.deleteImportHistory(logId);
            else if (action === 'cleanup') this.cleanupImportHistory();
        });
    },

    reload() { this.state.page = 1; this.loadUsers(); this.loadStats(); this.loadUnassigned(); },

    changePage(delta) {
        const newPage = this.state.page + delta;
        if (newPage < 1) return;
        this.state.page = newPage;
        this.loadUsers();
    },

    updateSortIcons() {
        document.querySelectorAll('[data-debt-sort]').forEach(th => {
            th.classList.remove('sort-asc', 'sort-desc');
            if (th.dataset.debtSort === this.state.sortBy) {
                th.classList.add(`sort-${this.state.sortDir}`);
            }
        });
    },

    clearPoll() {
        if (this.state.pollTimer) { clearTimeout(this.state.pollTimer); this.state.pollTimer = null; }
        this.state.currentPollId = null;
    },

    // Список периодов для дропдауна «Период просмотра» (шапка списка долгов).
    async loadViewPeriods() {
        const sel = this.dom.viewPeriod;
        if (!sel) return;
        try {
            const periods = await api.get('/admin/periods/history');
            const prev = sel.value;
            sel.innerHTML = '';
            (periods || []).forEach(p => {
                const opt = document.createElement('option');
                opt.value = String(p.id);
                opt.textContent = p.name + (p.is_active ? ' (активный)' : '');
                sel.appendChild(opt);
            });
            if (prev) sel.value = prev;
        } catch { /* молча — останется заглушка «Период…» */ }
    },

    // Сводка неразнесённых долгов (ФИО из 1С, не привязанные к жильцу/комнате).
    async loadUnassigned() {
        const card = this.dom.unassignedCard;
        if (!card) return;
        try {
            const qs = this.state.viewPeriodId ? `?period_id=${this.state.viewPeriodId}` : '';
            const d = await api.get(`/financier/debts/unassigned${qs}`);
            if (!d.count) { card.style.display = 'none'; return; }
            card.style.display = '';
            if (this.dom.unassignedMeta) {
                this.dom.unassignedMeta.textContent = `— ${fmtMoney(d.total_debt)} ₽ · ${d.count} ФИО`;
            }
            const rows = (d.items || []).map(it => `
                <tr style="border-bottom:1px solid var(--border-color);">
                    <td style="padding:6px 10px;">${esc(it.fio)}</td>
                    <td style="padding:6px 10px; text-align:center; color:var(--text-secondary); font-size:11px;">${esc((it.accounts || []).join(', '))}</td>
                    <td style="padding:6px 10px; text-align:right; color:#991b1b; font-weight:600;">${it.debt > 0 ? fmtMoney(it.debt) + ' ₽' : '—'}</td>
                    <td style="padding:6px 10px; text-align:right; color:#15803d;">${it.overpayment > 0 ? fmtMoney(it.overpayment) + ' ₽' : '—'}</td>
                </tr>`).join('');
            if (this.dom.unassignedBody) {
                this.dom.unassignedBody.innerHTML = `
                    <div style="overflow-x:auto; border:1px solid var(--border-color); border-radius:8px;">
                        <table style="width:100%; border-collapse:collapse; font-size:13px; min-width:480px;">
                            <thead style="background:var(--bg-page); font-size:11px; color:var(--text-secondary); text-transform:uppercase;">
                                <tr>
                                    <th style="text-align:left; padding:6px 10px;">ФИО из 1С</th>
                                    <th style="text-align:center; padding:6px 10px;">Счета</th>
                                    <th style="text-align:right; padding:6px 10px;">Долг</th>
                                    <th style="text-align:right; padding:6px 10px;">Переплата</th>
                                </tr>
                            </thead>
                            <tbody>${rows}</tbody>
                        </table>
                    </div>`;
            }
        } catch { /* молча — карточка просто не покажется */ }
    },

    // ==========================================================================
    // KPI
    // ==========================================================================
    async loadStats() {
        if (!this.dom.stats) return;
        try {
            const qs = this.state.viewPeriodId ? `?period_id=${this.state.viewPeriodId}` : '';
            const s = await api.get(`/financier/debts/stats${qs}`);
            // «Авто»-режим: подтягиваем дропдаун к периоду, который выбрал бэк
            // (активный → последний импорт → свежий), чтобы было видно, что
            // показываем именно май, а не пустой активный период.
            if (!this.state.viewPeriodId && s.period_id) {
                this.state.viewPeriodId = String(s.period_id);
                if (this.dom.viewPeriod) this.dom.viewPeriod.value = String(s.period_id);
            }
            this.renderStats(s);
        } catch (e) {
            this.dom.stats.innerHTML = `<div style="padding:14px; color:var(--danger-color); grid-column:1/-1;">Ошибка аналитики: ${esc(e.message)}</div>`;
        }
    },

    renderStats(s) {
        const card = (bg, color, icon, value, label, hint) => `
            <div style="background:${bg}; border-radius:10px; padding:14px 12px; border:1px solid ${color}33;">
                <div style="display:flex; align-items:center; gap:8px; color:${color}; font-size:12px; margin-bottom:4px;">
                    <span style="font-size:16px;">${icon}</span>${esc(label)}
                </div>
                <div style="font-size:20px; font-weight:700; color:#111827;">${value}</div>
                ${hint ? `<div style="font-size:11px; color:var(--text-secondary); margin-top:2px;">${esc(hint)}</div>` : ''}
            </div>
        `;

        const lastImp = s.last_import;
        const lastHtml = lastImp
            ? `счёт ${lastImp.account_type}, ${fmtDateTime(lastImp.started_at)} · ${lastImp.started_by || '—'}`
            : 'импортов ещё не было';

        // Режим «Квартиры»: считаем помещения, а не людей.
        const rooms = this.state.mode === 'rooms';

        this.dom.stats.innerHTML = [
            card('#f5f3ff', '#7c3aed', '📅', s.period_name || '—', 'Период (просмотр)',
                rooms ? `всего квартир: ${s.total_rooms ?? '—'}` : `всего жильцов: ${s.total_users}`),
            rooms
                ? card('#fef2f2', '#dc2626', '🏠', s.rooms_with_debt_count ?? 0, 'Квартир с долгом',
                    `средний долг: ${fmtMoney(s.avg_debt_per_room)} ₽ · жильцов-должников: ${s.debtors_count}`)
                : card('#fef2f2', '#dc2626', '🔴', s.debtors_count, 'Должников',
                    `средний долг: ${fmtMoney(s.avg_debt_per_debtor)} ₽`),
            rooms
                ? card('#ecfdf5', '#10b981', '🟢', s.rooms_overpaying_count ?? 0, 'Квартир с переплатой', `сумма: ${fmtMoney(s.total_overpay)} ₽`)
                : card('#ecfdf5', '#10b981', '🟢', s.overpayers_count, 'С переплатами', `сумма: ${fmtMoney(s.total_overpay)} ₽`),
            card('#fff7ed', '#ea580c', '💰', `${fmtMoney(s.total_debt)} ₽`, 'Суммарный долг', `209: ${fmtMoney(s.total_debt_209)} · 205: ${fmtMoney(s.total_debt_205)}`),
            card('#eff6ff', '#2563eb', '📊', s.readings_count, 'Показаний в периоде', ''),
            card('#f9fafb', '#6b7280', '⏱️', lastImp ? lastImp.status : '—', 'Последний импорт', lastHtml),
        ].join('');
    },

    // ==========================================================================
    // Список общежитий в фильтр
    // ==========================================================================
    async loadDormitories() {
        if (!this.dom.filterDorm) return;
        try {
            const dorms = await api.get('/financier/debts/dormitories');
            const prev = this.dom.filterDorm.value;
            this.dom.filterDorm.innerHTML = '<option value="">Все общежития</option>';
            dorms.forEach(d => {
                const opt = document.createElement('option');
                opt.value = d;
                opt.textContent = d;
                this.dom.filterDorm.appendChild(opt);
            });
            if (prev) this.dom.filterDorm.value = prev;
        } catch { /* молча */ }
    },

    // ==========================================================================
    // ИМПОРТ
    // ==========================================================================
    /** Bug T: при выборе файла авто-парсит его и показывает сводку под
     *  input'ом. Проверяет дубликат по SHA256 и предупреждает если файл
     *  уже импортировали. */
    async previewFile(accountType) {
        const input = accountType === '209' ? this.dom.inputUpload209 : this.dom.inputUpload205;
        const file = input?.files?.[0] || null;
        // Контейнер для preview-сводки: создаём рядом с input если ещё нет.
        const previewId = `debtPreview${accountType}`;
        let preview = document.getElementById(previewId);
        if (!preview && input) {
            preview = document.createElement('div');
            preview.id = previewId;
            preview.style.cssText = 'margin-top:6px; padding:6px 9px; border-radius:4px; font-size:10.5px; line-height:1.35; max-height:80px; overflow:hidden;';
            // Вставляем плашку ПОСЛЕ контейнера input'а, не внутрь — иначе
            // она «наезжает» на input при длинном hash/sample_fio.
            const wrapper = input.closest('.upload-row') || input.parentElement;
            wrapper?.parentElement?.insertBefore(preview, wrapper.nextSibling) || wrapper?.appendChild(preview);
        }
        if (!file) {
            if (preview) preview.innerHTML = '';
            if (accountType === '209') this._lastPreview209 = null;
            else this._lastPreview205 = null;
            return;
        }
        if (preview) preview.innerHTML = '<span style="color:var(--text-secondary);"><i class="fa-solid fa-spinner fa-spin"></i> Анализируем файл…</span>';

        const fd = new FormData();
        fd.append('account_type', accountType);
        fd.append('file', file);
        try {
            const res = await api.post('/financier/debts/preview-file', fd);
            if (accountType === '209') this._lastPreview209 = res;
            else this._lastPreview205 = res;

            const sizeKb = (res.size_bytes / 1024).toFixed(1);
            let bg, color, statusLine;
            if (res.duplicate_of) {
                bg = '#fef3c7'; color = '#92400e';
                const d = res.duplicate_of;
                const date = d.started_at?.split('T')[0] || '—';
                statusLine = `⚠ <b>Дубликат</b> №${d.log_id} (${date}, ${d.status})`;
            } else if (res.rows_with_fio === 0) {
                bg = '#fee2e2'; color = '#991b1b';
                statusLine = '❌ <b>ФИО не найдено</b> — не ОСВ 1С?';
            } else {
                bg = '#dcfce7'; color = '#166534';
                statusLine = `✓ <b>${res.rows_with_fio}</b> строк с ФИО`;
            }
            const sampleText = res.sample_fio?.length
                ? ` · ${res.sample_fio.slice(0, 2).map(s => esc(s.length > 24 ? s.slice(0, 22) + '…' : s)).join(', ')}`
                : '';

            if (preview) {
                preview.style.background = bg;
                preview.style.color = color;
                preview.innerHTML = `
                    ${statusLine}
                    <span style="color:var(--text-secondary); margin-left:6px;">${esc(res.file_name.length > 28 ? res.file_name.slice(0, 26) + '…' : res.file_name)} · ${sizeKb}KB${sampleText}</span>
                `;
            }
        } catch (e) {
            if (preview) {
                preview.style.background = '#fee2e2';
                preview.style.color = '#991b1b';
                preview.innerHTML = `<b>Ошибка анализа:</b> ${esc(e.message)}`;
            }
        }
    },

    // Список периодов для выбора «за какой месяц грузим». По умолчанию активный.
    async loadDebtPeriods() {
        const sel = this.dom.periodSelect;
        if (!sel) return;
        try {
            const periods = await api.get('/admin/periods/history');
            const prev = sel.value;
            sel.innerHTML = '';
            (periods || []).forEach(p => {
                const opt = document.createElement('option');
                opt.value = String(p.id);
                opt.textContent = p.name + (p.is_active ? ' (активный)' : '');
                sel.appendChild(opt);
            });
            const active = (periods || []).find(p => p.is_active);
            sel.value = prev
                || (active ? String(active.id) : (periods && periods[0] ? String(periods[0].id) : ''));
        } catch {
            sel.innerHTML = '<option value="">— активный период —</option>';
        }
    },

    async handleUpload() {
        if (this.state.isUploading) return toast('Импорт уже выполняется', 'info');

        const file209 = this.dom.inputUpload209?.files[0] || null;
        const file205 = this.dom.inputUpload205?.files[0] || null;
        const periodId = this.dom.periodSelect?.value || '';
        const periodLabel = this.dom.periodSelect?.selectedOptions?.[0]?.textContent || 'активный период';
        // Legacy: если только старая разметка (#debtFile1C + radio) — старая логика.
        if (!file209 && !file205) {
            const legacyFile = this.dom.inputUpload?.files[0];
            if (legacyFile) {
                return this._handleLegacyUpload(legacyFile);
            }
            return toast('Выберите хотя бы один файл .xlsx', 'error');
        }

        // Bug T: подсветить дубликаты в confirm-диалоге.
        const dupNotes = [];
        if (file209 && this._lastPreview209?.duplicate_of) {
            const d = this._lastPreview209.duplicate_of;
            dupNotes.push(`⚠ 209-файл уже импортирован: №${d.log_id} (${d.started_at?.split('T')[0] || '—'}, status=${d.status})`);
        }
        if (file205 && this._lastPreview205?.duplicate_of) {
            const d = this._lastPreview205.duplicate_of;
            dupNotes.push(`⚠ 205-файл уже импортирован: №${d.log_id} (${d.started_at?.split('T')[0] || '—'}, status=${d.status})`);
        }

        const summary = [
            `Период: ${periodLabel}`,
            file209 ? `209: ${file209.name}${this._lastPreview209 ? ` · ФИО найдено: ${this._lastPreview209.rows_with_fio}` : ''}` : null,
            file205 ? `205: ${file205.name}${this._lastPreview205 ? ` · ФИО найдено: ${this._lastPreview205.rows_with_fio}` : ''}` : null,
            ...dupNotes,
        ].filter(Boolean).join('\n');
        const confirmMsg = dupNotes.length
            ? `Загрузить файлы?\n${summary}\n\nЭти файлы уже загружались. Точно повторить?`
            : `Загрузить файлы?\n${summary}`;
        if (!await showConfirm(confirmMsg, { title: 'Загрузка файлов', confirmText: 'Загрузить' })) return;

        this.state.isUploading = true;
        if (this.dom.uploadResult) {
            this.dom.uploadResult.style.display = 'none';
            this.dom.uploadResult.innerHTML = '';
        }
        setLoading(this.dom.btnUpload, true, 'Загрузка...');

        const formData = new FormData();
        if (file209) formData.append('file_209', file209);
        if (file205) formData.append('file_205', file205);
        if (periodId) formData.append('period_id', periodId);

        try {
            const res = await api.post('/financier/import-debts-pair', formData);
            // Очищаем inputs чтобы случайно не нажать «загрузить» ещё раз.
            if (this.dom.inputUpload209) this.dom.inputUpload209.value = '';
            if (this.dom.inputUpload205) this.dom.inputUpload205.value = '';
            // Чистим preview-блоки.
            document.getElementById('debtPreview209')?.remove();
            document.getElementById('debtPreview205')?.remove();
            this._lastPreview209 = null;
            this._lastPreview205 = null;

            toast(`Файлы приняты (${res.tasks?.length || 0}). Обработка…`, 'info');
            // Polling по последнему таску — обычно у нас 1-2 и они идут
            // параллельно, общая длительность определяется самым медленным.
            // Для простоты UI ждём один из тасков; loadImportHistory всё равно
            // покажет обе записи в любом случае.
            const lastTask = res.tasks?.[res.tasks.length - 1];
            if (lastTask?.task_id) {
                this.pollTask(lastTask.task_id);
            } else {
                this.state.isUploading = false;
                setLoading(this.dom.btnUpload, false, '⬆ Загрузить выбранные');
                this.loadImportHistory();
            }
        } catch (e) {
            toast(`Ошибка: ${e.message}`, 'error');
            this.state.isUploading = false;
            setLoading(this.dom.btnUpload, false, '⬆ Загрузить выбранные');
        }
    },

    async _handleLegacyUpload(file) {
        // Старая разметка (только #debtFile1C + radio) — отдельный код-пас
        // для обратной совместимости. После полного удаления tab_debts.html
        // v1 этот метод можно убрать.
        const radio = document.querySelector('input[name="accountType"]:checked');
        const accountType = radio?.value || '209';
        if (!await showConfirm(`Загрузить долги для счёта ${accountType}?`, { title: 'Загрузка', confirmText: 'Загрузить' })) return;

        this.state.isUploading = true;
        if (this.dom.uploadResult) {
            this.dom.uploadResult.style.display = 'none';
            this.dom.uploadResult.innerHTML = '';
        }
        setLoading(this.dom.btnUpload, true, 'Загрузка...');

        const formData = new FormData();
        formData.append('file', file);
        formData.append('account_type', accountType);
        const legacyPeriod = this.dom.periodSelect?.value || '';
        if (legacyPeriod) formData.append('period_id', legacyPeriod);

        try {
            const res = await api.post('/financier/import-debts', formData);
            this.dom.inputUpload.value = '';
            toast(`Файл принят (Счёт ${accountType}). Обработка…`, 'info');
            this.pollTask(res.task_id);
        } catch (e) {
            toast(`Ошибка: ${e.message}`, 'error');
            this.state.isUploading = false;
            setLoading(this.dom.btnUpload, false, '⬆ Загрузить выбранные');
        }
    },

    async pollTask(taskId) {
        this.clearPoll();
        this.state.currentPollId = taskId;
        let attempts = 0;
        const maxAttempts = 150;

        const check = async () => {
            if (this.state.currentPollId !== taskId) return;
            attempts++;
            if (attempts > maxAttempts) {
                toast('Превышено время ожидания сервера.', 'warning');
                this.state.isUploading = false;
                setLoading(this.dom.btnUpload, false, '⬆ Загрузить выбранные');
                return;
            }
            try {
                const res = await api.get(`/admin/tasks/${taskId}`);
                if (this.state.currentPollId !== taskId) return;

                // Celery task проходит состояния:
                //   PENDING (в очереди) → STARTED (worker взял) → RETRY (autoretry)
                //   → SUCCESS | FAILURE
                // Раньше STARTED/RETRY валились в «Неизвестный статус» —
                // считаем их как «продолжаем polling».
                const inProgress = ['PENDING', 'STARTED', 'RETRY', 'RECEIVED'];
                if (inProgress.includes(res.state) || res.status === 'processing') {
                    this.state.pollTimer = setTimeout(check, 2000);
                    return;
                }
                if (res.status === 'done' || res.state === 'SUCCESS') {
                    this.renderUploadResult(res.result || res);
                    toast('Импорт завершён!', 'success');
                    this.reload();
                    this.loadImportHistory();
                    this.state.isUploading = false;
                    setLoading(this.dom.btnUpload, false, '⬆ Загрузить выбранные');
                    return;
                }
                if (res.state === 'FAILURE' || res.state === 'REVOKED') {
                    throw new Error(res.error || 'Ошибка воркера');
                }
                // На всякий случай: неизвестное состояние — повторяем polling,
                // а не сразу падаем с ошибкой. maxAttempts ограничит сверху.
                this.state.pollTimer = setTimeout(check, 3000);
            } catch (e) {
                if (this.state.currentPollId !== taskId) return;
                toast('Ошибка задачи: ' + e.message, 'error');
                if (this.dom.uploadResult) {
                    this.dom.uploadResult.style.display = 'block';
                    this.dom.uploadResult.innerHTML = '';
                    this.dom.uploadResult.appendChild(
                        el('div', { style: { color: 'red' } }, `Сбой: ${e.message}`)
                    );
                }
                this.state.isUploading = false;
                setLoading(this.dom.btnUpload, false, '⬆ Загрузить выбранные');
            }
        };
        check();
    },

    renderUploadResult(res) {
        if (!this.dom.uploadResult || !res) return;
        this.dom.uploadResult.innerHTML = '';
        this.dom.uploadResult.style.display = 'block';

        const success = el('div', {
            style: { padding: '15px', background: '#e8f5e9', color: '#2e7d32', borderRadius: '6px', border: '1px solid #c8e6c9' }
        },
            el('h4', { style: { margin: '0 0 10px 0' } }, `✅ Импорт завершён (Счёт ${res.account || '?'})`),
            el('ul', { style: { margin: 0, paddingLeft: '20px' } },
                el('li', {}, 'Обработано: ', el('strong', {}, String(res.processed))),
                el('li', {}, 'Обновлено: ', el('strong', {}, String(res.updated))),
                el('li', {}, 'Создано: ', el('strong', {}, String(res.created))),
                res.log_id ? el('li', {}, 'Запись в истории: ', el('strong', {}, `#${res.log_id}`)) : ''
            )
        );
        this.dom.uploadResult.appendChild(success);

        if (res.not_found_users && res.not_found_users.length) {
            const errorBox = el('div', {
                style: { marginTop: '15px', padding: '15px', background: '#ffebee', color: '#c62828', borderRadius: '6px', border: '1px solid #ffcdd2' }
            },
                el('h4', { style: { margin: '0 0 10px 0' } }, `⚠️ Не найдены (${res.not_found_users.length})`)
            );
            const scrollBox = el('div', {
                style: { maxHeight: '100px', overflow: 'auto', fontSize: '13px', background: 'rgba(255,255,255,.5)', padding: '5px' }
            });
            res.not_found_users.forEach(u => scrollBox.appendChild(el('div', {}, String(u))));
            errorBox.appendChild(scrollBox);
            if (res.log_id) {
                errorBox.appendChild(el('button', {
                    class: 'action-btn secondary-btn',
                    style: { marginTop: '10px', fontSize: '12px' },
                    onclick: () => this.openNotFoundModal(res.log_id)
                }, 'Привязать вручную'));
            }
            this.dom.uploadResult.appendChild(errorBox);
        }
    },

    // ==========================================================================
    // ТАБЛИЦА
    // ==========================================================================
    async loadUsers() {
        if (!this.dom.tableBody) return;
        if (this.state.mode === 'rooms') return this.loadRooms();
        const requestId = ++this.state.lastRequestId;
        this.dom.tableBody.innerHTML = '<tr><td colspan="10" style="text-align:center;padding:20px">Загрузка…</td></tr>';

        const params = new URLSearchParams({
            page: this.state.page,
            limit: this.state.limit,
            sort_by: this.state.sortBy,
            sort_dir: this.state.sortDir,
        });
        if (this.state.search) params.set('search', this.state.search);
        if (this.state.filterType === 'debtors') params.set('only_debtors', 'true');
        if (this.state.filterType === 'overpaid') params.set('only_overpaid', 'true');
        if (this.state.dormitory) params.set('dormitory', this.state.dormitory);
        if (this.state.minDebt) params.set('min_debt', this.state.minDebt);
        if (this.state.hideEmpty) params.set('has_data', 'true');
        if (this.state.viewPeriodId) params.set('period_id', this.state.viewPeriodId);

        try {
            const data = await api.get(`/financier/users-status?${params}`);
            if (requestId !== this.state.lastRequestId) return;
            this.state.total = data.total;
            this.renderUsers(data.items);
            this.updatePagination();
        } catch (e) {
            if (requestId !== this.state.lastRequestId) return;
            this.dom.tableBody.innerHTML = '';
            this.dom.tableBody.appendChild(
                el('tr', {}, el('td', { colspan: '10', style: { color: 'red', textAlign: 'center', padding: '20px' } }, e.message))
            );
        }
    },

    // ── РЕЖИМ КВАРТИР: зеркало loadUsers, агрегация по помещению, без ФИО ──
    async loadRooms() {
        const requestId = ++this.state.lastRequestId;
        this.dom.tableBody.innerHTML = '<tr><td colspan="10" style="text-align:center;padding:20px">Загрузка…</td></tr>';
        const sortBy = ['debt', 'overpay', 'total'].includes(this.state.sortBy)
            ? this.state.sortBy : 'room';
        const params = new URLSearchParams({
            page: this.state.page, limit: this.state.limit,
            sort_by: sortBy, sort_dir: this.state.sortDir,
        });
        if (this.state.search) params.set('search', this.state.search);
        if (this.state.filterType === 'debtors') params.set('only_debtors', 'true');
        if (this.state.filterType === 'overpaid') params.set('only_overpaid', 'true');
        if (this.state.dormitory) params.set('dormitory', this.state.dormitory);
        if (this.state.minDebt) params.set('min_debt', this.state.minDebt);
        if (this.state.hideEmpty) params.set('has_data', 'true');
        if (this.state.viewPeriodId) params.set('period_id', this.state.viewPeriodId);
        try {
            const data = await api.get(`/financier/rooms-status?${params}`);
            if (requestId !== this.state.lastRequestId) return;
            this.state.total = data.total;
            this.renderRooms(data.items);
            this.updatePagination();
        } catch (e) {
            if (requestId !== this.state.lastRequestId) return;
            this.dom.tableBody.innerHTML =
                `<tr><td colspan="10" style="color:red;text-align:center;padding:20px">${e.message}</td></tr>`;
        }
    },

    renderRooms(rooms) {
        if (!rooms || !rooms.length) {
            this.dom.tableBody.innerHTML = '<tr><td colspan="10" style="text-align:center;padding:20px; color:var(--text-secondary);">Нет данных для текущих фильтров</td></tr>';
            return;
        }
        const f = (v) => {
            const a = Math.abs(Number(v || 0));
            if (a < 0.005) return '0';
            return (a >= 10000 ? a.toFixed(0) : a.toFixed(2)).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
        };
        const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
            (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
        this.dom.tableBody.innerHTML = rooms.map((r) => {
            const d209 = Number(r.debt_209 || 0), o209 = Number(r.overpayment_209 || 0);
            const d205 = Number(r.debt_205 || 0), o205 = Number(r.overpayment_205 || 0);
            const totalDebt = d209 + d205, total = Number(r.current_total_cost || 0);
            const bg = totalDebt >= 10000 ? 'background:#fef2f2;'
                : totalDebt >= 1000 ? 'background:#fffbeb;'
                : (o209 + o205) > 0 ? 'background:#f0fdf4;' : '';
            return `<tr style="${bg}">
                <td style="color:var(--text-secondary);">#${r.room_id}</td>
                <td colspan="2"><b>${esc(r.address)}</b> <span style="color:var(--text-secondary);font-size:12px;">· 👤 ${r.residents_count}</span></td>
                <td style="text-align:right; color:#991b1b;">${d209 > 0 ? f(d209) : '—'}</td>
                <td style="text-align:right; color:#15803d;">${o209 > 0 ? f(o209) : '—'}</td>
                <td style="text-align:right; color:#d97706;">${d205 > 0 ? f(d205) : '—'}</td>
                <td style="text-align:right; color:#15803d;">${o205 > 0 ? f(o205) : '—'}</td>
                <td style="text-align:right; font-weight:700;">${totalDebt > 0 ? f(totalDebt) : '—'}</td>
                <td style="text-align:right;">${f(total)}</td>
                <td style="text-align:right;"><button class="icon-btn" data-room-residents="${r.room_id}" title="Кто живёт в квартире"><i class="fa-solid fa-users"></i></button></td>
            </tr>`;
        }).join('');
        this.dom.tableBody.querySelectorAll('[data-room-residents]').forEach((btn) => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.showRoomResidents(Number(btn.dataset.roomResidents));
            });
        });
    },

    async showRoomResidents(roomId) {
        const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
            (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
        const f = (v) => Number(v || 0).toFixed(2);
        try {
            const data = await api.get(`/financier/rooms/${roomId}/residents-finance`);
            const rows = (data.residents || []).map((p) => `<tr style="border-bottom:1px solid var(--border-color);">
                <td style="padding:8px 10px;">${esc(p.full_name || p.username)}</td>
                <td style="padding:8px 10px;text-align:right;color:#991b1b;">${f(Number(p.debt_209) + Number(p.debt_205))} ₽</td>
                <td style="padding:8px 10px;text-align:right;color:#15803d;">${f(Number(p.overpayment_209) + Number(p.overpayment_205))} ₽</td>
                <td style="padding:8px 10px;text-align:right;">${f(p.current_total_cost)} ₽</td>
            </tr>`).join('') || '<tr><td colspan="4" style="padding:16px;text-align:center;color:var(--text-secondary);">Нет жильцов</td></tr>';
            const overlay = document.createElement('div');
            overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:3000;display:flex;align-items:center;justify-content:center;padding:20px;';
            overlay.innerHTML = `<div style="background:var(--bg-card,#fff);border-radius:12px;max-width:560px;width:100%;max-height:82vh;overflow:auto;box-shadow:0 20px 60px rgba(0,0,0,0.3);">
                <div style="display:flex;justify-content:space-between;align-items:center;padding:14px 16px;border-bottom:1px solid var(--border-color);"><b>Жильцы квартиры · долги</b><button data-rr-close style="background:none;border:none;font-size:22px;line-height:1;cursor:pointer;">×</button></div>
                <table style="width:100%;border-collapse:collapse;font-size:13px;"><thead><tr style="background:var(--bg-page);font-size:11px;color:var(--text-secondary);text-transform:uppercase;"><th style="text-align:left;padding:8px 10px;">Жилец</th><th style="text-align:right;padding:8px 10px;">Долг</th><th style="text-align:right;padding:8px 10px;">Переплата</th><th style="text-align:right;padding:8px 10px;">Итог</th></tr></thead><tbody>${rows}</tbody></table></div>`;
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay || e.target.closest('[data-rr-close]')) overlay.remove();
            });
            document.body.appendChild(overlay);
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },

    setMode(mode) {
        if (this.state.mode === mode) return;
        this.state.mode = mode;
        this.state.page = 1;
        const t = document.getElementById('debtsTitle');
        if (t) t.textContent = mode === 'rooms' ? 'Долги по квартирам' : 'Список жильцов и долгов';
        const bu = document.getElementById('debtsModeUsers');
        const br = document.getElementById('debtsModeRooms');
        const sty = 'border-radius:0; padding:5px 12px; font-size:12px;';
        if (bu) { bu.className = 'action-btn ' + (mode === 'users' ? 'primary-btn' : 'secondary-btn'); bu.style.cssText = sty; }
        if (br) { br.className = 'action-btn ' + (mode === 'rooms' ? 'primary-btn' : 'secondary-btn'); br.style.cssText = sty; }
        this.loadUsers();
        this.loadStats();  // KPI-плашка зависит от режима (квартир с долгом / должников)
    },

    updatePagination() {
        if (!this.dom.pageInfo) return;
        const totalPages = Math.ceil(this.state.total / this.state.limit) || 1;
        this.dom.pageInfo.textContent = `Стр. ${this.state.page} из ${totalPages} (Всего: ${this.state.total})`;
        this.dom.btnPrev.disabled = this.state.page <= 1;
        this.dom.btnNext.disabled = this.state.page >= totalPages;
    },

    debtChip(amount) {
        const a = Number(amount || 0);
        if (a <= 0) return '';
        if (a >= 10000) return `<span style="background:#fee2e2; color:#991b1b; padding:1px 6px; border-radius:8px; font-size:10px; font-weight:700; margin-left:4px;">КРИТ.</span>`;
        if (a >= 1000) return `<span style="background:#fef3c7; color:#92400e; padding:1px 6px; border-radius:8px; font-size:10px; font-weight:600; margin-left:4px;">средн.</span>`;
        return '';
    },

    renderUsers(users) {
        this.dom.tableBody.innerHTML = '';
        if (!users || !users.length) {
            this.dom.tableBody.innerHTML = '<tr><td colspan="10" style="text-align:center;padding:20px; color:var(--text-secondary);">Нет данных для текущих фильтров</td></tr>';
            return;
        }

        const fragment = document.createDocumentFragment();
        users.forEach(u => {
            const d209 = parseFloat(u.debt_209 || 0), o209 = parseFloat(u.overpayment_209 || 0);
            const d205 = parseFloat(u.debt_205 || 0), o205 = parseFloat(u.overpayment_205 || 0);
            // Bug V: обороты для индикатора движения.
            const od209 = parseFloat(u.obor_debit_209 || 0), oc209 = parseFloat(u.obor_credit_209 || 0);
            const od205 = parseFloat(u.obor_debit_205 || 0), oc205 = parseFloat(u.obor_credit_205 || 0);
            const totalDebt = d209 + d205;
            const total = parseFloat(u.current_total_cost || 0);

            // Цветовой индикатор строки
            let rowBg = '';
            if (totalDebt >= 10000) rowBg = 'background:#fef2f2;';
            else if (totalDebt >= 1000) rowBg = 'background:#fffbeb;';
            else if ((o209 + o205) > 0) rowBg = 'background:#f0fdf4;';

            const room = u.room ? `${u.room.dormitory_name || '—'} / ${u.room.room_number || '—'}` : '—';

            // Bug AH: ячейка сальдо с inline-микроисторией движения средств.
            // Раньше показывали только текущее значение + tooltip — админу
            // приходилось вешать курсор, чтобы понять, что произошло.
            // Теперь под главным числом — компактная строчка вида
            // «был 635 · оплатил 635» с цветовым кодированием.
            //
            // Аргументы:
            //   value  — текущее сальдо в этой колонке (debt или overpay)
            //   oborD  — оборот Дт (доначислили) за период
            //   oborC  — оборот Кр (заплатили) за период
            //   isDebt — true для колонки «Долг», false для «Перепл.»
            //   accColor — цвет основного числа (красный/оранжевый для долгов,
            //              зелёный для переплат)
            //
            // Логика начального сальдо (обратное вычисление):
            //   Если у жильца сейчас долг X и были обороты Дт/Кр —
            //   start_debt = X + oborC - oborD
            //   (заплатил и стало X, значит до этого было X + заплатил − начислили)
            //   Если start_debt < 0 — было не долг, а переплата.
            const saldoCell = (value, oborD, oborC, isDebt, accColor) => {
                const hasValue = value > 0.005;
                const hasObor = oborD > 0.005 || oborC > 0.005;

                // Совсем пусто — без движения и без сальдо.
                if (!hasValue && !hasObor) {
                    return `<span style="color:#ccc;">—</span>`;
                }

                // Helper: компактное форматирование суммы без «₽» и без копеек,
                // если они .00 — экономим место.
                const f = (v) => {
                    const abs = Math.abs(v);
                    if (abs < 0.005) return '0';
                    // Тысячи разделяем тонким пробелом.
                    const fixed = abs >= 10000 ? abs.toFixed(0) : abs.toFixed(2);
                    return fixed.replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
                };

                // Вычисляем «было в начале периода» по обратной формуле.
                // Только для «Долг» — для «Перепл.» это симметрично, но мы
                // отдадим расшифровку колонке Долг (чтобы не дублировать).
                let startDebt = null;
                if (isDebt && hasObor) {
                    // value уже >0 ИЛИ был долг и сейчас 0 — обе ветки покрыты.
                    startDebt = value + oborC - oborD;
                }

                // Случай 1: переплата (isDebt=false), просто показываем число.
                // Движение здесь не описываем — оно в соседней колонке Долг.
                if (!isDebt) {
                    if (!hasValue) return `<span style="color:#ccc;">—</span>`;
                    return `<div style="line-height:1.25;">
                        <div style="font-weight:600; color:${accColor};">${f(value)}</div>
                        <div style="font-size:10.5px; color:#16a34a;">переплата</div>
                    </div>`;
                }

                // ── Колонка «Долг» — раскладываем движение в одну строчку.

                // 1. Долг 0, есть обороты — погашен (или начислили + сразу оплатили)
                if (!hasValue && hasObor) {
                    if (oborC > 0.005 && oborD < 0.005) {
                        // Был долг — оплатили полностью.
                        return `<div style="line-height:1.25;">
                            <div style="font-weight:600; color:#15803d;">0 <span style="font-size:11px;">✓</span></div>
                            <div style="font-size:10.5px; color:#6b7280;">был ${f(startDebt)} · оплатил ${f(oborC)}</div>
                        </div>`;
                    }
                    if (oborD > 0.005 && oborC > 0.005) {
                        // Начислили и оплатили — нулевое сальдо в результате.
                        return `<div style="line-height:1.25;">
                            <div style="font-weight:600; color:#15803d;">0 <span style="font-size:11px;">⊜</span></div>
                            <div style="font-size:10.5px; color:#6b7280;">+${f(oborD)} начисл · −${f(oborC)} оплат</div>
                        </div>`;
                    }
                    // Только начисление 0→0 — экзотика, fallback.
                    return `<div style="line-height:1.25;">
                        <div style="font-weight:600; color:#9ca3af;">0</div>
                        <div style="font-size:10.5px; color:#6b7280;">+${f(oborD)} начислено</div>
                    </div>`;
                }

                // 2. Долг есть + обороты — раскрываем движение.
                if (hasValue && hasObor) {
                    // Долг вырос (доначислили больше, чем оплатили).
                    if (value > (startDebt || 0) + 0.005) {
                        return `<div style="line-height:1.25;">
                            <div style="font-weight:600; color:#b91c1c;">${f(value)}</div>
                            <div style="font-size:10.5px; color:#b91c1c;">был ${f(startDebt)} · +${f(oborD - oborC)} ↑</div>
                        </div>`;
                    }
                    // Долг уменьшился — заплатил часть.
                    if ((startDebt || 0) > value + 0.005) {
                        return `<div style="line-height:1.25;">
                            <div style="font-weight:600; color:#a16207;">${f(value)}</div>
                            <div style="font-size:10.5px; color:#a16207;">был ${f(startDebt)} · оплатил ${f(oborC)}</div>
                        </div>`;
                    }
                    // Без изменения, но обороты были (начислили = оплатил).
                    return `<div style="line-height:1.25;">
                        <div style="font-weight:600; color:${accColor};">${f(value)}</div>
                        <div style="font-size:10.5px; color:#6b7280;">+${f(oborD)} · −${f(oborC)}</div>
                    </div>`;
                }

                // 3. Долг есть, оборотов нет — статичный долг (висит).
                return `<div style="line-height:1.25;">
                    <div style="font-weight:600; color:${accColor};">${f(value)}</div>
                    <div style="font-size:10.5px; color:#9ca3af;">без движения</div>
                </div>`;
            };

            const tr = el('tr', { class: 'table-row', style: { cssText: rowBg } },
                el('td', {}, String(u.id)),
                // Bug AI: ФИО кликабельно — открывает модалку «карточка жильца»
                // с разбором было/оплатил/осталось по каждому счёту + история.
                el('td', {
                    style: { fontWeight: '600', cursor: 'pointer', color: '#4338ca' },
                    title: 'Открыть карточку жильца с раскладкой долга',
                    onclick: () => this.openUserCard(u),
                }, u.username),
                el('td', { style: { fontSize: '12px' } }, room),
            );

            // «Не найден в счёте»: ФИО жильца не было в последнем импорте этого
            // счёта за период (seen_2xx === false). Отличаем от «долг 0».
            const notFoundCell = (acct) =>
                `<span style="color:#b45309; font-size:11px; font-style:italic;" title="ФИО не найдено в последнем импорте счёта ${acct} за этот период — данных по счёту нет (это не «долг 0»)">не найден</span>`;

            // Долг/Перепл 209 с движением
            const d209Td = el('td', { style: { borderLeft: '2px solid #eee' } });
            d209Td.innerHTML = (u.seen_209 === false && d209 < 0.005)
                ? notFoundCell('209')
                : saldoCell(d209, od209, oc209, true, '#c0392b');
            tr.appendChild(d209Td);

            const o209Td = el('td', {});
            o209Td.innerHTML = saldoCell(o209, od209, oc209, false, '#27ae60');
            tr.appendChild(o209Td);

            // Долг/Перепл 205 с движением
            const d205Td = el('td', { style: { borderLeft: '2px solid #eee' } });
            d205Td.innerHTML = (u.seen_205 === false && d205 < 0.005)
                ? notFoundCell('205')
                : saldoCell(d205, od205, oc205, true, '#d35400');
            tr.appendChild(d205Td);

            const o205Td = el('td', {});
            o205Td.innerHTML = saldoCell(o205, od205, oc205, false, '#27ae60');
            tr.appendChild(o205Td);

            // Суммарный долг + чип
            const sumCell = el('td', { style: { fontWeight: '700', color: totalDebt > 0 ? '#b91c1c' : 'var(--text-secondary)' } });
            sumCell.innerHTML = totalDebt > 0 ? `${fmtMoney(totalDebt)}${this.debtChip(totalDebt)}` : '—';
            tr.appendChild(sumCell);

            tr.appendChild(el('td', { style: { fontWeight: 'bold' } }, total !== 0 ? fmtMoney(total) : '—'));
            // Группа кнопок: «История» — модалка sparkline через все импорты;
            // «Корр.» — ручная корректировка сальдо. Раньше была только Корр.
            const actionsCell = el('td', { style: { textAlign: 'right', whiteSpace: 'nowrap' } });
            actionsCell.appendChild(el('button', {
                class: 'action-btn', style: { padding: '4px 8px', fontSize: '12px', background: '#fff', color: '#4338ca', border: '1px solid #c7d2fe', marginRight: '4px' },
                title: 'История долгов через все импорты 1С',
                onclick: () => this.openUserDebtHistory(u.id, u.username),
            }, '📊'));
            // 🔍 — поиск ФИО в архивах последних импортов 1С. Use case:
            // у жильца «—» в обоих счетах, а в Excel он должен быть. Эта
            // кнопка показывает где он есть/нет в архивах + значения.
            actionsCell.appendChild(el('button', {
                class: 'action-btn', style: { padding: '4px 8px', fontSize: '12px', background: '#fff', color: '#0ea5e9', border: '1px solid #bae6fd', marginRight: '4px' },
                title: 'Найти ФИО в архивах последних импортов 1С (диагностика «почему нет долга»)',
                onclick: () => this.openCheckCoverage(u.id, u.username),
            }, '🔍'));
            // Кнопка «Сбросить баланс» — обнуляет debt/overpay у всех reading
            // жильца. Полезно когда после отката импорта у жильца остались
            // зависшие сальдо в других периодах.
            actionsCell.appendChild(el('button', {
                class: 'action-btn', style: { padding: '4px 8px', fontSize: '12px', background: '#fff', color: '#b91c1c', border: '1px solid #fecaca', marginRight: '4px' },
                title: 'Сбросить баланс жильца — обнулить debt/overpay во ВСЕХ reading-ах. Использовать когда после отката импорта остались зависшие сальдо.',
                onclick: () => this.resetUserBalance(u.id, u.username),
            }, '🧹'));
            actionsCell.appendChild(el('button', {
                class: 'action-btn', style: { padding: '4px 10px', fontSize: '12px', background: '#6366f1', color: '#fff' },
                onclick: () => this.openAdjustModal(u.id, u.username),
            }, 'Корр.'));
            tr.appendChild(actionsCell);
            fragment.appendChild(tr);
        });
        this.dom.tableBody.appendChild(fragment);
    },

    // ==========================================================================
    // МОДАЛКА КОРРЕКТИРОВКИ (замена prompt-цепочки)
    // ==========================================================================
    openAdjustModal(userId, username) {
        if (!this.dom.adjustModal) return;
        this.dom.adjustForm.reset();
        this.dom.adjustUserId.value = String(userId);
        this.dom.adjustUserName.textContent = username;
        this.dom.adjustAccount.value = '209';
        this.dom.adjustModal.classList.add('open');
        setTimeout(() => this.dom.adjustAmount?.focus(), 50);
    },

    closeAdjustModal() {
        this.dom.adjustModal?.classList.remove('open');
    },

    async submitAdjust(e) {
        e.preventDefault();
        const payload = {
            user_id: Number(this.dom.adjustUserId.value),
            amount: parseFloat(this.dom.adjustAmount.value),
            description: (this.dom.adjustDescription.value || '').trim(),
            account_type: this.dom.adjustAccount.value,
        };
        if (isNaN(payload.amount)) return toast('Введите число', 'error');
        if (!payload.description) return toast('Укажите причину', 'error');

        const btn = this.dom.adjustForm.querySelector('button[type="submit"]');
        setLoading(btn, true, 'Сохранение...');
        try {
            await api.post('/admin/adjustments', payload);
            toast('Корректировка сохранена', 'success');
            this.closeAdjustModal();
            this.reload();
        } catch (err) {
            toast(err.message, 'error');
        } finally {
            setLoading(btn, false, '<i class="fa-solid fa-floppy-disk"></i> Сохранить корректировку');
        }
    },

    // ==========================================================================
    // ЭКСПОРТ
    // ==========================================================================
    async exportExcel() {
        const params = new URLSearchParams();
        if (this.state.search) params.set('search', this.state.search);
        if (this.state.filterType === 'debtors') params.set('only_debtors', 'true');
        if (this.state.filterType === 'overpaid') params.set('only_overpaid', 'true');
        if (this.state.dormitory) params.set('dormitory', this.state.dormitory);
        if (this.state.minDebt) params.set('min_debt', this.state.minDebt);
        setLoading(this.dom.btnExport, true);
        try {
            await api.download(`/financier/debts/export?${params}`, `debts_${Date.now()}.xlsx`);
            toast('Экспорт готов', 'success');
        } catch (e) {
            toast('Ошибка экспорта: ' + e.message, 'error');
        } finally {
            setLoading(this.dom.btnExport, false);
        }
    },

    // ==========================================================================
    // ИСТОРИЯ ИМПОРТОВ
    // ==========================================================================
    async loadImportHistory() {
        if (!this.dom.importHistoryList) return;
        this.dom.importHistoryList.innerHTML = '<div style="padding:20px; text-align:center; color:var(--text-secondary);"><i class="fa-solid fa-spinner fa-spin"></i> Загрузка…</div>';
        try {
            const logs = await api.get('/financier/debts/import-history?limit=20');
            if (!logs.length) {
                this.dom.importHistoryList.innerHTML = '<div style="padding:20px; text-align:center; color:var(--text-secondary); font-size:13px;">Ещё не было импортов.</div>';
                return;
            }
            // Шапка с кнопкой массовой чистки + список.
            const reverted = logs.filter(l => l.status === 'reverted' || l.status === 'failed').length;
            const headerBar = `
                <div style="display:flex; justify-content:space-between; align-items:center; padding:8px 12px; background:var(--bg-page); border-bottom:1px solid var(--border-color); font-size:12px;">
                    <span style="color:var(--text-secondary);">
                        Записей: <b>${logs.length}</b>${reverted > 0 ? ` · откаченных: <b style="color:#dc2626;">${reverted}</b>` : ''}
                    </span>
                    <button class="action-btn" data-history-action="cleanup"
                            style="padding:4px 10px; font-size:11px; background:#fef3c7; color:#92400e; border:1px solid #fde68a;"
                            title="Удалить откаченные и устаревшие записи истории (оставить 5 последних completed на каждый счёт)">
                        <i class="fa-solid fa-broom"></i> Очистить устаревшие
                    </button>
                </div>`;
            this.dom.importHistoryList.innerHTML = headerBar + logs.map(log => this.renderHistoryRow(log)).join('');
        } catch (e) {
            this.dom.importHistoryList.innerHTML = `<div style="padding:16px; color:var(--danger-color);">Ошибка: ${esc(e.message)}</div>`;
        }
    },

    renderHistoryRow(log) {
        const statusColors = {
            pending:   ['#3b82f6', '#dbeafe', 'В процессе'],
            completed: ['#059669', '#d1fae5', 'Готово'],
            failed:    ['#dc2626', '#fee2e2', 'Ошибка'],
            reverted:  ['#6b7280', '#f3f4f6', 'Откачен'],
        };
        const [fg, bg, label] = statusColors[log.status] || ['#6b7280', '#f3f4f6', log.status];
        const canUndo = log.status === 'completed';
        const hasNotFound = log.not_found_count > 0;

        return `
            <div style="padding:10px 12px; border-bottom:1px solid var(--border-color); display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
                <span style="background:${bg}; color:${fg}; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600; white-space:nowrap;">${esc(label)}</span>
                <span style="font-weight:600; color:#1f2937; font-size:13px; white-space:nowrap;">№${log.id} · ${esc(log.account_type)}</span>
                <span style="color:var(--text-secondary); font-size:12px; flex:1; min-width:140px;">${esc(fmtDateTime(log.started_at))} · ${esc(log.started_by || '—')}</span>
                <span style="font-size:12px; color:var(--text-secondary); white-space:nowrap;" title="Обработано / Обновлено / Создано">
                    📊 ${log.processed} / ✎ ${log.updated} / +${log.created}
                </span>
                ${hasNotFound ? `
                    <button class="action-btn secondary-btn" data-history-action="view-not-found" data-log-id="${log.id}"
                            style="padding:3px 8px; font-size:11px; background:#fef3c7; color:#92400e; border-color:#fde68a; white-space:nowrap;">
                        ⚠ ${log.not_found_count}
                    </button>` : ''}
                ${log.has_archive ? `
                    <a href="/api/financier/debts/import-history/${log.id}/download"
                       class="action-btn secondary-btn" download
                       title="Скачать оригинальный xlsx из 1С"
                       style="padding:3px 8px; font-size:11px; white-space:nowrap; text-decoration:none;">
                        <i class="fa-solid fa-download"></i>
                    </a>` : ''}
                ${log.status === 'completed' ? `
                    <button class="action-btn secondary-btn" data-history-action="diff" data-log-id="${log.id}"
                            title="Сравнить с предыдущим импортом того же счёта"
                            style="padding:3px 8px; font-size:11px; white-space:nowrap; background:#eef2ff; color:#4338ca; border-color:#c7d2fe;">
                        <i class="fa-solid fa-code-compare"></i> Diff
                    </button>
                    <button class="action-btn secondary-btn" data-history-action="diagnose" data-log-id="${log.id}"
                            title="Диагностика парсера: какие колонки нашёл, какие значения извлёк (для отладки «почему долг неправильный»)"
                            style="padding:3px 8px; font-size:11px; white-space:nowrap; background:#fffbeb; color:#92400e; border-color:#fde68a;">
                        <i class="fa-solid fa-microscope"></i>
                    </button>
                    ${log.has_archive ? `
                        <button class="action-btn secondary-btn" data-history-action="reparse" data-log-id="${log.id}"
                                title="Bug AE: переимпорт того же файла с актуальной логикой парсера. Полезно если reading'и созданы старой версией (debt берёт начальное сальдо вместо погашенного оборотами)."
                                style="padding:3px 8px; font-size:11px; white-space:nowrap; background:#ecfdf5; color:#065f46; border-color:#a7f3d0;">
                            <i class="fa-solid fa-arrows-rotate"></i> Переимпорт
                        </button>` : ''}` : ''}
                ${canUndo ? `
                    <button class="action-btn danger-btn" data-history-action="undo" data-log-id="${log.id}"
                            style="padding:3px 8px; font-size:11px; white-space:nowrap;">
                        <i class="fa-solid fa-rotate-left"></i> Откатить
                    </button>` : ''}
                <button class="action-btn" data-history-action="delete" data-log-id="${log.id}"
                        style="padding:3px 8px; font-size:11px; background:#f3f4f6; color:#6b7280; border:1px solid #d1d5db; white-space:nowrap;"
                        title="Удалить запись истории (без отката, если данные уже неактуальны)">
                    <i class="fa-regular fa-trash-can"></i>
                </button>
                ${log.error ? `<div style="width:100%; font-size:11px; color:#b91c1c; margin-top:4px;">${esc(log.error)}</div>` : ''}
            </div>
        `;
    },

    async undoImport(logId) {
        if (!await showConfirm(`Откатить импорт №${logId}?\nБудут восстановлены долги/переплаты, которые были ДО этого импорта, и удалены созданные им черновики. Действие необратимо.`, { title: 'Откат импорта', confirmText: 'Откатить', danger: true })) return;
        try {
            const res = await api.post(`/financier/debts/import-history/${logId}/undo`);
            toast(`Откачено: восстановлено ${res.restored_readings}, удалено ${res.removed_drafts}`, 'success');
            this.reload();
            this.loadImportHistory();
        } catch (e) {
            toast('Ошибка отката: ' + e.message, 'error');
        }
    },

    /** Bug AE: Переимпорт лога 1С из архива.
     *  Reading'и созданные старой версией парсера (до Bug U-fix6) могут иметь
     *  debt = начальное сальдо вместо погашенного оборотами. Этот endpoint
     *  берёт archive_path и запускает import_debts_task — pipeline UPDATE-ит
     *  существующие reading'и значениями из актуальной логики. */
    async reparseImport(logId) {
        if (!await showConfirm(
            `Переимпортировать импорт №${logId} из архива?\n\n` +
            `• Файл из 1С возьмётся из архивного хранилища\n` +
            `• Парсер применит актуальную логику (с учётом оборотов Дт/Кр)\n` +
            `• Долги жильцов обновятся, погашенные оборотами обнулятся\n` +
            `• Создастся новый лог импорта (старый останется для аудита)\n\n` +
            `Полезно если у жильцов в «Долги 1С» видны старые цифры (Муравьев Павел: 635,92 ₽ долг, хотя по ОСВ — погашено).`,
            { title: 'Переимпорт', confirmText: 'Переимпортировать' }
        )) return;
        try {
            const res = await api.post(`/financier/debts/import-history/${logId}/reparse`);
            toast(
                `Переимпорт запущен (task=${res.task_id?.slice?.(0, 8) || '—'}), счёт ${res.account_type}. ` +
                `Обнови историю через ~10-15 сек.`,
                'success'
            );
            // Через 12 секунд автоматически перезагружаем историю и таблицу
            setTimeout(() => {
                this.loadImportHistory();
                this.reload();
            }, 12000);
        } catch (e) {
            toast('Ошибка переимпорта: ' + (e.message || 'неизвестно'), 'error');
        }
    },

    /** Удаление одной записи истории импорта БЕЗ отката данных.
     *  Use case: импорт устарел (после rebuild/reload-period долги в БД
     *  обновлены другим импортом), запись «висит» с устаревшими цифрами. */
    /** Диагностика парсера: какие колонки нашёл, какие значения извлёк
     *  для sample-жильцов. Помогает понять «почему у Бендаса всё ещё
     *  2385.07» без захода на сервер за логами. */
    /** Этап 2: модалка-анализатор целостности долгов.
     *  Сравнивает applied_state свежих 209/205-импортов с readings БД,
     *  показывает три категории: drift / missing / extra. */
    async openIntegrityModal() {
        const overlay = document.createElement('div');
        overlay.style.cssText = 'position:fixed; inset:0; background:rgba(0,0,0,0.45); z-index:9999; display:flex; align-items:center; justify-content:center; padding:20px;';
        overlay.innerHTML = `
            <div style="background:#fff; border-radius:8px; width:min(1000px, 100%); max-height:90vh; display:flex; flex-direction:column;">
                <div style="display:flex; justify-content:space-between; align-items:center; padding:14px 18px; border-bottom:1px solid var(--border-color);">
                    <h3 style="margin:0; font-size:15px;">
                        🩺 Целостность долгов: applied_state ↔ БД
                    </h3>
                    <button data-close-integrity style="background:none; border:none; font-size:20px; color:#6b7280; cursor:pointer;">×</button>
                </div>
                <div id="integrityContent" style="padding:14px 18px; overflow-y:auto; flex:1;">
                    <p style="color:var(--text-secondary);"><i class="fa-solid fa-spinner fa-spin"></i> Анализ...</p>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);
        const close = () => overlay.remove();
        overlay.querySelector('[data-close-integrity]').addEventListener('click', close);
        overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });

        try {
            const data = await api.get('/financier/debts/integrity-check');
            this._renderIntegrityContent(overlay, data);
        } catch (e) {
            const c = overlay.querySelector('#integrityContent');
            if (c) c.innerHTML = `<p style="color:var(--danger-color);">Ошибка: ${esc(e.message)}</p>`;
        }
    },

    _renderIntegrityContent(overlay, data) {
        const cont = overlay.querySelector('#integrityContent');
        if (!cont) return;
        const s = data.summary || {};
        const f = (v) => Number(v || 0).toFixed(2);

        const allClean = s.drift_count === 0 && s.missing_in_db_count === 0 && s.extra_in_db_count === 0;

        const driftHtml = !s.drift_count ? '' : `
            <div style="display:flex; justify-content:space-between; align-items:center; margin:18px 0 6px;">
                <h4 style="margin:0; color:#a16207;">⚠ Drift (${s.drift_count}) — долг в БД не совпадает со свежим импортом</h4>
                <button data-fix-category="drift"
                        style="padding:6px 12px; background:#a16207; color:#fff; border:none; border-radius:4px; cursor:pointer; font-size:12px; font-weight:500;">
                    <i class="fa-solid fa-wand-magic-sparkles"></i> Исправить все ${s.drift_count}
                </button>
            </div>
            <p style="font-size:11px; color:#6b7280; margin:0 0 6px;">
                Записать «Ожидается» в БД (UPDATE по reading.id). Альтернатива — нажать ↻ Переимпорт на соответствующем логе.
            </p>
            <table style="width:100%; border-collapse:collapse; font-size:11.5px;">
                <thead style="background:#fffbeb;">
                    <tr>
                        <th style="padding:5px 7px; text-align:left;">ФИО</th>
                        <th style="padding:5px 7px; text-align:left;">Комната</th>
                        <th style="padding:5px 7px; text-align:right;">Ожидается 209</th>
                        <th style="padding:5px 7px; text-align:right;">В БД 209</th>
                        <th style="padding:5px 7px; text-align:right;">Ожидается 205</th>
                        <th style="padding:5px 7px; text-align:right;">В БД 205</th>
                        <th style="padding:5px 7px; text-align:right;">Δ</th>
                        <th style="padding:5px 7px;"></th>
                    </tr>
                </thead>
                <tbody>
                ${data.drift.map(d => `
                    <tr>
                        <td style="padding:4px 7px;">${esc(d.username || '—')}</td>
                        <td style="padding:4px 7px; color:#6b7280;">${esc(d.room_label || '—')}</td>
                        <td style="padding:4px 7px; text-align:right;">${f(d.expected.debt_209)}</td>
                        <td style="padding:4px 7px; text-align:right; color:#b91c1c;">${f(d.actual.debt_209)}</td>
                        <td style="padding:4px 7px; text-align:right;">${f(d.expected.debt_205)}</td>
                        <td style="padding:4px 7px; text-align:right; color:#b91c1c;">${f(d.actual.debt_205)}</td>
                        <td style="padding:4px 7px; text-align:right; font-weight:600;">${f(d.max_abs_diff)}</td>
                        <td style="padding:4px 7px;">
                            <button data-fix-user="${d.user_id}" title="Исправить только этого"
                                    style="padding:3px 7px; background:#fff; color:#a16207; border:1px solid #fde68a; border-radius:3px; cursor:pointer; font-size:11px;">
                                🛠
                            </button>
                        </td>
                    </tr>
                `).join('')}
                </tbody>
            </table>`;

        const missingHtml = !s.missing_in_db_count ? '' : `
            <div style="display:flex; justify-content:space-between; align-items:center; margin:18px 0 6px;">
                <h4 style="margin:0; color:#dc2626;">❗ Missing (${s.missing_in_db_count}) — в файле есть, в БД нет reading</h4>
                <button data-fix-category="missing"
                        style="padding:6px 12px; background:#dc2626; color:#fff; border:none; border-radius:4px; cursor:pointer; font-size:12px; font-weight:500;">
                    <i class="fa-solid fa-plus"></i> Создать все ${s.missing_in_db_count}
                </button>
            </div>
            <p style="font-size:11px; color:#6b7280; margin:0 0 6px;">
                INSERT недостающих reading'ов с ожидаемыми значениями из applied_state.
            </p>
            <table style="width:100%; border-collapse:collapse; font-size:11.5px;">
                <thead style="background:#fef2f2;">
                    <tr>
                        <th style="padding:5px 7px; text-align:left;">ФИО</th>
                        <th style="padding:5px 7px; text-align:left;">Комната</th>
                        <th style="padding:5px 7px; text-align:right;">Ожидается 209</th>
                        <th style="padding:5px 7px; text-align:right;">Ожидается 205</th>
                        <th style="padding:5px 7px;"></th>
                    </tr>
                </thead>
                <tbody>
                ${data.missing_in_db.map(m => `
                    <tr>
                        <td style="padding:4px 7px;">${esc(m.username || '—')}</td>
                        <td style="padding:4px 7px; color:#6b7280;">${esc(m.room_label || '—')}</td>
                        <td style="padding:4px 7px; text-align:right;">${f(m.expected.debt_209)}</td>
                        <td style="padding:4px 7px; text-align:right;">${f(m.expected.debt_205)}</td>
                        <td style="padding:4px 7px;">
                            <button data-fix-user="${m.user_id}" title="Создать reading этому"
                                    style="padding:3px 7px; background:#fff; color:#dc2626; border:1px solid #fecaca; border-radius:3px; cursor:pointer; font-size:11px;">
                                🛠
                            </button>
                        </td>
                    </tr>
                `).join('')}
                </tbody>
            </table>`;

        const extraHtml = !s.extra_in_db_count ? '' : `
            <div style="display:flex; justify-content:space-between; align-items:center; margin:18px 0 6px;">
                <h4 style="margin:0; color:#7c3aed;">👻 Extra/Zombie (${s.extra_in_db_count}) — в БД долг есть, в файле жильца нет</h4>
                <button data-zombie-from-integrity
                        style="padding:6px 12px; background:#7c3aed; color:#fff; border:none; border-radius:4px; cursor:pointer; font-size:12px; font-weight:500;">
                    <i class="fa-solid fa-broom"></i> Открыть Zombie-cleanup
                </button>
            </div>
            <p style="font-size:11px; color:#6b7280; margin:0 0 6px;">
                Зануляется через кнопку 👻 в шапке таблицы (отдельная модалка с подтверждением).
            </p>
            <table style="width:100%; border-collapse:collapse; font-size:11.5px;">
                <thead style="background:#f5f3ff;">
                    <tr>
                        <th style="padding:5px 7px; text-align:left;">ФИО</th>
                        <th style="padding:5px 7px; text-align:left;">Комната</th>
                        <th style="padding:5px 7px; text-align:right;">Долг 209</th>
                        <th style="padding:5px 7px; text-align:right;">Долг 205</th>
                    </tr>
                </thead>
                <tbody>
                ${data.extra_in_db.map(z => `
                    <tr>
                        <td style="padding:4px 7px;">${esc(z.username || '—')}</td>
                        <td style="padding:4px 7px; color:#6b7280;">${esc(z.room_label || '—')}</td>
                        <td style="padding:4px 7px; text-align:right; color:#b91c1c;">${f(z.actual.debt_209)}</td>
                        <td style="padding:4px 7px; text-align:right; color:#b91c1c;">${f(z.actual.debt_205)}</td>
                    </tr>
                `).join('')}
                </tbody>
            </table>`;

        const fixAllBtn = (s.drift_count + s.missing_in_db_count) === 0 ? '' : `
            <div style="margin-bottom:14px; padding:12px 14px; background:#eff6ff; border-left:3px solid #2563eb; border-radius:4px; display:flex; justify-content:space-between; align-items:center;">
                <div style="font-size:13px;">
                    <b>Найдено расхождений:</b> Drift=${s.drift_count}, Missing=${s.missing_in_db_count}, Extra=${s.extra_in_db_count}.
                    Drift+Missing исправляются автоматически из applied_state.
                </div>
                <button data-fix-category="all"
                        style="padding:8px 14px; background:#2563eb; color:#fff; border:none; border-radius:4px; cursor:pointer; font-size:13px; font-weight:600; white-space:nowrap;">
                    <i class="fa-solid fa-wand-magic-sparkles"></i> Исправить всё (${s.drift_count + s.missing_in_db_count})
                </button>
            </div>`;

        cont.innerHTML = `
            <div style="display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin-bottom:12px;">
                <div style="padding:10px; background:#fffbeb; border-radius:4px; border-left:3px solid #a16207;">
                    <div style="font-size:11px; color:#6b7280;">Drift</div>
                    <div style="font-size:22px; font-weight:700; color:#a16207;">${s.drift_count}</div>
                </div>
                <div style="padding:10px; background:#fef2f2; border-radius:4px; border-left:3px solid #dc2626;">
                    <div style="font-size:11px; color:#6b7280;">Missing</div>
                    <div style="font-size:22px; font-weight:700; color:#dc2626;">${s.missing_in_db_count}</div>
                </div>
                <div style="padding:10px; background:#f5f3ff; border-radius:4px; border-left:3px solid #7c3aed;">
                    <div style="font-size:11px; color:#6b7280;">Extra/Zombie</div>
                    <div style="font-size:22px; font-weight:700; color:#7c3aed;">${s.extra_in_db_count}</div>
                </div>
            </div>
            <div style="font-size:11px; color:#6b7280; margin-bottom:12px;">
                Сверка с логами 209=№${data.latest_209_log_id || '—'}, 205=№${data.latest_205_log_id || '—'}.
                Порог расхождения: ${data.threshold_rub} ₽. Жильцов в applied_state: ${s.expected_users}. Reading'ов в БД: ${s.actual_readings}.
            </div>
            ${allClean ? `
                <div style="margin-top:20px; padding:24px; text-align:center; color:#15803d;">
                    <i class="fa-solid fa-circle-check" style="font-size:32px;"></i>
                    <p style="margin:12px 0 0; font-weight:600;">Целостность данных в норме</p>
                    <p style="font-size:12px; color:#6b7280;">Никаких расхождений между импортом и БД не обнаружено.</p>
                </div>
            ` : (fixAllBtn + driftHtml + missingHtml + extraHtml)}
        `;

        // Прицепляем хендлеры на все кнопки фикса (group и individual).
        cont.querySelectorAll('[data-fix-category]').forEach(btn => {
            btn.addEventListener('click', () => {
                const cat = btn.getAttribute('data-fix-category');
                this._integrityFix(overlay, { category: cat });
            });
        });
        cont.querySelectorAll('[data-fix-user]').forEach(btn => {
            btn.addEventListener('click', () => {
                const uid = parseInt(btn.getAttribute('data-fix-user'), 10);
                if (uid) this._integrityFix(overlay, { category: 'user', user_id: uid });
            });
        });
        cont.querySelector('[data-zombie-from-integrity]')?.addEventListener('click', () => {
            overlay.remove();
            this.openZombieModal();
        });
    },

    /** Auto-fix Bug AK: вызывает /debts/integrity-fix с подтверждением.
     *  После фикса перезагружает модалку integrity-check, чтобы показать
     *  актуальное состояние (расхождений должно стать меньше или 0). */
    async _integrityFix(overlay, params) {
        const { category, user_id } = params;
        let confirmText;
        if (category === 'all') confirmText = 'Применить ВСЕ исправления (drift + missing) из applied_state?';
        else if (category === 'drift') confirmText = 'Записать «Ожидается» в БД для всех drift-расхождений?';
        else if (category === 'missing') confirmText = 'Создать reading-и для всех missing жильцов?';
        else if (category === 'user') confirmText = `Исправить расхождение для user_id=${user_id}?`;
        if (!await showConfirm(confirmText, { title: 'Исправление целостности', confirmText: 'Применить' })) return;

        try {
            const qs = new URLSearchParams({ category, confirm: 'YES' });
            if (user_id) qs.set('user_id', String(user_id));
            const res = await api.post(`/financier/debts/integrity-fix?${qs.toString()}`);
            const total = (res.fixed_drift || 0) + (res.fixed_missing || 0);
            toast(`Исправлено: drift=${res.fixed_drift || 0}, missing=${res.fixed_missing || 0}`, total > 0 ? 'success' : 'info');
            if ((res.errors || []).length) {
                console.warn('integrity-fix errors:', res.errors);
            }
            // Перезагружаем содержимое модалки и таблицу долгов.
            const fresh = await api.get('/financier/debts/integrity-check');
            this._renderIntegrityContent(overlay, fresh);
            this.reload();
            this.loadStats();
        } catch (e) {
            toast('Ошибка фикса: ' + (e.message || 'неизвестно'), 'error');
        }
    },

    /** Этап 3: модалка zombie-сальдо. Находит reading'и с долгом, которых
     *  в свежем импорте 1С уже нет. Кандидаты на зануление. */
    async openZombieModal() {
        const overlay = document.createElement('div');
        overlay.style.cssText = 'position:fixed; inset:0; background:rgba(0,0,0,0.45); z-index:9999; display:flex; align-items:center; justify-content:center; padding:20px;';
        overlay.innerHTML = `
            <div style="background:#fff; border-radius:8px; width:min(900px, 100%); max-height:90vh; display:flex; flex-direction:column;">
                <div style="display:flex; justify-content:space-between; align-items:center; padding:14px 18px; border-bottom:1px solid var(--border-color);">
                    <h3 style="margin:0; font-size:15px;">
                        👻 Zombie-сальдо: долги без свежего импорта 1С
                    </h3>
                    <button data-close-zombie style="background:none; border:none; font-size:20px; color:#6b7280; cursor:pointer;">×</button>
                </div>
                <div id="zombieContent" style="padding:14px 18px; overflow-y:auto; flex:1;">
                    <p style="color:var(--text-secondary);"><i class="fa-solid fa-spinner fa-spin"></i> Поиск...</p>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);
        const close = () => overlay.remove();
        overlay.querySelector('[data-close-zombie]').addEventListener('click', close);
        overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });

        try {
            const data = await api.get('/financier/debts/zombie-readings');
            this._renderZombieContent(overlay, data);
        } catch (e) {
            const c = overlay.querySelector('#zombieContent');
            if (c) c.innerHTML = `<p style="color:var(--danger-color);">Ошибка: ${esc(e.message)}</p>`;
        }
    },

    _renderZombieContent(overlay, data) {
        const cont = overlay.querySelector('#zombieContent');
        if (!cont) return;
        if (data.note) {
            cont.innerHTML = `<p style="color:var(--text-secondary);">${esc(data.note)}</p>`;
            return;
        }
        if (!data.count) {
            cont.innerHTML = `
                <div style="padding:24px; text-align:center; color:#15803d;">
                    <i class="fa-solid fa-circle-check" style="font-size:32px;"></i>
                    <p style="margin:12px 0 0; font-weight:600;">Zombie-сальдо не найдено</p>
                    <p style="font-size:12px; color:#6b7280;">Все долги в БД соответствуют свежему импорту 1С.</p>
                </div>`;
            return;
        }
        const totalSum = data.zombies.reduce((s, z) => s + (z.total_to_clean || 0), 0);
        const rowsHtml = data.zombies.map(z => `
            <tr>
                <td style="padding:6px 8px;">${z.user_id}</td>
                <td style="padding:6px 8px; font-weight:500;">${esc(z.username || '—')}</td>
                <td style="padding:6px 8px; font-size:11px; color:#6b7280;">${esc(z.room_label || '—')}</td>
                <td style="padding:6px 8px; text-align:right; color:#b91c1c;">${z.debt_209 ? z.debt_209.toFixed(2) : '—'}</td>
                <td style="padding:6px 8px; text-align:right; color:#b91c1c;">${z.debt_205 ? z.debt_205.toFixed(2) : '—'}</td>
                <td style="padding:6px 8px; text-align:right; color:#15803d;">${(z.overpayment_209 + z.overpayment_205) > 0 ? (z.overpayment_209 + z.overpayment_205).toFixed(2) : '—'}</td>
            </tr>
        `).join('');
        cont.innerHTML = `
            <div style="margin-bottom:12px; padding:10px 12px; background:#fef2f2; border-left:3px solid #b91c1c; border-radius:4px;">
                <b>Найдено zombie-reading'ов:</b> ${data.count}
                · сумма к занулению: <b>${totalSum.toFixed(2)} ₽</b>
                · сверка с логами 209=№${data.latest_209_log_id || '—'}, 205=№${data.latest_205_log_id || '—'}
                <p style="margin:6px 0 0; font-size:12px; color:#6b7280;">
                    Это reading'и с долгом/переплатой, чьего user_id нет в свежем импорте 1С.
                    Обычно — остатки от старого per-room импорта (Bug AG). Зануление безопасно: reading'и остаются
                    в БД (для аудита), но debt_*/overpayment_* становятся 0.
                </p>
            </div>
            <div style="overflow:auto; max-height:50vh;">
                <table style="width:100%; border-collapse:collapse; font-size:12px;">
                    <thead style="background:#f9fafb; position:sticky; top:0;">
                        <tr>
                            <th style="padding:6px 8px; text-align:left;">user_id</th>
                            <th style="padding:6px 8px; text-align:left;">ФИО</th>
                            <th style="padding:6px 8px; text-align:left;">Комната</th>
                            <th style="padding:6px 8px; text-align:right;">Долг 209</th>
                            <th style="padding:6px 8px; text-align:right;">Долг 205</th>
                            <th style="padding:6px 8px; text-align:right;">Перепл.</th>
                        </tr>
                    </thead>
                    <tbody>${rowsHtml}</tbody>
                </table>
            </div>
            <div style="margin-top:14px; display:flex; justify-content:flex-end; gap:8px;">
                <button data-zombie-confirm
                        style="padding:8px 14px; background:#b91c1c; color:#fff; border:none; border-radius:4px; cursor:pointer; font-weight:500;">
                    <i class="fa-solid fa-broom"></i> Занулить ${data.count} reading'ов
                </button>
            </div>
        `;
        overlay.querySelector('[data-zombie-confirm]')?.addEventListener('click', () => this._confirmZombieCleanup(overlay, data));
    },

    async _confirmZombieCleanup(overlay, data) {
        if (!await showConfirm(
            `Занулить debt_209, debt_205, overpayment_209, overpayment_205 у ${data.count} reading-ов?\n\n` +
            `Reading'и НЕ удалятся — только финансовые поля станут 0₽. Это обратимо через откат импорта или ручную корректировку.`,
            { title: 'Зануление сальдо', confirmText: 'Занулить', danger: true }
        )) return;
        try {
            const res = await api.post('/financier/debts/cleanup-zombie-readings?confirm=YES');
            toast(`Занулено ${res.cleaned} reading'ов`, 'success');
            overlay.remove();
            this.reload();
            this.loadStats();
        } catch (e) {
            toast('Ошибка: ' + (e.message || 'неизвестно'), 'error');
        }
    },

    async openDiagnoseModal(logId) {
        const overlay = document.createElement('div');
        overlay.style.cssText = 'position:fixed; inset:0; background:rgba(0,0,0,0.45); z-index:9999; display:flex; align-items:center; justify-content:center; padding:20px;';
        overlay.innerHTML = `
            <div style="background:#fff; border-radius:8px; width:min(900px, 100%); max-height:90vh; display:flex; flex-direction:column;">
                <div style="display:flex; justify-content:space-between; align-items:center; padding:14px 18px; border-bottom:1px solid var(--border-color);">
                    <h3 style="margin:0; font-size:15px;">
                        🔬 Диагностика парсера №${logId}
                    </h3>
                    <button data-close-diagnose style="background:none; border:none; font-size:20px; color:#6b7280; cursor:pointer;">×</button>
                </div>
                <div style="padding:10px 18px; border-bottom:1px solid var(--border-color); display:flex; gap:8px; align-items:center;">
                    <label style="font-size:12px; color:var(--text-secondary);">Поиск жильца:</label>
                    <input type="text" id="diagnoseFioSearch"
                           placeholder="Бендас / Миронов / любая часть ФИО"
                           style="flex:1; padding:5px 8px; font-size:12px; border:1px solid var(--border-color); border-radius:4px;">
                    <button id="diagnoseSearchBtn" class="action-btn primary-btn" style="padding:5px 10px; font-size:12px;">
                        <i class="fa-solid fa-search"></i> Найти
                    </button>
                </div>
                <div style="padding:16px 18px; overflow-y:auto; flex:1;" id="diagnoseContent">
                    <p style="color:var(--text-secondary); font-size:13px;">
                        <i class="fa-solid fa-spinner fa-spin"></i> Парсим архив… (5-15 сек)
                    </p>
                </div>
            </div>`;
        document.body.appendChild(overlay);
        const close = () => overlay.remove();
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay || e.target.closest('[data-close-diagnose]')) close();
        });

        // Кнопка поиска ФИО + Enter
        const searchInput = overlay.querySelector('#diagnoseFioSearch');
        const searchBtn = overlay.querySelector('#diagnoseSearchBtn');
        const reloadWithSearch = async () => {
            const fio = searchInput?.value?.trim() || '';
            const cont = overlay.querySelector('#diagnoseContent');
            if (cont) cont.innerHTML = `<p style="color:var(--text-secondary);"><i class="fa-solid fa-spinner fa-spin"></i> Ищем «${esc(fio)}»…</p>`;
            try {
                const url = fio
                    ? `/financier/debts/import-history/${logId}/parser-diagnose?fio_search=${encodeURIComponent(fio)}`
                    : `/financier/debts/import-history/${logId}/parser-diagnose`;
                const data = await api.get(url);
                this._renderDiagnoseContent(overlay, data, fio);
            } catch (e) {
                if (cont) cont.innerHTML = `<p style="color:var(--danger-color);">Ошибка: ${esc(e.message)}</p>`;
            }
        };
        searchBtn?.addEventListener('click', reloadWithSearch);
        searchInput?.addEventListener('keydown', (e) => { if (e.key === 'Enter') reloadWithSearch(); });

        try {
            const data = await api.get(`/financier/debts/import-history/${logId}/parser-diagnose`);
            this._renderDiagnoseContent(overlay, data, '');
            return;
        } catch (e) {
            const cont = overlay.querySelector('#diagnoseContent');
            if (cont) cont.innerHTML = `<p style="color:var(--danger-color);">Ошибка: ${esc(e.message)}</p>`;
            return;
        }
    },

    _renderDiagnoseContent(overlay, data, searchQuery) {
        const cont = overlay.querySelector('#diagnoseContent');
        if (!cont) return;
        try {
            const sectionsHtml = Object.keys(data.section_markers || {}).length
                ? Object.entries(data.section_markers).map(([k, v]) => `<span style="background:#dbeafe; color:#1e40af; padding:2px 7px; border-radius:4px; font-size:11px;">${esc(k)}: col ${v}</span>`).join(' ')
                : '<span style="color:#dc2626;">не найдены</span>';

            const accountHtml = data.account_total
                ? `<div style="background:#dcfce7; padding:8px 10px; border-radius:6px; font-size:12px;">
                       <b>Итоговая строка счёта найдена:</b><br>
                       row ${data.account_total.row_idx}, label_col ${data.account_total.label_col}, label «${esc(data.account_total.label)}»<br>
                       <b>Числовые позиции:</b> ${(data.account_total.numeric_positions || []).join(', ')}<br>
                       <b>Значения:</b> ${Object.entries(data.account_total.all_values || {}).map(([c, v]) => `col${c}=${Number(v).toLocaleString('ru-RU')}`).join(' · ')}
                   </div>`
                : `<div style="background:#fee2e2; color:#991b1b; padding:8px 10px; border-radius:6px; font-size:12px;">
                       ❌ Итоговая строка счёта (209.X / 205.X) НЕ найдена в первых 20 строках. Парсер пойдёт fallback'ом.
                   </div>`;

            const chosen = data.chosen || {};
            const chosenHtml = chosen.debt_col_last !== null
                ? `<div style="background:#fff; border:1px solid var(--border-color); padding:8px 10px; border-radius:6px; font-size:12px;">
                       <b>Парсер выбрал колонки:</b><br>
                       <b>Дебет:</b> начало <span style="color:#dc2626;">col ${chosen.debt_col_first}</span> · конец <span style="color:#059669;">col ${chosen.debt_col_last}</span><br>
                       <b>Кредит:</b> начало <span style="color:#dc2626;">col ${chosen.overpay_col_first}</span> · конец <span style="color:#059669;">col ${chosen.overpay_col_last}</span><br>
                       <b>Стратегия:</b> ${esc(chosen.strategy || '—')}
                       ${chosen.debt_col_first === chosen.debt_col_last ? '<br><b style="color:#dc2626;">⚠ debt_first == debt_last — парсер сводит «начало» и «конец» к одной колонке (НЕПРАВИЛЬНО!)</b>' : ''}
                   </div>`
                : '<div style="color:#dc2626;">⚠ Парсер не определил колонки!</div>';

            const renderSample = (s) => {
                // Сравнение с БД (если есть db_lookup из fio_search режима).
                const db = s.db_lookup;
                let dbBlock = '';
                if (db) {
                    if (db.matched_user_id) {
                        const mismatchColor = db.mismatch ? '#dc2626' : '#059669';
                        const mismatchIcon = db.mismatch ? '⚠' : '✓';
                        const dbDebt = db.db_debt !== null ? Number(db.db_debt).toFixed(2) : 'NULL (нет reading)';
                        dbBlock = `
                            <div style="margin-top:6px; padding:6px 8px; background:${db.mismatch ? '#fef2f2' : '#dcfce7'}; border-left:3px solid ${mismatchColor}; border-radius:4px; font-size:11px;">
                                <b>В БД</b> (user_id=${db.matched_user_id}, username=${esc(db.matched_username || '')}):
                                ${mismatchIcon} debt = ${dbDebt} (ожидается ${db.expected_debt})
                                ${db.fuzzy && db.fuzzy.score ? `<br><i>fuzzy: matched «${esc(db.fuzzy.key || '')}» score ${db.fuzzy.score}${db.fuzzy.too_low ? ' ⚠ TOO LOW' : ''}</i>` : ''}
                                ${db.mismatch ? '<br><b style="color:#991b1b;">⚠ Значения не совпадают — переимпорт нужен или wrong-user fuzzy.</b>' : ''}
                            </div>`;
                    } else {
                        dbBlock = `
                            <div style="margin-top:6px; padding:6px 8px; background:#fef2f2; border-left:3px solid #dc2626; border-radius:4px; font-size:11px;">
                                <b>⚠ Жилец НЕ найден в БД</b>
                                ${db.fuzzy ? `<br>лучший fuzzy: «${esc(db.fuzzy.key || '')}» score ${db.fuzzy.score} (порог 80)` : ''}
                                <br>Эти деньги (${db.expected_debt} / ${db.expected_overpayment}) попадут в not_found.
                            </div>`;
                    }
                }
                // Raw values по колонкам
                const rawHtml = s.raw_values ? `
                    <div style="font-size:10.5px; color:var(--text-secondary); margin-top:2px;">
                        ${Object.entries(s.raw_values).map(([k, v]) => `${esc(k)}=${v === null ? '<i>null</i>' : v}`).join(' · ')}
                    </div>` : '';
                return `
                    <div style="padding:6px 8px; background:#f9fafb; border-radius:4px; margin-bottom:4px; font-size:12px;">
                        <b>${esc(s.fio)}</b> (col ${s.fio_col})<br>
                        <span style="color:#dc2626;">debt = ${s.debt_extracted}</span> · <span style="color:#7c3aed;">overpayment = ${s.overpayment_extracted}</span>
                        ${rawHtml}
                        ${dbBlock}
                    </div>`;
            };
            const samplesHtml = (data.samples || []).length
                ? `<div style="margin-top:14px;">
                       <h4 style="margin:0 0 6px 0; font-size:13px;">${searchQuery ? `Найдено по «${esc(searchQuery)}»: ${data.samples.length}` : 'Sample 3 жильцов:'}</h4>
                       ${(data.samples || []).map(renderSample).join('')}
                   </div>`
                : (searchQuery ? `<div style="margin-top:14px; padding:10px; background:#fee2e2; color:#991b1b; border-radius:4px;">По запросу «${esc(searchQuery)}» в файле никого не найдено.</div>` : '');

            cont.innerHTML = `
                <div style="display:grid; gap:12px;">
                    <div>
                        <div style="font-size:11px; color:var(--text-secondary); margin-bottom:4px;">SECTION MARKERS:</div>
                        ${sectionsHtml}
                    </div>
                    <div>
                        <div style="font-size:11px; color:var(--text-secondary); margin-bottom:4px;">«Дебет» / «Кредит» позиции (header):</div>
                        Дебет: ${(data.debit_cols_in_header || []).join(', ') || '—'} · Кредит: ${(data.credit_cols_in_header || []).join(', ') || '—'}
                    </div>
                    ${accountHtml}
                    ${chosenHtml}
                    ${samplesHtml}
                </div>
            `;
        } catch (e) {
            const cont = overlay.querySelector('#diagnoseContent');
            if (cont) cont.innerHTML = `<p style="color:var(--danger-color);">Ошибка: ${esc(e.message)}</p>`;
        }
    },

    async deleteImportHistory(logId) {
        if (!await showConfirm(
            `Удалить запись истории импорта №${logId}?\n\n` +
            `ВНИМАНИЕ: это удаление БЕЗ отката данных. Используйте только если\n` +
            `этот импорт уже не актуален (данные перетёрты последующим импортом\n` +
            `или массовым rebuild). Если нужен откат — жми «Откатить» вместо.`,
            { title: 'Удаление записи', confirmText: 'Удалить', danger: true }
        )) return;
        try {
            await api.delete(`/financier/debts/import-history/${logId}`);
            toast(`Запись №${logId} удалена из истории`, 'success');
            this.loadImportHistory();
        } catch (e) {
            toast('Ошибка удаления: ' + e.message, 'error');
        }
    },

    /** Массовая чистка: удаляет все откаченные/failed + старые completed
     *  (оставляет последние 5 на каждый счёт). Идеально после массового
     *  rebuild когда в истории накопился мусор. */
    async cleanupImportHistory() {
        if (!await showConfirm(
            `Очистить устаревшие записи истории импорта?\n\n` +
            `Будут удалены:\n` +
            `  • все откаченные (status=reverted)\n` +
            `  • все failed (с ошибкой)\n` +
            `  • completed старше последних 5 на каждый счёт (209/205).\n\n` +
            `Актуальные последние импорты сохранятся. Действие необратимо.`,
            { title: 'Очистка истории', confirmText: 'Очистить', danger: true }
        )) return;
        try {
            const res = await api.post(
                `/financier/debts/import-history/cleanup?keep_last=5`,
                {}
            );
            toast(`Готово. Осталось записей: ${res.remaining !== undefined ? res.remaining : '—'}`, 'success');
            this.loadImportHistory();
        } catch (e) {
            toast('Ошибка чистки: ' + e.message, 'error');
        }
    },

    /** Поиск ФИО жильца в архивах последних импортов 1С. Открывает
     *  модалку которая для каждого импорта показывает: найдено / не
     *  найдено + значения из строки (если найдено). Помогает понять
     *  почему у жильца «—» в долгах. */
    async openCheckCoverage(userId, username) {
        // Простая модалка через document.body. Не используем глобальные
        // modal-helpers чтобы не плодить зависимости.
        const overlay = document.createElement('div');
        overlay.style.cssText = 'position:fixed; inset:0; background:rgba(0,0,0,0.45); z-index:9999; display:flex; align-items:center; justify-content:center; padding:20px;';
        overlay.innerHTML = `
            <div style="background:#fff; border-radius:8px; width:min(720px, 100%); max-height:90vh; display:flex; flex-direction:column;">
                <div style="display:flex; justify-content:space-between; align-items:center; padding:14px 18px; border-bottom:1px solid var(--border-color);">
                    <h3 style="margin:0; font-size:15px;">
                        🔍 Поиск «${esc(username)}» в архивах 1С
                    </h3>
                    <button data-close-coverage style="background:none; border:none; font-size:20px; color:#6b7280; cursor:pointer;">×</button>
                </div>
                <div style="padding:16px 18px; overflow-y:auto; flex:1;" id="coverageContent">
                    <p style="color:var(--text-secondary); font-size:13px;">
                        <i class="fa-solid fa-spinner fa-spin"></i> Парсим архивы… (до 20 сек, openpyxl на read-only)
                    </p>
                </div>
            </div>`;
        document.body.appendChild(overlay);
        const close = () => overlay.remove();
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay || e.target.closest('[data-close-coverage]')) close();
        });

        try {
            const data = await api.get(`/financier/debts/check-resident-coverage/${userId}`);
            const cont = overlay.querySelector('#coverageContent');
            if (!cont) return;

            const headerHtml = `
                <div style="margin-bottom:14px; padding:10px 12px; background:#f3f4f6; border-radius:6px; font-size:12.5px;">
                    <div><b>ФИО в БД:</b> ${esc(data.fio_db)}</div>
                    <div style="color:var(--text-secondary); margin-top:3px;">
                        Проверено импортов: ${data.imports_checked} (последних)
                    </div>
                </div>`;

            const items = (data.results || []).map(r => {
                let body;
                if (r.error) {
                    body = `<div style="color:#dc2626; font-size:11px;">⚠ ${esc(r.error)}</div>`;
                } else if (!r.matches.length) {
                    body = `<div style="color:var(--text-secondary); font-size:11px; font-style:italic;">Не найдено в этом архиве</div>`;
                } else {
                    body = r.matches.map(m => `
                        <div style="padding:6px 8px; background:${m.exact_match ? '#dcfce7' : '#fef3c7'}; border-radius:4px; margin-top:4px; font-size:11px;">
                            <b>${m.exact_match ? '✓ Точное совпадение' : '~ Похожее ФИО'}:</b>
                            ${esc(m.fio_in_excel)}<br>
                            <span style="color:var(--text-secondary);">Excel row ${m.row_excel} · значения: ${m.numeric_values.length ? m.numeric_values.map(v => v.toFixed(2)).join(' / ') : 'все нули'}</span>
                        </div>
                    `).join('');
                }
                const statusColor = r.status === 'completed' ? '#059669' : '#6b7280';
                return `
                    <div style="border:1px solid var(--border-color); border-radius:6px; padding:10px 12px; margin-bottom:8px;">
                        <div style="display:flex; justify-content:space-between; font-size:12px;">
                            <span><b>№${r.log_id} · ${esc(r.account_type)}</b> · <span style="color:${statusColor};">${esc(r.status)}</span></span>
                            <span style="color:var(--text-secondary);">${r.started_at ? esc(r.started_at.split('T')[0]) : '—'}</span>
                        </div>
                        ${body}
                    </div>`;
            }).join('');

            // Подсказки админу.
            const anyFound = (data.results || []).some(r => r.matches && r.matches.length > 0);
            const anyWithValues = (data.results || []).some(r =>
                r.matches && r.matches.some(m => m.numeric_values && m.numeric_values.length > 0)
            );
            let hint;
            if (!anyFound) {
                hint = `<div style="background:#fee2e2; color:#991b1b; padding:10px 12px; border-radius:6px; font-size:12px; margin-top:8px;">
                    💡 <b>ФИО не найдено ни в одном из последних импортов.</b> Жилец не передавался из 1С — обратитесь к бухгалтерии.
                </div>`;
            } else if (anyWithValues) {
                hint = `<div style="background:#fef3c7; color:#92400e; padding:10px 12px; border-radius:6px; font-size:12px; margin-top:8px;">
                    💡 <b>ФИО найдено с цифрами, но в БД у жильца долгов нет.</b> Возможно fuzzy-привязка пошла к другому жильцу. Откройте бейдж «⚠ N» (not_found) у соответствующего импорта и попробуйте reassign.
                </div>`;
            } else {
                hint = `<div style="background:#dcfce7; color:#166534; padding:10px 12px; border-radius:6px; font-size:12px; margin-top:8px;">
                    💡 <b>ФИО найдено, но с нулями.</b> Это нормально — у жильца нет долгов в 1С.
                </div>`;
            }

            cont.innerHTML = headerHtml + items + hint;
        } catch (e) {
            const cont = overlay.querySelector('#coverageContent');
            if (cont) cont.innerHTML = `<p style="color:var(--danger-color);">Ошибка: ${esc(e.message)}</p>`;
        }
    },

    // Разбор: почему ненайденные ФИО не сматчились (категории + ближайший кандидат).
    async renderNotFoundAnalysis(logId) {
        const box = this.dom.notFoundList;
        if (!box) return;
        box.innerHTML = '<div style="padding:20px; text-align:center;"><i class="fa-solid fa-spinner fa-spin"></i> Анализ…</div>';
        try {
            const data = await api.get(`/financier/debts/import-history/${logId}/not-found-analysis`);
            const c = data.categories || {};
            const CAT = {
                same:     { label: 'Скорее тот же',  color: '#15803d', bg: '#dcfce7', desc: 'фамилия+имя+отчество совпали — привязать безопасно' },
                namesake: { label: 'Однофамилец',    color: '#9a3412', bg: '#ffedd5', desc: 'РАЗНЫЙ человек — без проверки не привязывать' },
                absent:   { label: 'Нет в базе',     color: '#991b1b', bg: '#fee2e2', desc: 'не заведён жильцом (новый / наниматель / не-резидент)' },
            };
            const summary = ['same', 'namesake', 'absent'].map(k => `
                <div style="flex:1; background:${CAT[k].bg}; border-radius:8px; padding:10px; text-align:center;">
                    <div style="font-size:22px; font-weight:700; color:${CAT[k].color};">${c[k] || 0}</div>
                    <div style="font-size:11px; font-weight:600; color:${CAT[k].color};">${CAT[k].label}</div>
                    <div style="font-size:10px; color:var(--text-secondary); margin-top:2px;">${CAT[k].desc}</div>
                </div>`).join('');
            const rows = (data.items || []).map(it => {
                const cat = CAT[it.category] || CAT.absent;
                const cand = it.candidate
                    ? `${esc(it.candidate.username)}${it.candidate.room ? ` · <span style="color:var(--text-secondary);">${esc(it.candidate.room)}</span>` : ''}`
                    : '<span style="color:var(--text-tertiary);">—</span>';
                return `<tr style="border-bottom:1px solid #eef2f7;">
                    <td style="padding:5px 8px;">${esc(it.fio)}</td>
                    <td style="padding:5px 8px; text-align:right; font-family:monospace; color:#991b1b;">${Number(it.debt || 0).toFixed(2)}</td>
                    <td style="padding:5px 8px; text-align:center;"><span style="background:${cat.bg}; color:${cat.color}; padding:1px 7px; border-radius:8px; font-size:11px; font-weight:700;">${it.best_score}</span></td>
                    <td style="padding:5px 8px; font-size:12px;">${cand}${it.reason ? ` <span style="color:var(--text-tertiary); font-size:10px;">(${esc(it.reason)})</span>` : ''}</td>
                </tr>`;
            }).join('');
            box.innerHTML = `
                <button class="action-btn secondary-btn" id="btnNfBack" style="font-size:12px; padding:5px 10px; margin-bottom:12px;">← К списку (привязка)</button>
                <div style="display:flex; gap:8px; margin-bottom:12px;">${summary}</div>
                <table style="width:100%; border-collapse:collapse; font-size:12px;">
                    <thead style="background:var(--bg-page); font-size:10px; color:var(--text-tertiary); text-transform:uppercase;">
                        <tr>
                            <th style="text-align:left; padding:5px 8px;">ФИО из 1С</th>
                            <th style="text-align:right; padding:5px 8px;">Долг</th>
                            <th style="text-align:center; padding:5px 8px;" title="0–100: насколько близок лучший кандидат в базе">Score</th>
                            <th style="text-align:left; padding:5px 8px;">Ближайший в базе</th>
                        </tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>`;
            document.getElementById('btnNfBack')?.addEventListener('click', () => this.openNotFoundModal(logId));
        } catch (e) {
            box.innerHTML = `<div style="padding:16px; color:var(--danger-color);">Ошибка анализа: ${esc(e.message)}</div>`;
        }
    },

    // ==========================================================================
    // МОДАЛКА «НЕ НАЙДЕННЫЕ»
    // ==========================================================================
    async openNotFoundModal(logId) {
        if (!this.dom.notFoundModal) return;
        this.dom.notFoundModal.classList.add('open');
        this.dom.notFoundLogMeta.textContent = `импорт №${logId}`;
        this.dom.notFoundList.innerHTML = '<div style="padding:20px; text-align:center;"><i class="fa-solid fa-spinner fa-spin"></i> Загрузка…</div>';
        try {
            const data = await api.get(`/financier/debts/import-history/${logId}/not-found`);
            // Part B: явно подписываем счёт (209/205), чтобы было видно «в каком
            // счёте не найдено». Суммы по каждому ФИО рендерит renderNotFoundRow.
            this.dom.notFoundLogMeta.textContent = `импорт №${logId} · счёт ${data.account_type || '—'}`;
            const list = data.not_found_users || [];
            if (!list.length) {
                this.dom.notFoundList.innerHTML = '<div style="padding:20px; text-align:center; color:var(--text-secondary);">Все ФИО из этого импорта привязаны.</div>';
                return;
            }
            this.dom.notFoundList.innerHTML = `
                <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:10px; margin-bottom:12px;">
                    <p class="hint-text" style="font-size:12px; margin:0;">
                        ФИО из Excel, которых fuzzy-матчер не смог привязать к жильцу.
                        <b>Суммы долга/переплаты подгружены автоматически</b> — нажмите
                        «Найти похожих» (если жилец есть в системе) или «Создать жильца».
                    </p>
                    <button class="action-btn secondary-btn" id="btnNfAnalysis" style="white-space:nowrap; font-size:12px; padding:5px 10px;" title="Разобрать почему не сматчились: ближайший кандидат + категория">
                        📊 Почему не нашлись?
                    </button>
                </div>
                ${list.map(item => {
                    // Backend нормализует к dict {fio, debt, overpayment}.
                    // Старые импорты (до фикса) — debt/overpayment = "0".
                    const fio = (typeof item === 'object') ? item.fio : item;
                    const debt = (typeof item === 'object') ? Number(item.debt) || 0 : 0;
                    const overpay = (typeof item === 'object') ? Number(item.overpayment) || 0 : 0;
                    return this.renderNotFoundRow(fio, logId, data.account_type, debt, overpay);
                }).join('')}
            `;
            document.getElementById('btnNfAnalysis')?.addEventListener('click', () => this.renderNotFoundAnalysis(logId));
            // Контекст для click-handler. Меняется при каждом openNotFoundModal,
            // handler читает из state — нет накопления listeners.
            this._nfCtx = { logId, accountType: data.account_type };
            if (!this._nfClickHandlerAttached) {
                this.dom.notFoundList.addEventListener('click', (e) => {
                    const btn = e.target.closest('button[data-nf-action]');
                    if (!btn || !this._nfCtx) return;
                    const { logId, accountType } = this._nfCtx;
                    const action = btn.dataset.nfAction;
                    const row = btn.closest('.nf-row');
                    if (!row) return;
                    const fio = row.dataset.fio;
                    if (action === 'find') {
                        this._nfFindCandidates(row, fio, logId, accountType);
                    } else if (action === 'create') {
                        this._nfShowCreateForm(row, fio, logId, accountType);
                    } else if (action === 'legacy') {
                        this._nfShowLegacyForm(row, fio, logId, accountType);
                    } else if (action === 'pick-candidate') {
                        this._nfPickCandidate(row, fio, logId, accountType, Number(btn.dataset.userId), btn.dataset.username);
                    } else if (action === 'edit-fio') {
                        this._nfEditFio(btn, Number(btn.dataset.userId), btn.dataset.username);
                    } else if (action === 'submit-create') {
                        this._nfSubmitCreate(row, fio, logId, accountType);
                    } else if (action === 'submit-legacy') {
                        this._nfSubmitLegacy(row, fio, logId, accountType);
                    }
                });
                this._nfClickHandlerAttached = true;
            }
            // Инжектим CSS для иконки-карандаша «Исправить ФИО» один раз —
            // нужен hover-стейт и opacity, которые inline в HTML не работают.
            if (!document.getElementById('nf-fio-edit-styles')) {
                const styleEl = document.createElement('style');
                styleEl.id = 'nf-fio-edit-styles';
                styleEl.textContent = `
                    .nf-edit-fio-btn {
                        background: transparent;
                        border: none;
                        padding: 2px 5px;
                        border-radius: 4px;
                        color: var(--text-tertiary);
                        opacity: 0.35;
                        cursor: pointer;
                        font-size: 11px;
                        transition: opacity 0.15s, background 0.15s, color 0.15s;
                    }
                    .nf-candidate:hover .nf-edit-fio-btn { opacity: 0.7; }
                    .nf-edit-fio-btn:hover {
                        opacity: 1 !important;
                        background: #eef2ff;
                        color: #4338ca;
                    }
                `;
                document.head.appendChild(styleEl);
            }
        } catch (e) {
            this.dom.notFoundList.innerHTML = `<div style="padding:16px; color:var(--danger-color);">Ошибка: ${esc(e.message)}</div>`;
        }
    },

    renderNotFoundRow(fio, logId, accountType, debt = 0, overpay = 0) {
        // Каждая строка содержит:
        //  - ФИО + поля для суммы (префилл из импорта, можно править)
        //  - Кнопка «Найти похожих» — раскрывает inline-блок с кандидатами
        //  - Кнопка «Создать жильца» — раскрывает форму создания
        //  - (старое) Inline-форма с логином — fallback если знаешь точный логин
        const safeId = btoa(unescape(encodeURIComponent(fio))).replace(/[^a-zA-Z0-9]/g, '').slice(0, 16);
        const sumHint = (debt > 0 || overpay > 0)
            ? `<span style="font-size:11px; color:#92400e; margin-left:6px;">
                 ${debt > 0 ? `долг ${debt.toLocaleString('ru-RU')} ₽` : ''}
                 ${overpay > 0 ? `${debt > 0 ? ' · ' : ''}переплата ${overpay.toLocaleString('ru-RU')} ₽` : ''}
                 (из файла)
               </span>`
            : '';
        return `
            <div class="nf-row" data-fio="${esc(fio)}" data-row-id="${safeId}"
                 style="border:1px solid var(--border-color); border-radius:8px; margin-bottom:10px; padding:10px;">
                <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:8px; margin-bottom:8px;">
                    <div style="flex:1; min-width:0;">
                        <div style="font-weight:600; font-size:13px; color:#1f2937; overflow-wrap:anywhere;">
                            ${esc(fio)}${sumHint}
                        </div>
                        <div style="font-size:11px; color:var(--text-secondary);">счёт ${esc(accountType)}</div>
                    </div>
                    <div style="display:flex; gap:6px; flex-wrap:wrap;">
                        <input type="number" data-nf-debt step="0.01" placeholder="Долг ₽" value="${debt || ''}" style="width:100px; font-size:12px;">
                        <input type="number" data-nf-overpay step="0.01" placeholder="Перепл. ₽" value="${overpay || ''}" style="width:100px; font-size:12px;">
                    </div>
                </div>
                <div style="display:flex; gap:6px; flex-wrap:wrap;">
                    <button data-nf-action="find" class="action-btn primary-btn" style="padding:5px 10px; font-size:12px;">
                        <i class="fa-solid fa-magnifying-glass"></i> Найти похожих
                    </button>
                    <button data-nf-action="create" class="action-btn success-btn" style="padding:5px 10px; font-size:12px;">
                        <i class="fa-solid fa-user-plus"></i> Создать жильца
                    </button>
                    <button data-nf-action="legacy" class="action-btn secondary-btn" style="padding:5px 10px; font-size:12px;">
                        <i class="fa-solid fa-keyboard"></i> Логин вручную
                    </button>
                </div>
                <div data-nf-pane="candidates" style="display:none; margin-top:10px; padding:10px; background:#f9fafb; border-radius:6px;"></div>
                <div data-nf-pane="create" style="display:none; margin-top:10px; padding:10px; background:#f0fdf4; border-radius:6px; border:1px solid #bbf7d0;"></div>
                <div data-nf-pane="legacy" style="display:none; margin-top:10px;"></div>
            </div>
        `;
    },

    _nfGetSums(row) {
        // Возвращает {debt, overpayment} из input полей в шапке строки.
        const debt = parseFloat(row.querySelector('[data-nf-debt]')?.value) || 0;
        const overpayment = parseFloat(row.querySelector('[data-nf-overpay]')?.value) || 0;
        return { debt, overpayment };
    },

    _nfShowPane(row, paneName) {
        // Прячет все панели в строке, потом показывает нужную.
        row.querySelectorAll('[data-nf-pane]').forEach(p => {
            p.style.display = p.dataset.nfPane === paneName ? '' : 'none';
        });
    },

    _nfRenderCandidates(cands, headerLabel) {
        if (!cands.length) {
            return `<div style="font-size:13px; color:var(--text-secondary); padding:8px;">
                Жильцов не нашлось.
            </div>`;
        }
        return `
            <div style="font-size:11px; color:var(--text-secondary); margin-bottom:8px; text-transform:uppercase;">
                ${headerLabel} (${cands.length})
            </div>
            ${cands.map(c => `
                <div class="nf-candidate" data-user-id="${c.id}" data-username="${esc(c.username)}"
                     style="display:flex; justify-content:space-between; align-items:center; gap:10px;
                            padding:8px 10px; background:#fff; border:1px solid var(--border-color); border-radius:6px; margin-bottom:6px;">
                    <div style="flex:1; min-width:0;">
                        <div class="nf-candidate-name-row" style="font-weight:600; font-size:13px; display:inline-flex; align-items:center; gap:6px;">
                            <span class="nf-candidate-username">${esc(c.username)}</span>
                            <span style="font-size:11px; color:var(--text-secondary);">${c.score}%</span>
                            <button data-nf-action="edit-fio" data-user-id="${c.id}" data-username="${esc(c.username)}"
                                    class="nf-edit-fio-btn"
                                    title="Исправить ФИО в базе (если в системе написано с ошибкой)">
                                <i class="fa-solid fa-pen"></i>
                            </button>
                        </div>
                        <div style="font-size:11px; color:var(--text-secondary);">
                            ${esc(c.room_label)} · ${c.residents_count} чел.
                        </div>
                        ${c.reason ? `<div style="font-size:11px; color:#92400e; margin-top:3px;">
                            <i class="fa-solid fa-circle-info"></i> ${esc(c.reason)}
                        </div>` : ''}
                    </div>
                    <button data-nf-action="pick-candidate" data-user-id="${c.id}" data-username="${esc(c.username)}"
                            class="action-btn primary-btn" style="padding:4px 10px; font-size:12px; white-space:nowrap;">
                        <i class="fa-solid fa-check"></i> Это он
                    </button>
                </div>
            `).join('')}`;
    },

    _nfEditFio(btn, userId, currentUsername) {
        // Превращаем span с username в input + кнопки Save/Cancel inline.
        const cand = btn.closest('.nf-candidate');
        if (!cand) return;
        const nameRow = cand.querySelector('.nf-candidate-name-row');
        if (!nameRow || nameRow.dataset.editing === '1') return;
        nameRow.dataset.editing = '1';
        const originalHtml = nameRow.innerHTML;
        nameRow.innerHTML = `
            <input type="text" data-nf-fio-input value="${esc(currentUsername)}"
                   style="width:65%; padding:3px 6px; font-size:13px; border:1px solid var(--border-color); border-radius:4px;">
            <button data-nf-fio-save class="action-btn success-btn" style="padding:3px 8px; font-size:11px; margin-left:4px;">
                <i class="fa-solid fa-check"></i>
            </button>
            <button data-nf-fio-cancel class="action-btn secondary-btn" style="padding:3px 8px; font-size:11px; margin-left:2px;">
                <i class="fa-solid fa-xmark"></i>
            </button>
        `;
        const input = nameRow.querySelector('[data-nf-fio-input]');
        input.focus();
        input.select();

        nameRow.querySelector('[data-nf-fio-cancel]').addEventListener('click', () => {
            nameRow.innerHTML = originalHtml;
            nameRow.dataset.editing = '';
        });
        const doSave = async () => {
            const newName = input.value.trim();
            if (!newName || newName.length < 3) {
                toast('Имя минимум 3 символа', 'warning');
                return;
            }
            if (newName === currentUsername) {
                nameRow.innerHTML = originalHtml;
                nameRow.dataset.editing = '';
                return;
            }
            try {
                await api.put(`/users/${userId}`, { username: newName });
                toast(`Имя жильца обновлено: ${newName}`, 'success');
                // Обновляем DOM на месте — не пересоздаём всю модалку.
                cand.dataset.username = newName;
                cand.querySelectorAll('button[data-username]').forEach(b => {
                    b.dataset.username = newName;
                });
                nameRow.innerHTML = originalHtml;
                nameRow.querySelector('.nf-candidate-username').textContent = newName;
                nameRow.querySelector('button[data-nf-action="edit-fio"]').dataset.username = newName;
                nameRow.dataset.editing = '';
            } catch (e) {
                toast('Ошибка обновления: ' + e.message, 'error');
            }
        };
        nameRow.querySelector('[data-nf-fio-save]').addEventListener('click', doSave);
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); doSave(); }
            if (e.key === 'Escape') {
                nameRow.innerHTML = originalHtml;
                nameRow.dataset.editing = '';
            }
        });
    },

    async _nfFindCandidates(row, fio, logId, accountType) {
        const pane = row.querySelector('[data-nf-pane="candidates"]');
        this._nfShowPane(row, 'candidates');

        // 1) Сначала рендерим контейнер с поиском и spinner-результатом —
        // чтобы input поиска появился сразу, не ждал API.
        pane.innerHTML = `
            <div style="margin-bottom:10px;">
                <input type="text" data-nf-search placeholder="🔍 Поиск по фамилии или имени (мин. 2 буквы)"
                       style="width:100%; padding:7px 10px; font-size:13px; border:1px solid var(--border-color); border-radius:6px;">
                <div style="font-size:11px; color:var(--text-secondary); margin-top:3px;">
                    Auto-suggest показывает похожих по импортированному ФИО. Введите запрос — найдёт по подстроке.
                </div>
            </div>
            <div data-nf-results>
                <div style="text-align:center; padding:14px; color:var(--text-secondary);">
                    <i class="fa-solid fa-spinner fa-spin"></i> Поиск похожих по «${esc(fio)}»…
                </div>
            </div>`;

        const results = pane.querySelector('[data-nf-results]');
        const input = pane.querySelector('[data-nf-search]');

        // 2) Auto-suggest по fio. Загрузим один раз и оставим как fallback
        // когда input пустой.
        let autoSuggestHtml = '';
        try {
            const data = await api.get(`/financier/debts/find-candidates?fio=${encodeURIComponent(fio)}&limit=15`);
            autoSuggestHtml = this._nfRenderCandidates(data.candidates || [], 'Похожие по импорту');
            results.innerHTML = autoSuggestHtml;
        } catch (e) {
            results.innerHTML = `<div style="color:#b91c1c; padding:8px;">Ошибка: ${esc(e.message)}</div>`;
        }

        // 3) Debounced ручной поиск
        let searchTimer = null;
        input.addEventListener('input', () => {
            clearTimeout(searchTimer);
            const q = input.value.trim();
            if (q.length < 2) {
                // Возвращаем auto-suggest по fio
                results.innerHTML = autoSuggestHtml;
                return;
            }
            results.innerHTML = `
                <div style="text-align:center; padding:14px; color:var(--text-secondary);">
                    <i class="fa-solid fa-spinner fa-spin"></i> Ищу «${esc(q)}»…
                </div>`;
            searchTimer = setTimeout(async () => {
                try {
                    const data = await api.get(`/financier/debts/find-candidates?q=${encodeURIComponent(q)}&limit=20`);
                    results.innerHTML = this._nfRenderCandidates(data.candidates || [], `Найдено по «${esc(q)}»`);
                } catch (err) {
                    results.innerHTML = `<div style="color:#b91c1c; padding:8px;">Ошибка: ${esc(err.message)}</div>`;
                }
            }, 250);
        });
    },

    async _nfPickCandidate(row, fio, logId, accountType, userId, username) {
        const { debt, overpayment } = this._nfGetSums(row);
        const fd = new FormData();
        fd.append('fio', fio);
        fd.append('user_id', String(userId));
        fd.append('debt', String(debt));
        fd.append('overpayment', String(overpayment));
        try {
            await api.post(`/financier/debts/import-history/${logId}/reassign`, fd);
            toast(`Привязано: ${fio} → ${username}`, 'success');
            row.remove();
            this.reload();
            this.loadImportHistory();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },

    _nfShowCreateForm(row, fio, logId, accountType) {
        const pane = row.querySelector('[data-nf-pane="create"]');
        this._nfShowPane(row, 'create');
        // Генерим читаемый пароль (без 0/O, 1/l/I).
        const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789';
        const arr = new Uint8Array(12);
        (window.crypto || window.msCrypto).getRandomValues(arr);
        let pwd = ''; for (let i = 0; i < 12; i++) pwd += chars[arr[i] % chars.length];
        pane.innerHTML = `
            <div style="font-size:11px; color:#166534; margin-bottom:8px; text-transform:uppercase;">
                Создать нового жильца + сразу записать долг
            </div>
            <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px;">
                <input type="text" data-nf-login placeholder="Логин (для входа)" value="${esc(fio)}" style="font-size:13px;">
                <input type="text" data-nf-pwd placeholder="Пароль" value="${pwd}" style="font-size:13px; font-family:monospace;">
                <input type="text" data-nf-dorm placeholder="Общежитие" style="font-size:13px;">
                <input type="text" data-nf-room placeholder="Номер комнаты" style="font-size:13px;">
                <input type="number" data-nf-residents value="1" min="1" max="20" placeholder="Жильцов в семье" style="font-size:13px;">
                <select data-nf-type style="font-size:13px;">
                    <option value="family">Семья (по счётчику)</option>
                    <option value="single">Одиночка (per capita)</option>
                </select>
            </div>
            <div style="margin-top:8px;">
                <button data-nf-action="submit-create" class="action-btn success-btn" style="padding:6px 12px; font-size:12px;">
                    <i class="fa-solid fa-check"></i> Создать и привязать
                </button>
            </div>
        `;
    },

    async _nfSubmitCreate(row, fio, logId, accountType) {
        const { debt, overpayment } = this._nfGetSums(row);
        const pane = row.querySelector('[data-nf-pane="create"]');
        const login = pane.querySelector('[data-nf-login]').value.trim();
        const password = pane.querySelector('[data-nf-pwd]').value.trim();
        const dorm = pane.querySelector('[data-nf-dorm]').value.trim();
        const roomNo = pane.querySelector('[data-nf-room]').value.trim();
        const residents = Number(pane.querySelector('[data-nf-residents]').value) || 1;
        const type = pane.querySelector('[data-nf-type]').value;

        if (!login || login.length < 3) return toast('Логин минимум 3 символа', 'warning');
        if (!password || password.length < 6) return toast('Пароль минимум 6 символов', 'warning');
        if (!dorm || !roomNo) return toast('Укажите общежитие и номер комнаты', 'warning');

        try {
            await api.post(`/financier/debts/import-history/${logId}/create-and-match`, {
                fio,
                username: login,
                password,
                dormitory_name: dorm,
                room_number: roomNo,
                debt,
                overpayment,
                residents_count: residents,
                resident_type: type,
            });
            toast(`Создан жилец «${login}», долг ${debt} ₽ записан`, 'success');
            row.remove();
            this.reload();
            this.loadImportHistory();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },

    _nfShowLegacyForm(row, fio, logId, accountType) {
        const pane = row.querySelector('[data-nf-pane="legacy"]');
        this._nfShowPane(row, 'legacy');
        pane.innerHTML = `
            <div style="display:flex; gap:6px; align-items:center;">
                <input type="text" data-nf-legacy-login placeholder="Логин жильца" style="flex:1; font-size:13px;" autocomplete="off">
                <button data-nf-action="submit-legacy" class="action-btn primary-btn" style="padding:5px 10px; font-size:12px;">
                    <i class="fa-solid fa-link"></i> Привязать
                </button>
            </div>
            <div style="font-size:11px; color:var(--text-secondary); margin-top:4px;">
                Подходит когда знаешь точный логин жильца — поиск по точному совпадению.
            </div>
        `;
    },

    async _nfSubmitLegacy(row, fio, logId, accountType) {
        const { debt, overpayment } = this._nfGetSums(row);
        const pane = row.querySelector('[data-nf-pane="legacy"]');
        const login = pane.querySelector('[data-nf-legacy-login]').value.trim();
        if (!login) return toast('Укажите логин', 'warning');
        try {
            const userSearch = await api.get(`/users?page=1&limit=5&search=${encodeURIComponent(login)}`);
            const exact = (userSearch.items || []).find(u => u.username.toLowerCase() === login.toLowerCase());
            if (!exact) return toast(`Жилец «${login}» не найден`, 'error');

            const fd = new FormData();
            fd.append('fio', fio);
            fd.append('user_id', String(exact.id));
            fd.append('debt', String(debt));
            fd.append('overpayment', String(overpayment));
            await api.post(`/financier/debts/import-history/${logId}/reassign`, fd);
            toast(`Привязано: ${fio} → ${login}`, 'success');
            row.remove();
            this.reload();
            this.loadImportHistory();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },

    closeNotFoundModal() {
        this.dom.notFoundModal?.classList.remove('open');
    },

    async resetUserBalance(userId, username) {
        if (!await showConfirm(
            `Сбросить баланс жильца «${username}»?\n\n` +
            'Будут обнулены debt_209, debt_205, overpayment_209, overpayment_205 у ВСЕХ ' +
            'его reading-ов (во всех периодах). Действие можно отменить только через ' +
            'журнал действий (audit_log).\n\n' +
            'Используйте только если после отката импорта у жильца остались зависшие сальдо.',
            { title: 'Сброс баланса', confirmText: 'Сбросить', danger: true }
        )) return;
        try {
            const res = await api.post(`/financier/users/${userId}/reset-balance`);
            if (res.status === 'noop') {
                toast(`У ${username} баланс уже пустой`, 'info');
            } else {
                toast(`Сброшено reading-ов: ${res.reset_count}`, 'success');
            }
            this.reload();
            this.loadStats();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        }
    },

    // ==========================================================================
    // DIFF МОДАЛКА — сравнение импорта с предыдущим того же счёта
    //
    // Открывается из кнопки «Diff» в строке истории импортов. Backend
    // /diff отдаёт 5 категорий жильцов; рисуем 5 collapsible-секций.
    // ==========================================================================
    async openDiffModal(logId) {
        // Overlay + skeleton сразу — чтобы юзер видел что клик сработал.
        const old = document.getElementById('debtDiffModal');
        if (old) old.remove();
        const modal = document.createElement('div');
        modal.id = 'debtDiffModal';
        modal.style.cssText = `
            position:fixed; inset:0; background:rgba(0,0,0,0.45); z-index:1000;
            display:flex; align-items:center; justify-content:center; padding:20px;`;
        modal.innerHTML = `
            <div style="background:var(--bg-card); border-radius:12px; max-width:1100px; width:100%;
                        max-height:90vh; display:flex; flex-direction:column; box-shadow:0 12px 40px rgba(0,0,0,0.3);">
                <div style="padding:14px 20px; border-bottom:1px solid var(--border-color);
                            display:flex; align-items:center; justify-content:space-between;">
                    <h3 style="margin:0; font-size:15px;">
                        <i class="fa-solid fa-code-compare" style="color:#4338ca;"></i>
                        Сравнение импорта №${logId}
                    </h3>
                    <button class="secondary-btn" data-close-diff style="padding:6px 12px;">
                        <i class="fa-solid fa-xmark"></i> Закрыть
                    </button>
                </div>
                <div id="debtDiffBody" style="padding:14px 20px; overflow:auto; flex:1;">
                    <div style="text-align:center; padding:40px; color:var(--text-secondary);">
                        <i class="fa-solid fa-spinner fa-spin"></i> Загрузка diff…
                    </div>
                </div>
            </div>`;
        document.body.appendChild(modal);
        modal.addEventListener('click', (e) => {
            if (e.target === modal || e.target.closest('[data-close-diff]')) modal.remove();
        });
        const escHandler = (e) => { if (e.key === 'Escape') { modal.remove(); document.removeEventListener('keydown', escHandler); } };
        document.addEventListener('keydown', escHandler);

        try {
            const data = await api.get(`/financier/debts/import-history/${logId}/diff`);
            this._renderDiff(data);
        } catch (e) {
            const body = document.getElementById('debtDiffBody');
            if (body) body.innerHTML = `
                <div style="padding:16px; background:#fef2f2; border:1px solid #fecaca; border-radius:8px; color:#991b1b;">
                    Ошибка загрузки: ${esc(e.message)}
                </div>`;
        }
    },

    _renderDiff(data) {
        const body = document.getElementById('debtDiffBody');
        if (!body) return;

        if (data.fatal) {
            body.innerHTML = `
                <div style="padding:30px; text-align:center; color:var(--text-secondary);">
                    <i class="fa-solid fa-circle-info" style="font-size:24px; color:#3b82f6;"></i>
                    <div style="margin-top:10px;">${esc(data.fatal)}</div>
                </div>`;
            return;
        }

        const s = data.summary || {};
        const acc = data.account_type;
        const prevDate = data.previous_started_at
            ? new Date(data.previous_started_at).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' })
            : '—';
        const curDate = data.current_started_at
            ? new Date(data.current_started_at).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' })
            : '—';

        const header = `
            <div style="margin-bottom:18px; padding:12px 14px; background:#f9fafb; border:1px solid var(--border-color); border-radius:8px; font-size:13px;">
                <div style="margin-bottom:6px;">
                    <b>Счёт ${esc(acc)}:</b> сравнение
                    <span style="color:var(--text-secondary);">№${data.previous_id} (${esc(prevDate)})</span>
                    <i class="fa-solid fa-arrow-right" style="margin:0 6px;"></i>
                    <b>№${data.current_id} (${esc(curDate)})</b>
                </div>
                <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(160px, 1fr)); gap:8px; margin-top:8px;">
                    <div style="padding:8px 10px; background:#fef2f2; border-radius:6px; border:1px solid #fecaca;">
                        <div style="font-size:11px; color:#991b1b; text-transform:uppercase;">Новые должники</div>
                        <div style="font-size:18px; font-weight:700; color:#dc2626;">${s.new_debtors_count || 0}</div>
                    </div>
                    <div style="padding:8px 10px; background:#fff7ed; border-radius:6px; border:1px solid #fed7aa;">
                        <div style="font-size:11px; color:#9a3412; text-transform:uppercase;">Долг вырос</div>
                        <div style="font-size:18px; font-weight:700; color:#ea580c;">${s.debt_grew_count || 0}</div>
                        <div style="font-size:11px; color:var(--text-secondary);">+${fmtMoney(s.sum_new_and_grew || 0)}</div>
                    </div>
                    <div style="padding:8px 10px; background:#f0fdf4; border-radius:6px; border:1px solid #bbf7d0;">
                        <div style="font-size:11px; color:#166534; text-transform:uppercase;">Долг упал</div>
                        <div style="font-size:18px; font-weight:700; color:#16a34a;">${s.debt_dropped_count || 0}</div>
                        <div style="font-size:11px; color:var(--text-secondary);">−${fmtMoney(s.sum_dropped || 0)}</div>
                    </div>
                    <div style="padding:8px 10px; background:#ecfdf5; border-radius:6px; border:1px solid #a7f3d0;">
                        <div style="font-size:11px; color:#065f46; text-transform:uppercase;">Долг закрыт</div>
                        <div style="font-size:18px; font-weight:700; color:#10b981;">${s.debt_closed_count || 0}</div>
                        <div style="font-size:11px; color:var(--text-secondary);">−${fmtMoney(s.sum_closed || 0)}</div>
                    </div>
                    <div style="padding:8px 10px; background:#ede9fe; border-radius:6px; border:1px solid #ddd6fe;">
                        <div style="font-size:11px; color:#5b21b6; text-transform:uppercase;">Новые переплаты</div>
                        <div style="font-size:18px; font-weight:700; color:#7c3aed;">${s.new_overpay_count || 0}</div>
                    </div>
                </div>
            </div>`;

        const sec = (title, items, kind) => {
            if (!items || !items.length) return '';
            const colorMap = {
                new_debtors: '#dc2626',
                debt_grew:   '#ea580c',
                debt_dropped:'#16a34a',
                debt_closed: '#10b981',
                new_overpay: '#7c3aed',
            };
            const c = colorMap[kind] || '#6b7280';
            const rows = items.map(it => {
                const valueCell = kind === 'new_overpay'
                    ? `<td style="text-align:right; font-weight:600; color:${c};">${fmtMoney(it.overpayment)}</td>`
                    : `<td style="text-align:right; color:var(--text-secondary);">${fmtMoney(it.prev_debt)}</td>
                       <td style="text-align:right; font-weight:600;">${fmtMoney(it.current_debt)}</td>
                       <td style="text-align:right; font-weight:600; color:${c};">${it.delta >= 0 ? '+' : ''}${fmtMoney(it.delta)}</td>`;
                return `
                    <tr style="border-bottom:1px solid var(--border-color);">
                        <td style="padding:6px 10px;">${esc(it.username)}</td>
                        <td style="padding:6px 10px; color:var(--text-secondary); font-size:11px;">${esc(it.room_label)}</td>
                        ${valueCell}
                    </tr>`;
            }).join('');
            const headers = kind === 'new_overpay'
                ? '<th style="text-align:left; padding:6px 10px;">Жилец</th><th style="text-align:left; padding:6px 10px;">Комната</th><th style="text-align:right; padding:6px 10px;">Переплата</th>'
                : '<th style="text-align:left; padding:6px 10px;">Жилец</th><th style="text-align:left; padding:6px 10px;">Комната</th><th style="text-align:right; padding:6px 10px;">Было</th><th style="text-align:right; padding:6px 10px;">Стало</th><th style="text-align:right; padding:6px 10px;">Δ</th>';
            return `
                <details style="margin-bottom:14px; border:1px solid var(--border-color); border-radius:8px; overflow:hidden;" open>
                    <summary style="padding:10px 14px; cursor:pointer; background:${c}11; color:${c}; font-weight:600; font-size:13px;">
                        ${esc(title)} (${items.length})
                    </summary>
                    <table style="width:100%; border-collapse:collapse; font-size:12px;">
                        <thead style="background:var(--bg-page); font-size:11px; color:var(--text-secondary); text-transform:uppercase;">
                            <tr>${headers}</tr>
                        </thead>
                        <tbody>${rows}</tbody>
                    </table>
                </details>`;
        };

        body.innerHTML = header
            + sec('Новые должники', data.new_debtors, 'new_debtors')
            + sec('Долг вырос', data.debt_grew, 'debt_grew')
            + sec('Долг упал', data.debt_dropped, 'debt_dropped')
            + sec('Долг закрыт', data.debt_closed, 'debt_closed')
            + sec('Появились переплаты', data.new_overpay, 'new_overpay');

        // Если все секции пусты — показать «всё то же самое»
        if (!data.new_debtors?.length && !data.debt_grew?.length
            && !data.debt_dropped?.length && !data.debt_closed?.length
            && !data.new_overpay?.length) {
            body.innerHTML = header + `
                <div style="text-align:center; padding:40px; color:var(--text-secondary);">
                    <i class="fa-solid fa-equals" style="font-size:24px; color:#10b981;"></i>
                    <div style="margin-top:10px;">Изменений нет — суммы совпадают с прошлым импортом.</div>
                </div>`;
        }
    },

    // ==========================================================================
    // КАРТОЧКА ЖИЛЬЦА — полная раскладка долга по клику на ФИО (Bug AI)
    // ==========================================================================
    /** Модалка по клику на ФИО в таблице «Долги 1С».
     *  Показывает построчно по 209 и 205:
     *    Был долг (начало) → Доначислено → Оплачено → Стало (конец)
     *  + быстрые действия: 📊 история через все импорты, 🔍 поиск в архивах,
     *  ✏ корректировка, 🧹 сброс баланса.
     *
     *  Аргумент u — объект из таблицы со всеми полями (debt_209, obor_*, etc).
     */
    openUserCard(u) {
        document.getElementById('debtUserCardModal')?.remove();
        const d209 = parseFloat(u.debt_209 || 0), o209 = parseFloat(u.overpayment_209 || 0);
        const d205 = parseFloat(u.debt_205 || 0), o205 = parseFloat(u.overpayment_205 || 0);
        const od209 = parseFloat(u.obor_debit_209 || 0), oc209 = parseFloat(u.obor_credit_209 || 0);
        const od205 = parseFloat(u.obor_debit_205 || 0), oc205 = parseFloat(u.obor_credit_205 || 0);
        // Старт = end + Кр_оборот − Дт_оборот (обратное вычисление по дебетовому счёту 209/205).
        const start209 = d209 + oc209 - od209;
        const start205 = d205 + oc205 - od205;
        const room = u.room ? `${u.room.dormitory_name || '—'} / ${u.room.room_number || '—'}` : '—';

        const f = (v) => {
            const abs = Math.abs(v);
            if (abs < 0.005) return '0,00';
            return v.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
        };

        // Helper: рендерит одну секцию счёта (209 или 205) с раскладкой.
        const accountSection = (label, color, startD, endD, oborD, oborC, endO) => {
            const hadDebt = startD > 0.005;
            const hasDebtNow = endD > 0.005;
            const hasOverpayNow = endO > 0.005;
            const noMovement = oborD < 0.005 && oborC < 0.005;
            const noDebtNoMovement = !hadDebt && !hasDebtNow && !hasOverpayNow && noMovement;

            let verdictText, verdictColor, verdictIcon;
            if (noDebtNoMovement) {
                verdictText = 'нет данных из 1С';
                verdictColor = '#9ca3af';
                verdictIcon = '·';
            } else if (hadDebt && !hasDebtNow && oborC > 0) {
                verdictText = 'погашен полностью';
                verdictColor = '#15803d';
                verdictIcon = '✓';
            } else if (hadDebt && hasDebtNow && oborC > 0) {
                verdictText = `оплачено частично (осталось ${f(endD)} ₽)`;
                verdictColor = '#a16207';
                verdictIcon = '⚠';
            } else if (endD > startD + 0.005) {
                verdictText = `долг вырос на ${f(endD - startD)} ₽`;
                verdictColor = '#b91c1c';
                verdictIcon = '↑';
            } else if (hasDebtNow && noMovement) {
                verdictText = 'без движения — долг не оплачивался';
                verdictColor = '#b91c1c';
                verdictIcon = '!';
            } else if (hasOverpayNow) {
                verdictText = `переплата ${f(endO)} ₽`;
                verdictColor = '#15803d';
                verdictIcon = '+';
            } else {
                verdictText = '0 ₽';
                verdictColor = '#15803d';
                verdictIcon = '✓';
            }

            const row = (k, v, vColor, vSign) => `
                <tr>
                    <td style="padding:6px 0; color:#6b7280; font-size:12px;">${k}</td>
                    <td style="padding:6px 0; text-align:right; font-weight:600; color:${vColor || '#111827'}; font-variant-numeric:tabular-nums;">${vSign || ''}${f(v)} ₽</td>
                </tr>`;

            return `
                <div style="border:1px solid var(--border-color); border-radius:8px; padding:14px; background:#fff;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
                        <h4 style="margin:0; font-size:14px; color:${color};">${label}</h4>
                        <span style="font-size:11px; padding:3px 8px; background:${verdictColor}15; color:${verdictColor}; border-radius:12px; font-weight:600;">${verdictIcon} ${verdictText}</span>
                    </div>
                    <table style="width:100%; border-collapse:collapse;">
                        ${row('Долг на начало периода', startD, startD > 0 ? '#b91c1c' : '#9ca3af')}
                        ${oborD > 0.005 ? row('+ Доначислили за период', oborD, '#b91c1c', '+') : ''}
                        ${oborC > 0.005 ? row('− Оплачено за период', oborC, '#15803d', '−') : ''}
                        <tr><td colspan="2" style="border-bottom:1px dashed #e5e7eb; padding:2px 0;"></td></tr>
                        ${row('Долг на конец периода', endD, endD > 0 ? '#b91c1c' : '#15803d')}
                        ${endO > 0.005 ? row('Переплата на конец', endO, '#15803d', '+') : ''}
                    </table>
                </div>
            `;
        };

        const modal = document.createElement('div');
        modal.id = 'debtUserCardModal';
        modal.style.cssText = 'position:fixed; inset:0; background:rgba(0,0,0,0.45); z-index:1000; display:flex; align-items:center; justify-content:center; padding:20px;';
        modal.innerHTML = `
            <div style="background:#f9fafb; border-radius:12px; max-width:720px; width:100%; max-height:90vh; display:flex; flex-direction:column; box-shadow:0 12px 40px rgba(0,0,0,0.3);">
                <div style="padding:14px 20px; border-bottom:1px solid var(--border-color); background:#fff; border-radius:12px 12px 0 0; display:flex; align-items:center; justify-content:space-between;">
                    <div>
                        <h3 style="margin:0; font-size:16px;">${esc(u.username)}</h3>
                        <div style="font-size:12px; color:#6b7280; margin-top:2px;">
                            ID ${u.id} · ${esc(room)}
                        </div>
                    </div>
                    <button data-close-card style="background:none; border:none; font-size:20px; color:#6b7280; cursor:pointer;">×</button>
                </div>
                <div style="padding:16px 20px; overflow-y:auto; flex:1; display:flex; flex-direction:column; gap:12px;">
                    ${accountSection('209 — Коммуналка', '#c0392b', start209, d209, od209, oc209, o209)}
                    ${accountSection('205 — Найм', '#d35400', start205, d205, od205, oc205, o205)}

                    <div style="font-size:11px; color:#6b7280; padding:8px 12px; background:#fffbeb; border-left:3px solid #fbbf24; border-radius:4px;">
                        💡 «Долг на начало» считается обратно: <code>конец + оплачено − доначислено</code>.
                        Если в строке «Доначислили» или «Оплачено» нет — значит в этом периоде по этому счёту движения не было.
                    </div>
                </div>
                <div style="padding:12px 20px; border-top:1px solid var(--border-color); background:#fff; border-radius:0 0 12px 12px; display:flex; gap:8px; flex-wrap:wrap; justify-content:flex-end;">
                    <button data-card-action="history" style="padding:8px 12px; background:#fff; color:#4338ca; border:1px solid #c7d2fe; border-radius:4px; cursor:pointer; font-size:12px;">
                        <i class="fa-solid fa-chart-line"></i> История через импорты
                    </button>
                    <button data-card-action="coverage" style="padding:8px 12px; background:#fff; color:#0ea5e9; border:1px solid #bae6fd; border-radius:4px; cursor:pointer; font-size:12px;">
                        <i class="fa-solid fa-magnifying-glass"></i> Найти в архивах 1С
                    </button>
                    <button data-card-action="reset" style="padding:8px 12px; background:#fff; color:#b91c1c; border:1px solid #fecaca; border-radius:4px; cursor:pointer; font-size:12px;">
                        <i class="fa-solid fa-broom"></i> Сбросить баланс
                    </button>
                    <button data-card-action="adjust" style="padding:8px 14px; background:#6366f1; color:#fff; border:none; border-radius:4px; cursor:pointer; font-size:12px; font-weight:600;">
                        <i class="fa-solid fa-pen"></i> Корректировка
                    </button>
                </div>
            </div>
        `;
        document.body.appendChild(modal);
        const close = () => modal.remove();
        modal.querySelector('[data-close-card]').addEventListener('click', close);
        modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
        modal.querySelectorAll('[data-card-action]').forEach(btn => {
            btn.addEventListener('click', () => {
                const action = btn.getAttribute('data-card-action');
                close();
                if (action === 'history') this.openUserDebtHistory(u.id, u.username);
                else if (action === 'coverage') this.openCheckCoverage(u.id, u.username);
                else if (action === 'reset') this.resetUserBalance(u.id, u.username);
                else if (action === 'adjust') this.openAdjustModal(u.id, u.username);
            });
        });
    },

    // ==========================================================================
    // ИСТОРИЯ ДОЛГОВ ЖИЛЬЦА — sparkline 209 + 205 через все импорты
    // ==========================================================================
    async openUserDebtHistory(userId, username) {
        document.getElementById('debtUserHistoryModal')?.remove();
        const modal = document.createElement('div');
        modal.id = 'debtUserHistoryModal';
        modal.style.cssText = `
            position:fixed; inset:0; background:rgba(0,0,0,0.45); z-index:1000;
            display:flex; align-items:center; justify-content:center; padding:20px;`;
        modal.innerHTML = `
            <div style="background:var(--bg-card); border-radius:12px; max-width:820px; width:100%;
                        max-height:85vh; display:flex; flex-direction:column; box-shadow:0 12px 40px rgba(0,0,0,0.3);">
                <div style="padding:14px 20px; border-bottom:1px solid var(--border-color);
                            display:flex; align-items:center; justify-content:space-between;">
                    <h3 style="margin:0; font-size:15px;">
                        <i class="fa-solid fa-chart-line" style="color:#4338ca;"></i>
                        История долгов: ${esc(username)}
                    </h3>
                    <button class="secondary-btn" data-close-uh style="padding:6px 12px;">
                        <i class="fa-solid fa-xmark"></i> Закрыть
                    </button>
                </div>
                <div id="debtUserHistoryBody" style="padding:14px 20px; overflow:auto; flex:1;">
                    <div style="text-align:center; padding:40px; color:var(--text-secondary);">
                        <i class="fa-solid fa-spinner fa-spin"></i> Загрузка…
                    </div>
                </div>
            </div>`;
        document.body.appendChild(modal);
        modal.addEventListener('click', (e) => {
            if (e.target === modal || e.target.closest('[data-close-uh]')) modal.remove();
        });
        const escHandler = (e) => { if (e.key === 'Escape') { modal.remove(); document.removeEventListener('keydown', escHandler); } };
        document.addEventListener('keydown', escHandler);

        try {
            const data = await api.get(`/financier/debts/user-debt-history/${userId}`);
            this._renderUserDebtHistory(data);
        } catch (e) {
            const body = document.getElementById('debtUserHistoryBody');
            if (body) body.innerHTML = `
                <div style="padding:16px; background:#fef2f2; border:1px solid #fecaca; border-radius:8px; color:#991b1b;">
                    Ошибка: ${esc(e.message)}
                </div>`;
        }
    },

    _renderUserDebtHistory(data) {
        const body = document.getElementById('debtUserHistoryBody');
        if (!body) return;

        if (data.fatal) {
            body.innerHTML = `
                <div style="padding:30px; text-align:center; color:var(--text-secondary);">
                    <i class="fa-solid fa-circle-info" style="font-size:24px; color:#f59e0b;"></i>
                    <div style="margin-top:10px;">${esc(data.fatal)}</div>
                </div>`;
            return;
        }
        if (!data.points || !data.points.length) {
            body.innerHTML = `
                <div style="padding:30px; text-align:center; color:var(--text-secondary);">
                    Жилец не встречался ни в одном импорте — долгов нет.
                </div>`;
            return;
        }

        // Разделяем точки на 209 / 205, рисуем 2 sparkline + table
        const points209 = data.points.filter(p => p.account_type === '209');
        const points205 = data.points.filter(p => p.account_type === '205');

        // SVG sparkline — высота 60, ширина ~600
        const renderSpark = (pts, color, account) => {
            if (!pts.length) {
                return `<div style="color:var(--text-secondary); font-size:12px; padding:14px;">${account}: данных нет</div>`;
            }
            const W = 580, H = 60, P = 20;
            const debts = pts.map(p => p.debt);
            const maxD = Math.max(...debts, 1);
            const step = pts.length > 1 ? (W - 2 * P) / (pts.length - 1) : 0;
            const polyline = pts.map((p, i) => {
                const x = P + i * step;
                const y = H - P - (p.debt / maxD) * (H - 2 * P);
                return `${x.toFixed(1)},${y.toFixed(1)}`;
            }).join(' ');
            const last = pts[pts.length - 1];
            return `
                <div style="margin-bottom:12px;">
                    <div style="font-size:12px; color:var(--text-secondary); margin-bottom:4px;">
                        Счёт <b>${account}</b>: ${pts.length} точек, max ${fmtMoney(maxD)}, последний долг <b style="color:${color};">${fmtMoney(last.debt)}</b>
                    </div>
                    <svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" style="border:1px solid var(--border-color); border-radius:6px; background:#fafafa; display:block;">
                        <polyline fill="none" stroke="${color}" stroke-width="2" points="${polyline}"/>
                        ${pts.map((p, i) => {
                            const x = P + i * step;
                            const y = H - P - (p.debt / maxD) * (H - 2 * P);
                            return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3" fill="${color}">
                                      <title>${new Date(p.started_at).toLocaleDateString('ru-RU')}: ${fmtMoney(p.debt)}</title>
                                    </circle>`;
                        }).join('')}
                    </svg>
                </div>`;
        };

        const tableRows = data.points
            .slice()
            .reverse()  // самый свежий импорт сверху
            .map(p => `
                <tr style="border-bottom:1px solid var(--border-color);">
                    <td style="padding:6px 10px; font-size:12px;">${new Date(p.started_at).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' })}</td>
                    <td style="padding:6px 10px;"><span style="background:${p.account_type === '209' ? '#dbeafe' : '#fef3c7'}; color:${p.account_type === '209' ? '#1e40af' : '#92400e'}; padding:2px 6px; border-radius:4px; font-size:11px; font-weight:600;">${p.account_type}</span></td>
                    <td style="padding:6px 10px; text-align:right; font-weight:600; color:${p.debt > 0 ? '#dc2626' : 'var(--text-secondary)'};">${p.debt > 0 ? fmtMoney(p.debt) : '—'}</td>
                    <td style="padding:6px 10px; text-align:right; color:${p.overpayment > 0 ? '#7c3aed' : 'var(--text-secondary)'};">${p.overpayment > 0 ? fmtMoney(p.overpayment) : '—'}</td>
                    <td style="padding:6px 10px; color:var(--text-secondary); font-size:11px;">${esc(p.file_name || '—')}</td>
                </tr>`).join('');

        body.innerHTML = `
            <div style="margin-bottom:10px; color:var(--text-secondary); font-size:12px;">
                Комната: <b>${esc(data.room_label || '—')}</b> ·
                ${data.points.length} ${data.points.length === 1 ? 'импорт' : (data.points.length < 5 ? 'импорта' : 'импортов')}
            </div>
            ${renderSpark(points209, '#dc2626', '209 (Коммуналка)')}
            ${renderSpark(points205, '#ea580c', '205 (Найм)')}
            <table style="width:100%; margin-top:14px; border-collapse:collapse; font-size:13px;">
                <thead style="background:var(--bg-page); font-size:11px; color:var(--text-secondary); text-transform:uppercase;">
                    <tr>
                        <th style="text-align:left; padding:6px 10px;">Дата</th>
                        <th style="text-align:left; padding:6px 10px;">Счёт</th>
                        <th style="text-align:right; padding:6px 10px;">Долг</th>
                        <th style="text-align:right; padding:6px 10px;">Переплата</th>
                        <th style="text-align:left; padding:6px 10px;">Файл</th>
                    </tr>
                </thead>
                <tbody>${tableRows}</tbody>
            </table>`;
    },
};
