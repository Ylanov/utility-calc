// static/js/modules/readings.js

import { api } from '../core/api.js';
import { el, toast, setLoading, showPrompt } from '../core/dom.js';
import { TableController } from '../core/table-controller.js';

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

        if (this.dom.btnBulk) {
            this.dom.btnBulk.addEventListener('click', () => this.bulkApprove());
        }
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
                    anomalies_only: this.dom.filterCheckbox?.checked || false
                };
            },

            renderRow: (r) => {
                const totalCost = r.total_cost ?? 0;

                // --- Бейдж изменения показаний ---
                let editBadge = null;
                if (r.edit_count > 1 && r.edit_history && r.edit_history.length > 0) {
                    const lastEdit = r.edit_history[r.edit_history.length - 1].date;
                    editBadge = el('span', {
                        title: `Последняя правка: ${lastEdit}`,
                        style: {
                            marginLeft: '8px', fontSize: '11px', background: '#fef08a', color: '#b45309',
                            padding: '2px 6px', borderRadius: '12px', fontWeight: 'bold', cursor: 'help'
                        }
                    }, `⚠️ Изменено: ${r.edit_count} раз`);
                }

                // --- Формирование колонки статуса с Risk Score ---
                const statusCell = el('td', {});

                let scoreColor = '#10b981'; // Зеленый (Норма)
                let scoreText = 'Низкий риск';

                if (r.anomaly_score >= 80) {
                    scoreColor = '#ef4444'; // Красный
                    scoreText = 'Критичный риск';
                } else if (r.anomaly_score >= 40) {
                    scoreColor = '#f59e0b'; // Оранжевый
                    scoreText = 'Средний риск';
                }

                if (r.anomaly_flags === 'PENDING') {
                    statusCell.appendChild(el('div', {
                        style: { fontSize: '12px', color: '#6b7280', fontStyle: 'italic', marginBottom: '4px' }
                    }, '⏳ Считаем риски...'));
                } else if (r.anomaly_score > 0 || (r.anomaly_flags && r.anomaly_flags !== '')) {
                    statusCell.appendChild(el('div', {
                        style: { fontSize: '12px', fontWeight: 'bold', color: scoreColor, marginBottom: '4px' }
                    }, `Рейтинг риска: ${r.anomaly_score}/100 (${scoreText})`));
                } else {
                    statusCell.appendChild(el('div', {
                        style: { fontSize: '12px', color: '#10b981', marginBottom: '4px' }
                    }, '✅ Норма'));
                }

                // Добавляем сами бейджи аномалий под Risk Score
                statusCell.appendChild(this.createBadges(r.anomaly_details, r.anomaly_flags));

                return el('tr', {},
                    el('td', {},
                        el('div', { style: { fontWeight: '600', display: 'flex', alignItems: 'center' } },
                            r.username,
                            editBadge
                        ),
                        el('div', { style: { fontSize: '11px', color: '#888' } }, r.dormitory || 'Общ. не указано')
                    ),
                    statusCell,
                    el('td', { class: 'text-right' }, Number(r.cur_hot).toFixed(3)),
                    el('td', { class: 'text-right' }, Number(r.cur_cold).toFixed(3)),
                    el('td', { class: 'text-right' }, Number(r.cur_elect).toFixed(3)),
                    el('td', { class: 'text-right', style: { color: '#27ae60', fontWeight: 'bold' } },
                        `${Number(totalCost).toFixed(2)} ₽`
                    ),
                    el('td', { class: 'text-center' },
                        el('button', {
                            class: 'btn-icon btn-history',
                            title: 'История правок жильцом',
                            style: { marginRight: '5px', background: '#f3f4f6', borderColor: '#d1d5db' },
                            onclick: () => this.showHistoryModal(r)
                        }, '🕒'),
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

    showHistoryModal(reading) {
        const overlay = el('div', { class: 'modal-overlay open', style: { zIndex: 10000 } });
        const closeBtn = el('button', { class: 'close-icon' }, '×');
        closeBtn.onclick = () => document.body.removeChild(overlay);

        const content = el('div', { class: 'modal-form' });

        if (!reading.edit_history || reading.edit_history.length === 0) {
            content.appendChild(el('p', { style: { textAlign: 'center', color: '#6b7280', padding: '20px 0' } }, 'Жилец передал показания с первого раза. Истории правок нет.'));
        } else {
            const timeline = el('div', { style: { display: 'flex', flexDirection: 'column', gap: '10px' } });

            reading.edit_history.forEach((h, index) => {
                const isLast = index === reading.edit_history.length - 1;

                const item = el('div', {
                    style: {
                        padding: '12px', background: isLast ? '#eff6ff' : '#f9fafb',
                        borderLeft: isLast ? '3px solid #3b82f6' : '3px solid #d1d5db',
                        borderRadius: '6px', fontSize: '13px'
                    }
                },
                    el('div', { style: { fontWeight: 'bold', marginBottom: '5px', color: '#374151' } },
                        isLast ? `🗓️ ${h.date} (Предпоследний вариант)` : `🗓️ ${h.date}`
                    ),
                    el('div', { style: { color: '#4b5563', fontFamily: 'monospace', fontSize: '14px' } },
                        `ГВС: ${h.hot} | ХВС: ${h.cold} | Свет: ${h.elect}`
                    )
                );
                timeline.appendChild(item);
            });

            const currentItem = el('div', {
                style: {
                    padding: '12px', background: '#ecfdf5',
                    borderLeft: '3px solid #10b981',
                    borderRadius: '6px', fontSize: '13px', marginTop: '10px'
                }
            },
                el('div', { style: { fontWeight: 'bold', marginBottom: '5px', color: '#065f46' } }, `✅ Текущие показания (В таблице)`),
                el('div', { style: { color: '#065f46', fontFamily: 'monospace', fontSize: '14px' } },
                    `ГВС: ${reading.cur_hot} | ХВС: ${reading.cur_cold} | Свет: ${reading.cur_elect}`
                )
            );
            timeline.appendChild(currentItem);

            content.appendChild(timeline);
        }

        const btnOk = el('button', {
            class: 'action-btn primary-btn full-width', style: { marginTop: '20px' }
        }, 'Закрыть историю');
        btnOk.onclick = () => document.body.removeChild(overlay);
        content.appendChild(btnOk);

        const modalWindow = el('div', { class: 'modal-window', style: { width: '450px' } },
            el('div', { class: 'modal-header' },
                el('h3', {}, `История: ${reading.username}`),
                closeBtn
            ),
            content
        );

        overlay.appendChild(modalWindow);
        document.body.appendChild(overlay);
    },

    async importReadings() {
        const file = this.dom.inputImport?.files?.[0];

        if (!file) {
            toast('Сначала выберите файл Excel', 'info');
            return;
        }

        const formData = new FormData();
        formData.append('file', file);

        setLoading(this.dom.btnImport, true, 'Загрузка...');

        try {
            const res = await api.post('/admin/readings/import', formData);
            this.showImportResultModal(res);

            if (this.dom.inputImport) {
                this.dom.inputImport.value = '';
            }

            this.table.refresh();
        } catch (e) {
            toast('Ошибка импорта: ' + e.message, 'error');
        } finally {
            setLoading(this.dom.btnImport, false, '📥 Загрузить');
        }
    },

    showImportResultModal(result) {
        const hasErrors = result.errors && result.errors.length > 0;

        const overlay = el('div', { class: 'modal-overlay open', style: { zIndex: 10000 } });

        const headerTitle = hasErrors
            ? '⚠️ Результат импорта (Есть ошибки)'
            : '✅ Импорт успешно завершен';

        const headerColor = hasErrors ? '#d97706' : '#059669';

        const closeBtn = el('button', { class: 'close-icon' }, '×');
        closeBtn.onclick = () => document.body.removeChild(overlay);

        const content = el('div', { class: 'modal-form' },
            el('ul', {
                style: {
                    marginBottom: '15px',
                    paddingLeft: '20px',
                    fontSize: '15px',
                    color: '#374151'
                }
            },
                el('li', { style: { marginBottom: '5px' } },
                    `Добавлено черновиков: `,
                    el('strong', { style: { color: '#059669' } }, String(result.added || 0))
                ),
                el('li', {},
                    `Обновлено существующих: `,
                    el('strong', { style: { color: '#2563eb' } }, String(result.updated || 0))
                )
            )
        );

        if (hasErrors) {
            const errorBox = el('div', {
                style: {
                    maxHeight: '250px',
                    overflowY: 'auto',
                    background: '#fef2f2',
                    border: '1px solid #fecaca',
                    borderRadius: '8px',
                    padding: '12px',
                    fontSize: '13px',
                    color: '#991b1b',
                    fontFamily: 'monospace'
                }
            });

            result.errors.forEach(err => {
                errorBox.appendChild(el('div', {
                    style: {
                        marginBottom: '6px',
                        borderBottom: '1px dashed #fca5a5',
                        paddingBottom: '6px'
                    }
                }, String(err)));
            });

            content.appendChild(
                el('h4', {
                    style: { marginBottom: '10px', color: '#dc2626', fontSize: '14px' }
                }, `Ошибки (${result.errors.length}):`)
            );

            content.appendChild(errorBox);
        }

        const btnOk = el('button', {
            class: 'action-btn primary-btn full-width',
            style: { marginTop: '20px' }
        }, 'Понятно, закрыть');

        btnOk.onclick = () => document.body.removeChild(overlay);

        content.appendChild(btnOk);

        const modalWindow = el('div', {
            class: 'modal-window',
            style: { width: '550px' }
        },
            el('div', { class: 'modal-header' },
                el('h3', { style: { color: headerColor } }, headerTitle),
                closeBtn
            ),
            content
        );

        overlay.appendChild(modalWindow);
        document.body.appendChild(overlay);
    },

    createBadges(details, rawFlags) {
        const container = el('div', {
            style: { display: 'flex', gap: '4px', flexWrap: 'wrap' }
        });

        // Используем детальную информацию от бэкенда (из новой V2 логики)
        if (details && details.length > 0) {
            details.forEach(d => {
                container.appendChild(el('span', {
                    title: d.message,
                    style: {
                        background: d.color || '#95a5a6',
                        color: 'white',
                        padding: '2px 6px',
                        borderRadius: '4px',
                        fontSize: '10px',
                        fontWeight: 'bold',
                        cursor: 'help'
                    }
                }, d.code)); // Код (например SPIKE_HOT)
            });
            return container;
        }

        // Fallback для старых данных
        if (rawFlags && rawFlags !== 'PENDING') {
            rawFlags.split(',').forEach(flag => {
                container.appendChild(el('span', {
                    style: {
                        background: '#9ca3af',
                        color: 'white',
                        padding: '2px 6px',
                        borderRadius: '4px',
                        fontSize: '10px',
                        fontWeight: 'bold'
                    }
                }, flag));
            });
        }

        return container;
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
        document.getElementById('m_elect_usage').textContent = dElect;['m_corr_hot', 'm_corr_cold', 'm_corr_elect', 'm_corr_sewage'].forEach(id => {
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

        const parseInput = (elId) => {
            const el = document.getElementById(elId);
            if (!el || !el.value) return 0;
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
    }
};