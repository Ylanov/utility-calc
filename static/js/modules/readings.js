// static/js/modules/readings.js
import { api } from '../core/api.js';
import { el, toast, setLoading, showPrompt } from '../core/dom.js';
import { TableController } from '../core/table-controller.js';
// Импортируем вынесенную логику UI
import { createBadges, showHistoryModal, showImportResultModal, openApproveModal } from './readings-ui.js';

export const ReadingsModule = {
    table: null,

    init() {
        this.cacheDOM();

        if (!this.isInitialized) {
            this.bindEvents();
            this.isInitialized = true;
        }

        this.loadActivePeriod();
        this.initTable();
    },

    cacheDOM() {
        this.dom = {
            btnRefresh: document.getElementById('btnRefreshReadings'),
            btnBulk: document.getElementById('btnBulkApprove'),
            filterCheckbox: document.getElementById('filterAnomalies'),
            periodActive: document.getElementById('periodActiveState'),
            periodClosed: document.getElementById('periodClosedState'),
            periodLabel: document.getElementById('activePeriodLabel'),
            btnImport: document.getElementById('btnImportReadings'),
            inputImport: document.getElementById('importReadingsFile')
        };
    },

    bindEvents() {
        if (this.dom.btnRefresh) this.dom.btnRefresh.addEventListener('click', () => this.table.refresh());
        if (this.dom.btnImport) this.dom.btnImport.addEventListener('click', () => this.importReadings());
        if (this.dom.btnBulk) this.dom.btnBulk.addEventListener('click', () => this.bulkApprove());

        if (this.dom.filterCheckbox) {
            this.dom.filterCheckbox.addEventListener('change', () => {
                if (this.table) {
                    this.table.state.page = 1;
                    this.table.load();
                }
            });
        }
    },

    async loadActivePeriod() {
        try {
            const data = await api.get('/admin/periods/active');
            if (data && data.name) {
                if (this.dom.periodActive) this.dom.periodActive.style.display = 'flex';
                if (this.dom.periodClosed) this.dom.periodClosed.style.display = 'none';
                if (this.dom.periodLabel) this.dom.periodLabel.textContent = data.name;
            } else {
                if (this.dom.periodActive) this.dom.periodActive.style.display = 'none';
                if (this.dom.periodClosed) this.dom.periodClosed.style.display = 'flex';
            }
        } catch (e) {
            console.warn("Ошибка проверки периода", e);
        }
    },

    initTable() {
        this.table = new TableController({
            endpoint: '/admin/readings',
            dom: { tableBody: 'readingsTableBody', prevBtn: 'btnPrev', nextBtn: 'btnNext', pageInfo: 'pageIndicator' },

            getExtraParams: () => ({ anomalies_only: this.dom.filterCheckbox?.checked || false }),

            renderRow: (r) => {
                const totalCost = r.total_cost ?? 0;

                let editBadge = null;
                if (r.edit_count > 1 && r.edit_history && r.edit_history.length > 0) {
                    const lastEdit = r.edit_history[r.edit_history.length - 1].date;
                    editBadge = el('span', {
                        title: `Последняя правка: ${lastEdit}`,
                        style: { marginLeft: '8px', fontSize: '11px', background: '#fef08a', color: '#b45309', padding: '2px 6px', borderRadius: '12px', fontWeight: 'bold', cursor: 'help' }
                    }, `⚠️ Изменено: ${r.edit_count} раз`);
                }

                const statusCell = el('td', {});
                let scoreColor = '#10b981', scoreText = 'Низкий риск';
                if (r.anomaly_score >= 80) { scoreColor = '#ef4444'; scoreText = 'Критичный риск'; }
                else if (r.anomaly_score >= 40) { scoreColor = '#f59e0b'; scoreText = 'Средний риск'; }

                if (r.anomaly_flags === 'PENDING') {
                    statusCell.appendChild(el('div', { style: { fontSize: '12px', color: '#6b7280', fontStyle: 'italic', marginBottom: '4px' } }, '⏳ Считаем риски...'));
                } else if (r.anomaly_score > 0 || (r.anomaly_flags && r.anomaly_flags !== '')) {
                    statusCell.appendChild(el('div', { style: { fontSize: '12px', fontWeight: 'bold', color: scoreColor, marginBottom: '4px' } }, `Рейтинг риска: ${r.anomaly_score}/100 (${scoreText})`));
                } else {
                    statusCell.appendChild(el('div', { style: { fontSize: '12px', color: '#10b981', marginBottom: '4px' } }, '✅ Норма'));
                }

                statusCell.appendChild(createBadges(r.anomaly_details, r.anomaly_flags));

                return el('tr', {},
                    el('td', {},
                        el('div', { style: { fontWeight: '600', display: 'flex', alignItems: 'center' } }, r.username, editBadge),
                        el('div', { style: { fontSize: '11px', color: '#888' } }, r.dormitory || 'Общ. не указано')
                    ),
                    statusCell,
                    el('td', { class: 'text-right' }, Number(r.cur_hot).toFixed(3)),
                    el('td', { class: 'text-right' }, Number(r.cur_cold).toFixed(3)),
                    el('td', { class: 'text-right' }, Number(r.cur_elect).toFixed(3)),
                    el('td', { class: 'text-right', style: { color: '#27ae60', fontWeight: 'bold' } }, `${Number(totalCost).toFixed(2)} ₽`),
                    el('td', { class: 'text-center' },
                        el('button', {
                            class: 'btn-icon btn-history', title: 'История правок жильцом',
                            style: { marginRight: '5px', background: '#f3f4f6', borderColor: '#d1d5db' },
                            onclick: () => showHistoryModal(r)
                        }, '🕒'),
                        el('button', {
                            class: 'btn-icon btn-adjust', title: 'Финансовая корректировка', style: { marginRight: '5px' },
                            // ИЗМЕНЕНИЕ: Передаем dormitory, чтобы в модалке был точный адрес
                            onclick: () => this.openAdjustmentModal(r.user_id, r.username, r.dormitory)
                        }, '±'),
                        el('button', {
                            class: 'btn-icon btn-check', title: 'Проверить и утвердить',
                            onclick: () => openApproveModal(r, () => this.table.refresh())
                        }, '✓')
                    )
                );
            }
        });
        this.table.init();
    },

    async importReadings() {
        const file = this.dom.inputImport?.files?.[0];
        if (!file) return toast('Сначала выберите файл Excel', 'info');

        const formData = new FormData();
        formData.append('file', file);
        setLoading(this.dom.btnImport, true, 'Загрузка...');

        try {
            const res = await api.post('/admin/readings/import', formData);
            showImportResultModal(res);
            if (this.dom.inputImport) this.dom.inputImport.value = '';
            this.table.refresh();
        } catch (e) {
            toast('Ошибка импорта: ' + e.message, 'error');
        } finally {
            setLoading(this.dom.btnImport, false, '📥 Загрузить');
        }
    },

    // ИЗМЕНЕНИЕ: Принимаем параметр dormitory для красивого заголовка
    async openAdjustmentModal(userId, username, dormitory) {
        const displayInfo = dormitory ? `${username} (${dormitory})` : username;
        const amountStr = await showPrompt(`Корректировка: ${displayInfo}`, 'Введите сумму (например -500 для скидки или 1000 для долга):');
        if (!amountStr) return;

        const amount = parseFloat(amountStr.replace(',', '.'));
        if (isNaN(amount)) return toast('Нужно ввести корректное число!', 'error');

        const desc = await showPrompt('Причина', 'Укажите основание (например: перерасчет):', 'Перерасчет');
        if (!desc) return;

        try {
            await api.post('/admin/adjustments', { user_id: userId, amount, description: desc });
            toast('Корректировка сохранена', 'success');
            this.table.refresh();
        } catch (e) {
            toast(e.message, 'error');
        }
    },

    async bulkApprove() {
        if (!confirm('Вы уверены? Это утвердит ВСЕ текущие черновики без ошибок.')) return;
        setLoading(this.dom.btnBulk, true);
        try {
            const res = await api.post('/admin/approve-bulk', {});
            toast(`Утверждено записей: ${res.approved_count}`, 'success');
            this.table.refresh();
        } catch (e) {
            toast(e.message, 'error');
        } finally {
            setLoading(this.dom.btnBulk, false);
        }
    }
};