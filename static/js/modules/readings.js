// static/js/modules/readings.js (ФИНАЛЬНАЯ ВЕРСИЯ)
import { api } from '../core/api.js';
import { el, toast, setLoading, showPrompt } from '../core/dom.js';
import { TableController } from '../core/table-controller.js';

const ANOMALY_MAP = {
    "NEGATIVE": { color: "#c0392b", label: "Ошибка (<0)" },
    "ZERO": { color: "#f39c12", label: "Нулевой" },
    "HIGH": { color: "#e74c3c", label: "Высокий" },
    "FROZEN": { color: "#3498db", label: "Замерзший" },
    "PEERS": { color: "#9b59b6", label: "Аномалия (Группа)" },
    "IMPORTED_DRAFT": { color: "#8e44ad", label: "Импорт" }
};

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
            btnClosePeriod: document.querySelector('#periodActiveState button'),
            periodNameInput: document.getElementById('newPeriodNameInput'),
            btnOpenPeriod: document.querySelector('#periodClosedState button'),
            btnImport: document.getElementById('btnImportReadings'),
            inputImport: document.getElementById('importReadingsFile')
        };
    },

    bindEvents() {
        if (this.dom.btnRefresh) {
            this.dom.btnRefresh.addEventListener('click', () => this.table.refresh());
        }
        if (this.dom.btnImport) {
            this.dom.btnImport.addEventListener('click', () => this.importReadings());
        }

        if (this.dom.filterCheckbox) {
            this.dom.filterCheckbox.addEventListener('change', () => {
                if (this.table) {
                    this.table.state.page = 1;
                    this.table.load();
                }
            });
        }

        if (this.dom.btnBulk) this.dom.btnBulk.addEventListener('click', () => this.bulkApprove());
        if (this.dom.btnClosePeriod) this.dom.btnClosePeriod.addEventListener('click', () => this.closePeriodAction());
        if (this.dom.btnOpenPeriod) this.dom.btnOpenPeriod.addEventListener('click', () => this.openPeriodAction());
    },

    initTable() {
        this.table = new TableController({
            endpoint: '/admin/readings',

            dom: {
                tableBody: 'readingsTableBody',
                prevBtn: 'btnPrev',
                nextBtn: 'btnNext',
                pageInfo: 'pageIndicator'
            },

            getExtraParams: () => {
                return {
                    anomalies_only: this.dom.filterCheckbox.checked
                };
            },

            renderRow: (r) => {
                const totalCost = r.total_cost !== null && r.total_cost !== undefined ? r.total_cost : 0;

                return el('tr', {},
                    el('td', {},
                        el('div', { style: { fontWeight: '600' } }, r.username),
                        el('div', { style: { fontSize: '11px', color: '#888' } }, r.dormitory || 'Общ. не указано')
                    ),
                    el('td', {}, this.createBadges(r.anomaly_flags)),
                    el('td', { class: 'text-right' }, Number(r.cur_hot).toFixed(3)),
                    el('td', { class: 'text-right' }, Number(r.cur_cold).toFixed(3)),
                    el('td', { class: 'text-right' }, Number(r.cur_elect).toFixed(3)),
                    el('td', { class: 'text-right', style: { color: '#27ae60', fontWeight: 'bold' } },
                        `${Number(totalCost).toFixed(2)} ₽`
                    ),
                    el('td', { class: 'text-center' },
                        el('button', {
                            class: 'btn-icon btn-adjust',
                            title: 'Финансовая корректировка',
                            style: { marginRight: '5px' },
                            onclick: () => this.openAdjustmentModal(r.user_id, r.username)
                        }, '±'),
                        el('button', {
                            class: 'btn-icon btn-check',
                            title: 'Проверить и утвердить',
                            onclick: () => this.openApproveModal(r)
                        }, '✓')
                    )
                );
            }
        });

        this.table.init();
    },

    async importReadings() {
        const file = this.dom.inputImport.files[0];
        if (!file) {
            toast('Сначала выберите файл Excel', 'info');
            return;
        }

        const formData = new FormData();
        formData.append('file', file);

        setLoading(this.dom.btnImport, true, 'Загрузка...');

        try {
            const res = await api.post('/admin/readings/import', formData);

            // НОВОЕ: Используем красивую модалку вместо alert()
            this.showImportResultModal(res);

            this.dom.inputImport.value = '';
            this.table.refresh();
        } catch (e) {
            toast('Ошибка импорта: ' + e.message, 'error');
        } finally {
            setLoading(this.dom.btnImport, false, '📥 Загрузить');
        }
    },

    // НОВОЕ: Динамическое создание модалки для показа результатов импорта показаний
    showImportResultModal(result) {
        const hasErrors = result.errors && result.errors.length > 0;

        const overlay = el('div', { class: 'modal-overlay open', style: { zIndex: 10000 } });
        const headerTitle = hasErrors ? '⚠️ Результат импорта (Есть ошибки)' : '✅ Импорт успешно завершен';
        const headerColor = hasErrors ? '#d97706' : '#059669';

        const closeBtn = el('button', { class: 'close-icon' }, '×');
        closeBtn.onclick = () => document.body.removeChild(overlay);

        const content = el('div', { class: 'modal-form' },
            el('ul', { style: { marginBottom: '15px', paddingLeft: '20px', fontSize: '15px', color: '#374151' } },
                el('li', { style: { marginBottom: '5px' } }, `Добавлено черновиков: `, el('strong', { style: { color: '#059669'} }, String(result.added || 0))),
                el('li', {}, `Обновлено существующих: `, el('strong', { style: { color: '#2563eb'} }, String(result.updated || 0)))
            )
        );

        if (hasErrors) {
            const errorBox = el('div', {
                style: {
                    maxHeight: '250px', overflowY: 'auto', background: '#fef2f2',
                    border: '1px solid #fecaca', borderRadius: '8px', padding: '12px',
                    fontSize: '13px', color: '#991b1b', fontFamily: 'monospace'
                }
            });

            result.errors.forEach(err => {
                errorBox.appendChild(el('div', {
                    style: { marginBottom: '6px', borderBottom: '1px dashed #fca5a5', paddingBottom: '6px' }
                }, String(err)));
            });

            content.appendChild(el('h4', { style: { marginBottom: '10px', color: '#dc2626', fontSize: '14px' } }, `Ошибки (${result.errors.length}):`));
            content.appendChild(errorBox);
        }

        const btnOk = el('button', { class: 'action-btn primary-btn full-width', style: { marginTop: '20px' } }, 'Понятно, закрыть');
        btnOk.onclick = () => document.body.removeChild(overlay);
        content.appendChild(btnOk);

        const modalWindow = el('div', { class: 'modal-window', style: { width: '550px' } },
            el('div', { class: 'modal-header' },
                el('h3', { style: { color: headerColor } }, headerTitle),
                closeBtn
            ),
            content
        );

        overlay.appendChild(modalWindow);
        document.body.appendChild(overlay);
    },

    createBadges(flags) {
        if (!flags) return el('span', { style: { color: '#ccc' } }, '-');
        const container = el('div', { style: { display: 'flex', gap: '4px', flexWrap: 'wrap' } });
        flags.split(',').forEach(flag => {
            let type = "UNKNOWN";
            for (const key in ANOMALY_MAP) {
                if (flag.includes(key)) {
                    type = key;
                    break;
                }
            }
            const meta = ANOMALY_MAP[type] || { color: '#95a5a6', label: flag };
            container.appendChild(el('span', {
                title: flag,
                style: {
                    background: meta.color, color: 'white', padding: '2px 6px',
                    borderRadius: '4px', fontSize: '10px', fontWeight: 'bold', cursor: 'help'
                }
            }, meta.label));
        });
        return container;
    },

    async loadActivePeriod() {
        try {
            const data = await api.get('/admin/periods/active');
            if (data && data.name) {
                this.dom.periodActive.style.display = 'flex';
                this.dom.periodClosed.style.display = 'none';
                this.dom.periodLabel.textContent = data.name;
            } else {
                this.dom.periodActive.style.display = 'none';
                this.dom.periodClosed.style.display = 'flex';
            }
        } catch (e) { console.warn("Ошибка проверки периода", e); }
    },

    async openApproveModal(reading) {
        const modal = document.getElementById('approveModal');
        if (!modal) return;
        document.getElementById('modal_reading_id').value = reading.id;
        document.getElementById('m_username').textContent = reading.username;
        const dHot = (Number(reading.cur_hot) - Number(reading.prev_hot)).toFixed(3);
        const dCold = (Number(reading.cur_cold) - Number(reading.prev_cold)).toFixed(3);
        const dElect = (Number(reading.cur_elect) - Number(reading.prev_elect)).toFixed(3);
        document.getElementById('m_hot_usage').textContent = dHot;
        document.getElementById('m_cold_usage').textContent = dCold;
        document.getElementById('m_elect_usage').textContent = dElect;
        ['m_corr_hot', 'm_corr_cold', 'm_corr_elect', 'm_corr_sewage'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.value = 0;
        });
        modal.classList.add('open');
        const btnSubmit = document.getElementById('btnModalSubmit');
        btnSubmit.onclick = () => this.submitApproval(reading.id);
        const btnClose = document.getElementById('btnModalClose');
        btnClose.onclick = () => modal.classList.remove('open');
    },

    async submitApproval(id) {
        const btn = document.getElementById('btnModalSubmit');

        // НОВОЕ: Заменяем запятую на точку перед парсингом, чтобы избежать NaN
        const parseInput = (elId) => {
            const el = document.getElementById(elId);
            if (!el || !el.value) return 0; // Безопасный возврат 0
            return parseFloat(el.value.replace(',', '.')) || 0;
        };

        const data = {
            hot_correction: parseInput('m_corr_hot'),
            cold_correction: parseInput('m_corr_cold'),
            electricity_correction: parseInput('m_corr_elect'),
            sewage_correction: parseInput('m_corr_sewage')
        };

        setLoading(btn, true, 'Сохранение...');
        try {
            const res = await api.post(`/admin/approve/${id}`, data);
            toast(`Утверждено! Сумма: ${Number(res.new_total).toFixed(2)} ₽`, 'success');
            document.getElementById('approveModal').classList.remove('open');
            this.table.refresh();
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        } finally {
            setLoading(btn, false);
        }
    },

    async openAdjustmentModal(userId, username) {
        const amountStr = await showPrompt(`Корректировка: ${username}`, 'Введите сумму (например -500 для скидки или 1000 для долга):');
        if (!amountStr) return;

        // НОВОЕ: Защита от опечатки с запятой
        const amount = parseFloat(amountStr.replace(',', '.'));
        if (isNaN(amount)) {
            toast('Нужно ввести корректное число!', 'error');
            return;
        }

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
    },

    async closePeriodAction() {
        if (!confirm('Закрыть месяц? Будет произведен авто-расчет для всех должников. Это может занять время.')) return;

        const btn = this.dom.btnClosePeriod;
        setLoading(btn, true, 'Запуск...');

        try {
            const res = await api.post('/admin/periods/close', {});

            if (res.task_id) {
                toast('Процесс закрытия запущен. Пожалуйста, подождите...', 'info');
                await this.pollCloseTask(res.task_id, btn);
            } else {
                toast(`Месяц закрыт. Авто-расчетов: ${res.auto_generated || 0}`, 'success');
                setTimeout(() => window.location.reload(), 1500);
            }

        } catch (e) {
            toast(e.message, 'error');
            setLoading(btn, false, '🔒 Закрыть месяц');
        }
    },

    async pollCloseTask(taskId, btn) {
        const maxAttempts = 60;
        let attempts = 0;

        const check = async () => {
            attempts++;
            if (attempts > maxAttempts) {
                setLoading(btn, false, '🔒 Закрыть месяц');
                toast('Время ожидания истекло. Проверьте статус позже.', 'warning');
                return;
            }

            try {
                const statusData = await api.get(`/admin/tasks/${taskId}`);

                if (statusData.state === 'PENDING' || statusData.state === 'STARTED' || statusData.status === 'processing') {
                    btn.innerText = `Обработка... ${attempts}с`;
                    setTimeout(check, 2000);
                }
                else if (statusData.status === 'done' || statusData.state === 'SUCCESS') {
                    const result = statusData.result || {};

                    if (result.status === 'error') {
                        throw new Error(result.message);
                    }

                    toast(`Месяц успешно закрыт! Авто-расчетов: ${result.auto_generated || 0}`, 'success');
                    setLoading(btn, false, 'Готово');

                    setTimeout(() => window.location.reload(), 1500);
                }
                else if (statusData.state === 'FAILURE') {
                    throw new Error(statusData.error || 'Ошибка выполнения задачи');
                }
            } catch (e) {
                setLoading(btn, false, '🔒 Закрыть месяц');
                toast('Ошибка при закрытии: ' + e.message, 'error');
            }
        };

        check();
    },

    async openPeriodAction() {
        const name = this.dom.periodNameInput.value.trim();
        if (!name) {
            toast('Введите название месяца!', 'info');
            return;
        }
        setLoading(this.dom.btnOpenPeriod, true);
        try {
            await api.post('/admin/periods/open', { name });
            toast(`Период "${name}" открыт`, 'success');
            setTimeout(() => window.location.reload(), 1500);
        } catch (e) {
            toast(e.message, 'error');
            setLoading(this.dom.btnOpenPeriod, false);
        }
    }
};