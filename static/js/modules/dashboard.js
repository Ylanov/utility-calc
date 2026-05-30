// static/js/modules/dashboard.js
//
// Bug AC: рефакторинг дашборда — убрали «Журнал действий» (он есть отдельной
// вкладкой). Добавили:
//   • «Центр внимания» — агрегат всех алертов из улучшений за месяц
//     (Bug H/I/N/O/W/X/Y/Z + tickets + stuck-drafts). Endpoint
//     /api/admin/dashboard/attention-center делает все count'ы одним
//     SQL-snapshot.
//   • «Быстрые действия» — частые операции одним кликом (импорт 1С,
//     утвердить безопасные, auto-rebuild, аномальные дельты, переезд, долги).
//   • «Тренд начислений» — мини-чарт горизонтальных баров за N мес
//     по последним N закрытым периодам (без сторонних библиотек).
import { api } from '../core/api.js';
import { toast } from '../core/dom.js';

// Локальный escape — используется в обычных функциях рендера KPI и в
// шаблонных строках, где нет доступа к `this._escape`. Безопасно для
// текста, не для атрибутов (для атрибутов используем this._escape с
// дополнительной экранировкой кавычек — оно у нас же делается).
function esc(str) {
    if (str === null || str === undefined) return '';
    const d = document.createElement('div');
    d.textContent = String(str);
    return d.innerHTML;
}

export const DashboardModule = {
    isInitialized: false,
    // Request-id счётчики — защита от race condition. Если админ кликает
    // рефреш/открывает KPI-модалку несколько раз подряд, поздний ответ
    // не должен перезаписать свежий рендер. После каждого await проверяем id.
    _kpiReqId: 0,
    _gsheetsReqId: 0,
    _detailReqId: 0,
    _attentionReqId: 0,
    _trendReqId: 0,
    // Состояние мини-чарта тренда — выбранный диапазон месяцев.
    trendState: { months: 6 },

    init() {
        this.cacheDOM();
        if (!this.isInitialized) {
            this.bindEvents();
            this.isInitialized = true;
        }
        this.loadKPI();
        this.loadGsheetsWidget();
        this.loadAttentionCenter();
        this.loadRevenueTrend();
    },

    cacheDOM() {
        this.dom = {
            // KPI
            kpiUsers: document.getElementById('kpiUsers'),
            kpiUsersDetail: document.getElementById('kpiUsersDetail'),
            kpiRooms: document.getElementById('kpiRooms'),
            kpiRoomsDetail: document.getElementById('kpiRoomsDetail'),
            kpiSubmitted: document.getElementById('kpiSubmitted'),
            kpiSubmittedDetail: document.getElementById('kpiSubmittedDetail'),
            kpiAnomalies: document.getElementById('kpiAnomalies'),
            kpiAnomaliesDetail: document.getElementById('kpiAnomaliesDetail'),
            kpiRevenue: document.getElementById('kpiRevenue'),
            kpiRevenueDetail: document.getElementById('kpiRevenueDetail'),
            comparisonBanner: document.getElementById('comparisonBanner'),
            comparisonText: document.getElementById('comparisonText'),
            comparisonDelta: document.getElementById('comparisonDelta'),
            // GSheets widget
            gsheetsWidgetBody: document.getElementById('gsheetsWidgetBody'),
            gsheetsWidgetMeta: document.getElementById('gsheetsWidgetMeta'),
            btnGsheetsWidgetOpen: document.getElementById('btnGsheetsWidgetOpen'),
            btnGsheetsWidgetRefresh: document.getElementById('btnGsheetsWidgetRefresh'),
            // KPI detail modal
            detailModal: document.getElementById('dashboardDetailModal'),
            detailTitle: document.getElementById('dashboardDetailTitle'),
            detailBody: document.getElementById('dashboardDetailBody'),
            detailNavBtn: document.querySelector('[data-dd-navigate]'),
            // Bug AC: новые виджеты
            attentionGrid: document.getElementById('attentionGrid'),
            attentionSummary: document.getElementById('attentionSummary'),
            btnRefreshAttention: document.getElementById('btnRefreshAttention'),
            trendBody: document.getElementById('revenueTrendBody'),
            trendMonths: document.getElementById('trendMonths'),
            btnRefreshTrend: document.getElementById('btnRefreshTrend'),
            // Bug AD: «Сверить ростер»
            btnRosterDiagnose: document.getElementById('btnRosterDiagnose'),
            rosterModal: document.getElementById('rosterDiagnoseModal'),
            rosterText: document.getElementById('rosterText'),
            btnRosterRun: document.getElementById('btnRosterRun'),
            btnRosterClear: document.getElementById('btnRosterClear'),
            rosterResult: document.getElementById('rosterResult'),
            rosterSummary: document.getElementById('rosterSummary'),
        };
    },

    bindEvents() {
        // GSheets widget — refresh и переход в Операции/GSheets
        this.dom.btnGsheetsWidgetRefresh?.addEventListener('click', () => this.loadGsheetsWidget());
        this.dom.btnGsheetsWidgetOpen?.addEventListener('click', () => {
            window.location.hash = 'tools';
            setTimeout(() => {
                window.dispatchEvent(new CustomEvent('tools:open-section', {
                    detail: { section: 'gsheets' },
                }));
            }, 400);
        });

        // Кликабельные KPI — все карточки с data-dashboard-kpi.
        document.querySelectorAll('[data-dashboard-kpi]').forEach(card => {
            card.addEventListener('click', () => this.openKpiDetail(card.dataset.dashboardKpi));
        });
        this.dom.detailModal?.addEventListener('click', (e) => {
            if (e.target.closest('[data-dd-close]') || e.target === this.dom.detailModal) {
                this.closeDetailModal();
            }
        });

        // Bug AC: Центр внимания — refresh и клики по тайлам
        this.dom.btnRefreshAttention?.addEventListener('click', () => this.loadAttentionCenter());

        // Bug AC: Тренд начислений — смена диапазона и refresh
        this.dom.trendMonths?.addEventListener('change', (e) => {
            this.trendState.months = parseInt(e.target.value, 10) || 6;
            this.loadRevenueTrend();
        });
        this.dom.btnRefreshTrend?.addEventListener('click', () => this.loadRevenueTrend());

        // Bug AC: Быстрые действия — делегирование клика по контейнеру.
        // data-quick-action указывает что именно делать. Большинство —
        // навигация в вкладку, часть запускает действие на месте.
        document.querySelectorAll('[data-quick-action]').forEach(btn => {
            btn.addEventListener('click', () => this._handleQuickAction(btn.dataset.quickAction));
        });

        // Bug AD: «Сверить ростер» — модалка диагностики подач
        this.dom.btnRosterDiagnose?.addEventListener('click', () => this._openRosterModal());
        this.dom.btnRosterRun?.addEventListener('click', () => this._runRosterDiagnose());
        this.dom.btnRosterClear?.addEventListener('click', () => {
            if (this.dom.rosterText) this.dom.rosterText.value = '';
            if (this.dom.rosterResult) this.dom.rosterResult.innerHTML = '';
            if (this.dom.rosterSummary) this.dom.rosterSummary.textContent = '';
        });
        this.dom.rosterModal?.addEventListener('click', (e) => {
            if (e.target.closest('[data-roster-close]') || e.target === this.dom.rosterModal) {
                this.dom.rosterModal.classList.remove('open');
            }
        });
    },

    // =====================================================
    // Bug AD: «Сверить ростер» — открыть/запустить диагностику
    // POST /api/admin/gsheets/diagnose-roster принимает текст,
    // возвращает summary + items[]. Рендерим таблицей.
    // =====================================================
    _openRosterModal() {
        if (!this.dom.rosterModal) return;
        this.dom.rosterModal.classList.add('open');
        setTimeout(() => this.dom.rosterText?.focus(), 100);
    },

    async _runRosterDiagnose() {
        const text = (this.dom.rosterText?.value || '').trim();
        if (!text) {
            toast('Вставь список из Google Sheets', 'warning');
            return;
        }
        this.dom.rosterResult.innerHTML =
            `<div style="padding:20px; text-align:center; color:var(--text-secondary);">
                <i class="fa-solid fa-spinner fa-spin"></i> Разбор…
            </div>`;
        try {
            const data = await api.post('/admin/gsheets/diagnose-roster', { text });
            this._renderRosterResult(data);
        } catch (e) {
            this.dom.rosterResult.innerHTML =
                `<div style="padding:16px; color:var(--danger-color);">
                    Ошибка: ${this._escape(e.message || 'неизвестно')}
                </div>`;
        }
    },

    _renderRosterResult(data) {
        const s = data?.summary || {};
        const items = data?.items || [];

        if (this.dom.rosterSummary) {
            const parts = [];
            if (s.parsed) parts.push(`распарсено ${s.parsed} строк`);
            if (s.unique && s.unique !== s.parsed) parts.push(`уник. ${s.unique}`);
            if (s.found_reading) parts.push(`<span style="color:#10b981;">✅ ${s.found_reading} в системе</span>`);
            if (s.in_gsheets_pending) parts.push(`<span style="color:#3b82f6;">⏳ ${s.in_gsheets_pending} pending</span>`);
            if (s.in_gsheets_conflict) parts.push(`<span style="color:#f59e0b;">🔀 ${s.in_gsheets_conflict} конфликт</span>`);
            if (s.in_gsheets_unmatched) parts.push(`<span style="color:#ef4444;">🔍 ${s.in_gsheets_unmatched} не найдены</span>`);
            if (s.in_gsheets_rejected) parts.push(`<span style="color:#6b7280;">🗑 ${s.in_gsheets_rejected} отклонены</span>`);
            if (s.not_in_gsheets_but_user_exists) parts.push(`<span style="color:#dc2626;">⚠ ${s.not_in_gsheets_but_user_exists} не дошли до gsheets</span>`);
            if (s.user_not_found) parts.push(`<span style="color:#7c2d12;">❌ ${s.user_not_found} нет в БД</span>`);
            this.dom.rosterSummary.innerHTML = parts.join(' · ');
        }

        if (!items.length) {
            this.dom.rosterResult.innerHTML =
                `<div style="padding:16px; color:var(--text-secondary);">${this._escape(data?.warning || 'Ничего не распарсилось')}</div>`;
            return;
        }

        const statusColor = {
            approved: '#10b981',
            auto_approved: '#10b981',
            pending: '#3b82f6',
            conflict: '#f59e0b',
            unmatched: '#ef4444',
            rejected: '#6b7280',
            not_in_gsheets: '#dc2626',
        };

        const rows = items.map(it => {
            const sc = statusColor[it.gsheets_status] || '#64748b';
            const room = it.room_input ? this._escape(it.room_input) : '—';
            const matched = it.matched_room ? this._escape(it.matched_room) : '—';
            const userPart = it.username
                ? `<a href="#" data-roster-user="${it.user_id}" style="color:var(--primary-color);">${this._escape(it.username)}</a>`
                : '—';
            const gsLink = it.gsheets_id
                ? `<a href="#tools" title="Открыть в матчере" style="color:var(--primary-color);">#${it.gsheets_id}</a>`
                : '—';
            return `
                <tr>
                    <td style="padding:6px 8px; color:var(--text-secondary); font-size:11px;">${it.line_no}</td>
                    <td style="padding:6px 8px; font-weight:500;">${this._escape(it.fio)}</td>
                    <td style="padding:6px 8px; text-align:center; font-family:monospace; font-size:12px;">${room}</td>
                    <td style="padding:6px 8px;">${userPart}</td>
                    <td style="padding:6px 8px; font-size:12px; color:var(--text-secondary);">${matched}</td>
                    <td style="padding:6px 8px; text-align:center;">${gsLink}</td>
                    <td style="padding:6px 8px;">
                        <span style="color:${sc}; font-size:12px;">${this._escape(it.note)}</span>
                    </td>
                </tr>
            `;
        }).join('');

        this.dom.rosterResult.innerHTML = `
            <div style="max-height:55vh; overflow:auto; border:1px solid var(--border-color); border-radius:8px;">
                <table style="width:100%; border-collapse:collapse; font-size:13px;">
                    <thead style="background:#f8fafc; position:sticky; top:0; z-index:1;">
                        <tr>
                            <th style="padding:8px; text-align:left; width:32px;">№</th>
                            <th style="padding:8px; text-align:left;">ФИО (из вставки)</th>
                            <th style="padding:8px; width:80px;">Комната</th>
                            <th style="padding:8px; text-align:left;">Жилец в БД</th>
                            <th style="padding:8px; text-align:left;">Комната в системе</th>
                            <th style="padding:8px; width:70px;">GS row</th>
                            <th style="padding:8px; text-align:left;">Статус</th>
                        </tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
        `;
    },

    // =====================================================
    // Bug AC: QUICK ACTIONS
    // Стараемся переиспользовать существующие потоки: вместо
    // вызова API дёргаем уже-готовые кнопки/таб-переключатели.
    // =====================================================
    _handleQuickAction(action) {
        switch (action) {
            case 'import-1c':
                window.location.hash = 'debts';
                setTimeout(() => {
                    document.getElementById('debtFile209')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
                }, 300);
                break;

            case 'approve-safe': {
                // Если мы уже на дашборде — есть кнопка #btnBulkApprove ниже.
                const btn = document.getElementById('btnBulkApprove');
                if (btn) {
                    btn.click();
                    btn.scrollIntoView({ behavior: 'smooth', block: 'center' });
                } else {
                    window.location.hash = 'dashboard';
                    setTimeout(() => document.getElementById('btnBulkApprove')?.click(), 300);
                }
                break;
            }

            case 'auto-rebuild':
                window.location.hash = 'tools';
                setTimeout(() => {
                    window.dispatchEvent(new CustomEvent('tools:open-section', {
                        detail: { section: 'analyzer', subTab: 'auto-rebuild' },
                    }));
                }, 400);
                break;

            case 'anomalous-deltas':
                window.location.hash = 'tools';
                setTimeout(() => {
                    window.dispatchEvent(new CustomEvent('tools:open-section', {
                        detail: { section: 'analyzer', subTab: 'anomalous-deltas' },
                    }));
                }, 400);
                break;

            case 'move-candidates':
                window.location.hash = 'tools';
                setTimeout(() => {
                    window.dispatchEvent(new CustomEvent('tools:open-section', {
                        detail: { section: 'analyzer', subTab: 'move-candidates' },
                    }));
                }, 400);
                break;

            case 'debts-1c':
                window.location.hash = 'debts';
                break;

            default:
                toast(`Действие «${action}» пока не реализовано`, 'warning');
        }
    },

    // =====================================================
    // Bug AC: ЦЕНТР ВНИМАНИЯ
    // Один endpoint /api/admin/dashboard/attention-center
    // возвращает массив items {key, label, hint, count, severity,
    // icon, color, tab, subTab?}. Рендерим сетку плиток. Клик на
    // плитку → переключаем хеш-таб (+ опционально подвкладку).
    // =====================================================
    async loadAttentionCenter() {
        if (!this.dom.attentionGrid) return;
        const myId = ++this._attentionReqId;
        try {
            const data = await api.get('/admin/dashboard/attention-center');
            if (myId !== this._attentionReqId) return;
            this._renderAttentionCenter(data);
        } catch (e) {
            if (myId !== this._attentionReqId) return;
            this.dom.attentionGrid.innerHTML =
                `<div style="padding:18px; color:var(--danger-color); grid-column:1/-1;">
                    Ошибка загрузки алертов: ${this._escape(e.message || 'неизвестно')}
                </div>`;
        }
    },

    _renderAttentionCenter(data) {
        const items = data?.items || [];
        if (this.dom.attentionSummary) {
            const period = data?.active_period ? ` · период: ${this._escape(data.active_period)}` : '';
            this.dom.attentionSummary.innerHTML = data?.total_alerts > 0
                ? `<span style="color:#dc2626; font-weight:600;">⚠ ${data.total_alerts} активных алертов</span>${period}`
                : `<span style="color:#10b981;">✓ Все спокойно</span>${period}`;
        }

        if (!items.length) {
            this.dom.attentionGrid.innerHTML =
                `<div style="padding:18px; color:var(--text-secondary); grid-column:1/-1; text-align:center;">
                    Нет данных
                </div>`;
            return;
        }

        this.dom.attentionGrid.innerHTML = items.map(it => {
            const safeLabel = this._escape(it.label || '');
            const safeHint = this._escape(it.hint || '');
            const sev = it.severity || 'info';
            const isOk = sev === 'ok' || (it.count || 0) === 0;
            return `
                <div class="attention-tile severity-${sev}"
                     data-attention-key="${this._escape(it.key)}"
                     data-attention-tab="${this._escape(it.tab || '')}"
                     data-attention-sub="${this._escape(it.subTab || '')}"
                     title="${safeHint}">
                    <div class="at-icon" style="color:${it.color || '#64748b'};">
                        <i class="fa-solid ${this._escape(it.icon || 'fa-circle-info')}"></i>
                    </div>
                    <div class="at-body">
                        <div class="at-label">${safeLabel}</div>
                        <div class="at-hint">${safeHint}</div>
                    </div>
                    <div class="at-count" style="color:${isOk ? '#94a3b8' : (it.color || '#dc2626')};">
                        ${it.count || 0}
                    </div>
                </div>
            `;
        }).join('');

        // Делегируем клики по тайлам — переключаем вкладку (и подвкладку,
        // если задана через data-attention-sub).
        this.dom.attentionGrid.querySelectorAll('.attention-tile').forEach(tile => {
            tile.addEventListener('click', () => {
                const tab = tile.dataset.attentionTab;
                const sub = tile.dataset.attentionSub;
                if (!tab) return;
                window.location.hash = tab;
                if (sub) {
                    setTimeout(() => {
                        window.dispatchEvent(new CustomEvent('tools:open-section', {
                            detail: { section: 'analyzer', subTab: sub },
                        }));
                    }, 400);
                }
            });
        });
    },

    // =====================================================
    // Bug AC: ТРЕНД НАЧИСЛЕНИЙ
    // GET /api/admin/dashboard/revenue-trend?months=N → массив периодов.
    // Рисуем горизонтальные бары через CSS grid (.trend-row).
    // =====================================================
    async loadRevenueTrend() {
        if (!this.dom.trendBody) return;
        const months = this.trendState.months || 6;
        const myId = ++this._trendReqId;
        try {
            const data = await api.get(`/admin/dashboard/revenue-trend?months=${months}`);
            if (myId !== this._trendReqId) return;
            this._renderRevenueTrend(data);
        } catch (e) {
            if (myId !== this._trendReqId) return;
            this.dom.trendBody.innerHTML =
                `<div style="padding:16px; color:var(--danger-color);">
                    Ошибка загрузки тренда: ${this._escape(e.message || 'неизвестно')}
                </div>`;
        }
    },

    _renderRevenueTrend(data) {
        const items = data?.items || [];
        const max = Math.max(data?.max_sum || 0, 1); // защита от деления на 0
        if (!items.length) {
            this.dom.trendBody.innerHTML =
                `<div style="padding:16px; color:var(--text-secondary); text-align:center;">
                    Нет закрытых периодов
                </div>`;
            return;
        }
        const fmtMoney = (v) => Number(v || 0).toLocaleString('ru-RU',
            { minimumFractionDigits: 0, maximumFractionDigits: 0 }) + ' ₽';

        this.dom.trendBody.innerHTML = items.map(p => {
            const pct = Math.max(2, Math.round((p.approved_sum / max) * 100));
            return `
                <div class="trend-row ${p.is_active ? 'is-active' : ''}"
                     title="${this._escape(p.name)}: ${p.count} утверждённых записей">
                    <div class="tr-name">${this._escape(p.name)}${p.is_active ? ' · <span style="color:#f59e0b; font-size:11px;">тек.</span>' : ''}</div>
                    <div class="tr-bar"><div class="tr-bar-fill" style="width:${pct}%;"></div></div>
                    <div class="tr-sum">${fmtMoney(p.approved_sum)}</div>
                </div>
            `;
        }).join('');
    },

    // =====================================================
    // KPI
    // =====================================================
    async loadKPI() {
        const myId = ++this._kpiReqId;
        try {
            const data = await api.get('/admin/dashboard');
            if (myId !== this._kpiReqId) return;  // поздний ответ — игнор
            this.renderKPI(data);
        } catch (e) {
            if (myId !== this._kpiReqId) return;
            console.error('Dashboard KPI error:', e);
        }
    },

    renderKPI(data) {
        // Жильцы
        if (this.dom.kpiUsers) this.dom.kpiUsers.textContent = data.users.total;
        if (this.dom.kpiUsersDetail) {
            this.dom.kpiUsersDetail.textContent = `${data.users.with_room} с комнатой, ${data.users.without_room} без`;
        }

        // Комнаты
        if (this.dom.kpiRooms) this.dom.kpiRooms.textContent = data.rooms.total;
        if (this.dom.kpiRoomsDetail) {
            this.dom.kpiRoomsDetail.textContent = `${data.rooms.occupied} заняты, ${data.rooms.empty} свободны`;
        }

        // Показания
        if (data.period) {
            if (this.dom.kpiSubmitted) {
                this.dom.kpiSubmitted.textContent = `${data.period.submit_percent}%`;
                this.dom.kpiSubmitted.style.color = data.period.submit_percent >= 80 ? '#10b981' :
                    data.period.submit_percent >= 50 ? '#f59e0b' : '#ef4444';
            }
            if (this.dom.kpiSubmittedDetail) {
                this.dom.kpiSubmittedDetail.textContent =
                    `${data.period.submitted_rooms} из ${data.period.total_occupied_rooms} комнат (${data.period.name})`;
            }

            // Аномалии
            if (this.dom.kpiAnomalies) {
                this.dom.kpiAnomalies.textContent = data.period.anomalies;
                this.dom.kpiAnomalies.style.color = data.period.anomalies > 0 ? '#f59e0b' : '#10b981';
            }
            if (this.dom.kpiAnomaliesDetail) {
                this.dom.kpiAnomaliesDetail.textContent = `${data.period.total_drafts} черновиков всего`;
            }

            // Начислено
            if (this.dom.kpiRevenue) {
                this.dom.kpiRevenue.textContent = `${Number(data.period.approved_sum).toLocaleString('ru-RU')} ₽`;
            }
            if (this.dom.kpiRevenueDetail) {
                this.dom.kpiRevenueDetail.textContent = `${data.period.approved_count} утверждённых записей`;
            }
        } else {
            if (this.dom.kpiSubmitted) this.dom.kpiSubmitted.textContent = '—';
            if (this.dom.kpiSubmittedDetail) this.dom.kpiSubmittedDetail.textContent = 'Нет активного периода';
            if (this.dom.kpiAnomalies) this.dom.kpiAnomalies.textContent = '—';
            if (this.dom.kpiAnomaliesDetail) this.dom.kpiAnomaliesDetail.textContent = '—';
            if (this.dom.kpiRevenue) this.dom.kpiRevenue.textContent = '—';
            if (this.dom.kpiRevenueDetail) this.dom.kpiRevenueDetail.textContent = '—';
        }

        // Сравнение с прошлым периодом
        if (data.comparison && this.dom.comparisonBanner) {
            this.dom.comparisonBanner.style.display = 'block';
            const c = data.comparison;
            const color = c.delta > 0 ? '#ef4444' : c.delta < 0 ? '#10b981' : '#6b7280';
            const arrow = c.delta > 0 ? '▲' : c.delta < 0 ? '▼' : '—';
            const sign = c.delta > 0 ? '+' : '';

            this.dom.comparisonBanner.style.borderLeftColor = color;
            if (this.dom.comparisonText) {
                this.dom.comparisonText.innerHTML =
                    `<strong>${esc(c.prev_period_name)}</strong>: ${Number(c.prev_sum).toLocaleString('ru-RU')} ₽ → ` +
                    `<strong>Текущий</strong>: ${Number(c.current_sum).toLocaleString('ru-RU')} ₽`;
            }
            if (this.dom.comparisonDelta) {
                this.dom.comparisonDelta.innerHTML =
                    `<span style="color:${color}">${arrow} ${sign}${Number(c.delta).toLocaleString('ru-RU')} ₽</span>` +
                    `<div style="font-size:13px; color:${color};">${sign}${c.percent_change}%</div>`;
            }
        } else if (this.dom.comparisonBanner) {
            this.dom.comparisonBanner.style.display = 'none';
        }
    },

    // =====================================================
    // KPI DETAIL MODAL — единая модалка деталей по любой KPI-карточке.
    // Роутинг по data-dashboard-kpi: users | rooms | submissions |
    // anomalies | finance | comparison. Каждый handler использует
    // существующие endpoint-ы и не добавляет новых backend-вызовов.
    // =====================================================
    async openKpiDetail(kpi) {
        if (!this.dom.detailModal) return;
        const config = {
            users:       { title: '👥 Жильцы — распределение',        tab: 'users',   navLabel: 'Открыть вкладку «Жильцы»' },
            rooms:       { title: '🏠 Жилфонд — состояние комнат',    tab: 'housing', navLabel: 'Открыть «Жилфонд»' },
            submissions: { title: '📈 Подача показаний за период',    tab: null,      navLabel: null },
            anomalies:   { title: '⚠️ Анализатор — срабатывания',     tab: 'tools',   navLabel: 'Открыть Центр анализа' },
            finance:     { title: '💰 Финансы — должники и переплаты', tab: 'debts',   navLabel: 'Открыть «Долги 1С»' },
            comparison:  { title: '📊 Сравнение с прошлым периодом',   tab: 'tools',   navLabel: 'Открыть сравнение периодов' },
        }[kpi];
        if (!config) return;

        this.dom.detailTitle.textContent = config.title;
        this.dom.detailBody.innerHTML =
            `<div style="padding:40px; text-align:center; color:var(--text-secondary);">
                <i class="fa-solid fa-spinner fa-spin"></i> Загрузка…
            </div>`;

        // Кнопка «Открыть вкладку» — deep-link в соответствующий раздел.
        const navBtn = this.dom.detailNavBtn;
        if (navBtn) {
            if (config.tab) {
                navBtn.style.display = '';
                navBtn.querySelector('[data-dd-navigate-label]').textContent = config.navLabel;
                navBtn.onclick = () => {
                    this.closeDetailModal();
                    window.location.hash = config.tab;
                };
            } else {
                navBtn.style.display = 'none';
            }
        }
        this.dom.detailModal.classList.add('open');

        // Race guard: если админ быстро кликает разные KPI, поздний ответ
        // старого рендера мог перезаписать свежий заголовок — в модалку
        // «Жильцы» показывались бы цифры из «Комнат». Проверяем id после await.
        const myId = ++this._detailReqId;
        this._currentKpi = kpi;

        try {
            switch (kpi) {
                case 'users':       await this._renderUsersDetail(myId); break;
                case 'rooms':       await this._renderRoomsDetail(myId); break;
                case 'submissions': await this._renderSubmissionsDetail(myId); break;
                case 'anomalies':   await this._renderAnomaliesDetail(myId); break;
                case 'finance':     await this._renderFinanceDetail(myId); break;
                case 'comparison':  await this._renderComparisonDetail(myId); break;
            }
        } catch (e) {
            if (myId !== this._detailReqId) return;
            this.dom.detailBody.innerHTML =
                `<div style="padding:24px; color:var(--danger-color);">Ошибка: ${this._escape(e.message)}</div>`;
        }
    },

    closeDetailModal() {
        this.dom.detailModal?.classList.remove('open');
    },

    // Универсальный рендер KPI-блока в модалке: сетка маленьких карточек.
    _detailGrid(cards) {
        return `
            <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(140px, 1fr)); gap:10px; margin-bottom:16px;">
                ${cards.map(c => `
                    <div style="background:${c.bg || '#f9fafb'}; border:1px solid ${(c.color || '#6b7280')}22; border-radius:10px; padding:12px;">
                        <div style="font-size:11px; color:${c.color || '#6b7280'}; text-transform:uppercase; letter-spacing:.3px; margin-bottom:4px;">
                            ${this._escape(c.label)}
                        </div>
                        <div style="font-size:22px; font-weight:700; color:#111827;">${this._escape(String(c.value))}</div>
                        ${c.hint ? `<div style="font-size:11px; color:var(--text-secondary); margin-top:3px;">${this._escape(c.hint)}</div>` : ''}
                    </div>
                `).join('')}
            </div>
        `;
    },

    async _renderUsersDetail(myId) {
        const s = await api.get('/users/stats');
        if (myId !== undefined && myId !== this._detailReqId) return;
        const cards = [
            { label: 'Всего жильцов',   value: s.total_users,      color: '#2563eb', bg: '#eff6ff' },
            { label: 'С комнатой',      value: s.with_room,        color: '#10b981', bg: '#ecfdf5' },
            { label: 'Семьи',           value: s.by_resident_type?.family || 0,  color: '#7c3aed', bg: '#f5f3ff' },
            { label: 'Холостяки',       value: s.by_resident_type?.single || 0,  color: '#ea580c', bg: '#fff7ed' },
        ];
        const dorms = s.by_dormitory || [];
        const dormTable = dorms.length ? `
            <h4 style="margin:0 0 10px;"><i class="fa-solid fa-building"></i> По общежитиям</h4>
            <table style="width:100%; border-collapse:collapse; font-size:13px; background:white; border:1px solid var(--border-color); border-radius:6px; overflow:hidden;">
                <thead style="background:#f3f4f6;">
                    <tr><th style="text-align:left; padding:8px 12px;">Общежитие</th>
                        <th style="text-align:right; padding:8px 12px;">Жильцов</th></tr>
                </thead>
                <tbody>${dorms.map(d => `
                    <tr style="border-top:1px solid #f3f4f6;">
                        <td style="padding:8px 12px;">${this._escape(d.name)}</td>
                        <td style="padding:8px 12px; text-align:right; font-weight:600;">${d.count}</td>
                    </tr>`).join('')}</tbody>
            </table>
        ` : '';
        const topD = (s.top_debtors || []).slice(0, 5);
        const topO = (s.top_overpaid || []).slice(0, 5);
        const topList = (items, color, label, field) => items.length ? `
            <h4 style="margin:16px 0 8px;"><i class="fa-solid fa-chart-simple" style="color:${color};"></i> ${label}</h4>
            <ul style="list-style:none; margin:0; padding:0; font-size:13px; background:white; border:1px solid var(--border-color); border-radius:6px; overflow:hidden;">
                ${items.map(r => `
                    <li style="padding:8px 12px; border-top:1px solid #f3f4f6; display:flex; justify-content:space-between;">
                        <span>${this._escape(r.username)}</span>
                        <b style="color:${color};">${Number(r[field] || 0).toLocaleString('ru-RU', {minimumFractionDigits:2, maximumFractionDigits:2})} ₽</b>
                    </li>`).join('')}
            </ul>
        ` : '';

        this.dom.detailBody.innerHTML = `
            ${this._detailGrid(cards)}
            ${dormTable}
            ${topList(topD, '#dc2626', 'Топ-5 должников', 'amount')}
            ${topList(topO, '#7c3aed', 'Топ-5 переплатчиков', 'amount')}
        `;
    },

    async _renderRoomsDetail(myId) {
        const s = await api.get('/rooms/stats');
        if (myId !== undefined && myId !== this._detailReqId) return;
        const cards = [
            { label: 'Всего комнат',  value: s.total_rooms,       color: '#2563eb', bg: '#eff6ff' },
            { label: 'Пустых',        value: s.empty,             color: '#6b7280', bg: '#f3f4f6' },
            { label: 'Частичных',     value: s.partial,           color: '#a16207', bg: '#fef9c3' },
            { label: 'Полных',        value: s.full,              color: '#10b981', bg: '#ecfdf5' },
            { label: 'Переполнено',   value: s.overcrowded,       color: '#dc2626', bg: '#fef2f2' },
            { label: 'Заполненность', value: `${s.occupancy_pct}%`,color: '#4338ca', bg: '#eef2ff',
              hint: `${s.total_residents}/${s.total_capacity} мест` },
            { label: 'Без счётчиков', value: s.missing_meters_count, color: '#ea580c', bg: '#fff7ed',
              hint: 'не хватает серийника ГВС/ХВС/эл.' },
        ];
        const byD = s.by_dormitory || [];
        this.dom.detailBody.innerHTML = `
            ${this._detailGrid(cards)}
            ${byD.length ? `
                <h4 style="margin:0 0 10px;"><i class="fa-solid fa-building"></i> По общежитиям</h4>
                <table style="width:100%; border-collapse:collapse; font-size:13px; background:white; border:1px solid var(--border-color); border-radius:6px; overflow:hidden;">
                    <thead style="background:#f3f4f6;">
                        <tr>
                            <th style="text-align:left; padding:8px 12px;">Общежитие</th>
                            <th style="text-align:right; padding:8px 12px;">Комнат</th>
                            <th style="text-align:right; padding:8px 12px;">Жильцов / мест</th>
                            <th style="text-align:right; padding:8px 12px;">Заполн.</th>
                        </tr>
                    </thead>
                    <tbody>${byD.map(d => `
                        <tr style="border-top:1px solid #f3f4f6;">
                            <td style="padding:8px 12px;">${this._escape(d.name)}</td>
                            <td style="padding:8px 12px; text-align:right;">${d.rooms}</td>
                            <td style="padding:8px 12px; text-align:right;">${d.residents} / ${d.capacity}</td>
                            <td style="padding:8px 12px; text-align:right; color:${d.occupancy_pct >= 80 ? '#10b981' : d.occupancy_pct >= 50 ? '#f59e0b' : '#ef4444'}; font-weight:600;">${d.occupancy_pct}%</td>
                        </tr>`).join('')}</tbody>
                </table>
            ` : ''}
        `;
    },

    async _renderSubmissionsDetail(myId) {
        const data = await api.get('/admin/periods/close-preview');
        if (myId !== undefined && myId !== this._detailReqId) return;
        const pct = data.total_occupied_rooms > 0
            ? Math.round(data.rooms_with_readings / data.total_occupied_rooms * 100) : 0;
        const cards = [
            { label: 'Сдали',        value: data.rooms_with_readings,    color: '#10b981', bg: '#ecfdf5' },
            { label: 'Не сдали',     value: data.rooms_without_readings, color: '#dc2626', bg: '#fef2f2' },
            { label: 'Авто-утв.',    value: data.safe_drafts,            color: '#3b82f6', bg: '#eff6ff' },
            { label: 'Аномалий',     value: data.anomalies_count,        color: '#f59e0b', bg: '#fef3c7' },
            { label: '% сдачи',      value: `${pct}%`,                   color: '#4338ca', bg: '#eef2ff' },
        ];
        const dorms = data.dormitories || [];
        this.dom.detailBody.innerHTML = `
            ${this._detailGrid(cards)}
            ${dorms.length ? `
                <h4 style="margin:0 0 10px;"><i class="fa-solid fa-building"></i> По общежитиям</h4>
                <table style="width:100%; border-collapse:collapse; font-size:13px; background:white; border:1px solid var(--border-color); border-radius:6px; overflow:hidden;">
                    <thead style="background:#f3f4f6;">
                        <tr>
                            <th style="text-align:left; padding:8px 12px;">Общежитие</th>
                            <th style="text-align:center; padding:8px 12px; color:#10b981;">Сдали</th>
                            <th style="text-align:center; padding:8px 12px; color:#ef4444;">Не сдали</th>
                            <th style="text-align:center; padding:8px 12px;">%</th>
                        </tr>
                    </thead>
                    <tbody>${dorms.map(d => `
                        <tr style="border-top:1px solid #f3f4f6;">
                            <td style="padding:8px 12px;">${this._escape(d.name)}</td>
                            <td style="padding:8px 12px; text-align:center; font-weight:600;">${d.submitted}</td>
                            <td style="padding:8px 12px; text-align:center; color:${d.missing > 0 ? '#ef4444' : '#9ca3af'}; font-weight:600;">${d.missing}</td>
                            <td style="padding:8px 12px; text-align:center; color:${d.percent >= 80 ? '#10b981' : d.percent >= 50 ? '#f59e0b' : '#ef4444'}; font-weight:600;">${d.percent}%</td>
                        </tr>`).join('')}</tbody>
                </table>
            ` : ''}
        `;
    },

    async _renderAnomaliesDetail(myId) {
        const data = await api.get('/admin/analyzer/dashboard?days=30');
        if (myId !== undefined && myId !== this._detailReqId) return;
        const a = data.anomalies || {};
        const sev = a.by_severity || {};
        const cards = [
            { label: 'Всего за 30 дн.',   value: a.total_flagged_readings || 0, color: '#dc2626', bg: '#fef2f2' },
            { label: 'Критических',       value: sev['critical (80-100)'] || 0, color: '#ef4444', bg: '#fee2e2' },
            { label: 'Средних',           value: sev['medium (40-79)'] || 0,    color: '#f59e0b', bg: '#fef3c7' },
            { label: 'Низких',            value: sev['low (1-39)'] || 0,        color: '#10b981', bg: '#ecfdf5' },
        ];
        const top = a.top_flags || [];
        const max = top[0]?.count || 1;
        this.dom.detailBody.innerHTML = `
            ${this._detailGrid(cards)}
            <h4 style="margin:0 0 10px;"><i class="fa-solid fa-fire" style="color:#dc2626;"></i> Топ срабатываний</h4>
            ${top.length ? top.map(f => {
                const pctBar = Math.round((f.count / max) * 100);
                return `
                    <div style="display:flex; align-items:center; gap:10px; margin-bottom:6px; font-size:13px;">
                        <div style="width:200px; font-family:monospace;" title="${this._escape(f.flag)}">${this._escape(f.flag)}</div>
                        <div style="flex:1; background:#f3f4f6; border-radius:4px; height:14px; overflow:hidden;">
                            <div style="width:${pctBar}%; height:100%; background:#dc2626;"></div>
                        </div>
                        <div style="width:40px; text-align:right; font-weight:600;">${f.count}</div>
                    </div>`;
            }).join('') : `<div style="color:var(--text-secondary); font-style:italic;">За 30 дней аномалий не было.</div>`}
        `;
    },

    async _renderFinanceDetail(myId) {
        // Берём stats по долгам (быстро, одно число + разбивка).
        const s = await api.get('/financier/debts/stats');
        if (myId !== undefined && myId !== this._detailReqId) return;
        const fmtMoney = (v) => Number(v || 0).toLocaleString('ru-RU', {minimumFractionDigits:2, maximumFractionDigits:2}) + ' ₽';
        const cards = [
            { label: 'Период',        value: s.period_name || '—',  color: '#7c3aed', bg: '#f5f3ff' },
            { label: 'Должников',     value: s.debtors_count,       color: '#dc2626', bg: '#fef2f2' },
            { label: 'Переплатчиков', value: s.overpayers_count,    color: '#10b981', bg: '#ecfdf5' },
            { label: 'Сумма долга',   value: fmtMoney(s.total_debt),color: '#ea580c', bg: '#fff7ed' },
            { label: 'Сумма перепл.', value: fmtMoney(s.total_overpay), color: '#059669', bg: '#ecfdf5' },
            { label: 'Ср. долг',      value: fmtMoney(s.avg_debt_per_debtor), color: '#f59e0b', bg: '#fef3c7' },
        ];
        const dorms = s.by_dormitory || [];
        this.dom.detailBody.innerHTML = `
            ${this._detailGrid(cards)}
            ${dorms.length ? `
                <h4 style="margin:0 0 10px;"><i class="fa-solid fa-building"></i> Топ-${dorms.length} общежитий по долгу</h4>
                <table style="width:100%; border-collapse:collapse; font-size:13px; background:white; border:1px solid var(--border-color); border-radius:6px; overflow:hidden;">
                    <thead style="background:#f3f4f6;">
                        <tr>
                            <th style="text-align:left; padding:8px 12px;">Общежитие</th>
                            <th style="text-align:right; padding:8px 12px;">Должников</th>
                            <th style="text-align:right; padding:8px 12px;">Сумма</th>
                        </tr>
                    </thead>
                    <tbody>${dorms.map(d => `
                        <tr style="border-top:1px solid #f3f4f6;">
                            <td style="padding:8px 12px;">${this._escape(d.name)}</td>
                            <td style="padding:8px 12px; text-align:right;">${d.debtors}</td>
                            <td style="padding:8px 12px; text-align:right; color:#dc2626; font-weight:600;">${fmtMoney(d.total_debt)}</td>
                        </tr>`).join('')}</tbody>
                </table>
            ` : ''}
        `;
    },

    async _renderComparisonDetail() {
        // Без запросов — используем то что уже лежит в comparisonBanner.
        // Если данные не загружены — предлагаем открыть tools → compare.
        const txt = (this.dom.comparisonText?.textContent || '').trim();
        const delta = (this.dom.comparisonDelta?.textContent || '').trim();
        this.dom.detailBody.innerHTML = `
            <div style="padding:20px;">
                <div style="display:flex; justify-content:space-between; align-items:center; padding:16px 20px; background:#eff6ff; border-radius:8px; border-left:4px solid #3b82f6; margin-bottom:16px;">
                    <div>
                        <div style="font-size:13px; color:var(--text-secondary);">Сравнение двух периодов</div>
                        <div style="font-size:15px; margin-top:4px;">${this._escape(txt || '—')}</div>
                    </div>
                    <div style="font-size:22px; font-weight:700;">${this._escape(delta || '—')}</div>
                </div>
                <p class="hint-text" style="font-size:13px;">
                    Для подробного сравнения по ресурсам и общежитиям — откройте Операции → Центр анализа → «Анализ периода».
                </p>
            </div>
        `;
    },

    // =====================================================
    // ВИДЖЕТ GOOGLE SHEETS
    // Легковесная сводка на дашборде — чтобы админ видел
    // состояние буфера импорта без переключения вкладок.
    // Используется существующий endpoint /admin/gsheets/stats —
    // новых запросов на backend не добавляем.
    // =====================================================
    async loadGsheetsWidget() {
        if (!this.dom.gsheetsWidgetBody) return;
        const myId = ++this._gsheetsReqId;
        try {
            const s = await api.get('/admin/gsheets/stats');
            if (myId !== this._gsheetsReqId) return;
            this.renderGsheetsWidget(s);
        } catch (e) {
            if (myId !== this._gsheetsReqId) return;
            this.dom.gsheetsWidgetBody.innerHTML =
                `<div style="padding:12px; color:var(--danger-color); grid-column:1/-1;">Ошибка: ${this._escape(e.message)}</div>`;
            if (this.dom.gsheetsWidgetMeta) this.dom.gsheetsWidgetMeta.textContent = '';
        }
    },

    renderGsheetsWidget(s) {
        const by = s.by_status || {};
        const active = (by.pending || 0) + (by.conflict || 0) + (by.unmatched || 0);
        const auto = by.auto_approved || 0;
        const done = by.approved || 0;
        const reject = by.rejected || 0;

        // Мета-строка в шапке виджета: последний импорт + статус настройки sheet_id.
        if (this.dom.gsheetsWidgetMeta) {
            const parts = [];
            if (!s.sheet_id_configured) {
                parts.push('<span style="color:var(--danger-color);">⚠ GSHEETS_SHEET_ID не задан</span>');
            } else if (s.last_import_at) {
                const d = new Date(s.last_import_at);
                parts.push('последний sync: ' + d.toLocaleString('ru-RU', {
                    day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
                }));
            } else {
                parts.push('импортов пока не было');
            }
            if (s.auto_sync_interval_min) {
                parts.push(`авто каждые ${s.auto_sync_interval_min} мин`);
            }
            this.dom.gsheetsWidgetMeta.innerHTML = parts.join(' · ');
        }

        const chip = (bg, color, icon, value, label, big = false) => `
            <div style="background:${bg}; border:1px solid ${color}33; border-radius:10px; padding:12px; text-align:left;">
                <div style="display:flex; align-items:center; gap:6px; color:${color}; font-size:11px; text-transform:uppercase; letter-spacing:0.3px; margin-bottom:4px;">
                    <span>${icon}</span>${this._escape(label)}
                </div>
                <div style="font-size:${big ? '24' : '20'}px; font-weight:700; color:${big && value > 0 ? color : '#111827'};">${value}</div>
            </div>
        `;

        // Подсветка «требуют действия» красным, если > 0
        const activeColor = active > 0 ? '#dc2626' : '#6b7280';
        const activeBg = active > 0 ? '#fef2f2' : '#f9fafb';

        this.dom.gsheetsWidgetBody.innerHTML = [
            chip(activeBg, activeColor, '⚠️', active, 'Требуют действия', true),
            chip('#eff6ff', '#3b82f6', '⏳', by.pending || 0, 'В ожидании'),
            chip('#fef3c7', '#f59e0b', '🔀', by.conflict || 0, 'Конфликт'),
            chip('#fee2e2', '#ef4444', '🔍', by.unmatched || 0, 'Не найден'),
            chip('#ede9fe', '#8b5cf6', '🤖', auto, 'Авто-утверждено'),
            chip('#d1fae5', '#10b981', '✅', done, 'Утверждено'),
            chip('#f3f4f6', '#6b7280', '🗑', reject, 'Отклонено'),
        ].join('');
    },

    _escape(s) {
        if (s == null) return '';
        return String(s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    },
};