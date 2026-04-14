// static/js/modules/summary.js
import { api } from '../core/api.js';
import { el, setLoading, toast } from '../core/dom.js';

// Утилита для экранирования HTML
function esc(str) {
    if (str === null || str === undefined) return '';
    const div = document.createElement('div');
    div.textContent = String(str);
    return div.innerHTML;
}

export const SummaryModule = {
    isInitialized: false,
    periodsCache: [],
    state: {
        selectedPeriodId: null,
        controller: null,
        pollTimer: null
    },

    dom: {},

    init() {
        if (!this.dom.container) {
            this.cacheDOM();
            if (!this.isInitialized) {
                this.bindEvents();
                this.isInitialized = true;
            }
        }
        this.loadPeriods();
    },

    cacheDOM() {
        this.dom = {
            container: document.getElementById('summaryContainer'),
            periodSelector: document.getElementById('summaryPeriodSelector'),
            btnRefresh: document.getElementById('btnRefreshSummary'),
            btnExcel: document.getElementById('btnDownloadExcel'),
            btnZip: document.getElementById('btnDownloadZip'),
            // Новые элементы: Предпросмотр закрытия
            closePreviewCard: document.getElementById('closePreviewCard'),
            closePreviewContainer: document.getElementById('closePreviewContainer'),
            btnLoadPreview: document.getElementById('btnLoadPreview'),
            // Новые элементы: Сравнение периодов
            comparePeriodA: document.getElementById('comparePeriodA'),
            comparePeriodB: document.getElementById('comparePeriodB'),
            btnCompare: document.getElementById('btnCompare'),
            compareContainer: document.getElementById('compareContainer')
        };
    },

    bindEvents() {
        if (this.dom.btnRefresh) this.dom.btnRefresh.addEventListener('click', () => this.loadData());
        if (this.dom.btnExcel) this.dom.btnExcel.addEventListener('click', () => this.downloadExcel());
        if (this.dom.btnZip) this.dom.btnZip.addEventListener('click', () => this.downloadZip());
        // Новые обработчики
        if (this.dom.btnLoadPreview) this.dom.btnLoadPreview.addEventListener('click', () => this.loadClosePreview());
        if (this.dom.btnCompare) this.dom.btnCompare.addEventListener('click', () => this.runComparison());
    },

    async loadPeriods() {
        if (!this.dom.periodSelector) return;
        this.dom.periodSelector.innerHTML = '<span>Загрузка периодов...</span>';

        try {
            const periods = await api.get('/admin/periods/history');
            this.periodsCache = periods || [];
            this.dom.periodSelector.innerHTML = '';

            const select = el('select', {
                onchange: (e) => {
                    this.state.selectedPeriodId = e.target.value;
                    this.loadData();
                }
            });

            if (!periods || !periods.length) {
                this.dom.container.innerHTML = '<div style="text-align:center; padding:40px;">Нет доступных периодов.</div>';
                return;
            }

            periods.forEach(p => {
                select.appendChild(el('option', { value: p.id }, `${p.name}${p.is_active ? ' (Активный)' : ''}`));
            });
            this.dom.periodSelector.appendChild(select);

            this.state.selectedPeriodId = periods[0].id;
            this.loadData();

            // Заполняем селекторы сравнения и определяем видимость предпросмотра
            this.populateCompareSelectors(periods);
            this.updateClosePreviewVisibility(periods);

        } catch (e) {
            this.dom.periodSelector.textContent = "Ошибка загрузки периодов.";
        }
    },

    // =====================================================
    // ПРЕДПРОСМОТР ЗАКРЫТИЯ ПЕРИОДА
    // =====================================================

    updateClosePreviewVisibility(periods) {
        // Показываем блок предпросмотра только если есть активный период
        const hasActive = periods.some(p => p.is_active);
        if (this.dom.closePreviewCard) {
            this.dom.closePreviewCard.style.display = hasActive ? 'block' : 'none';
        }
    },

    async loadClosePreview() {
        if (!this.dom.closePreviewContainer) return;
        setLoading(this.dom.btnLoadPreview, true, 'Анализ...');
        this.dom.closePreviewContainer.innerHTML = '<div style="text-align:center; padding:20px; color:#888;">Сканируем данные...</div>';

        try {
            const data = await api.get('/admin/periods/close-preview');
            this.renderClosePreview(data);
        } catch (e) {
            this.dom.closePreviewContainer.innerHTML = `<div style="color:red; padding:16px;">Ошибка: ${esc(e.message)}</div>`;
        } finally {
            setLoading(this.dom.btnLoadPreview, false, 'Загрузить отчёт');
        }
    },

    renderClosePreview(data) {
        const pct = data.total_occupied_rooms > 0
            ? Math.round(data.rooms_with_readings / data.total_occupied_rooms * 100) : 0;

        const progressColor = pct >= 80 ? '#10b981' : pct >= 50 ? '#f59e0b' : '#ef4444';

        let dormHtml = '';
        if (data.dormitories && data.dormitories.length > 0) {
            dormHtml = `
                <table style="width:100%; border-collapse:collapse; margin-top:16px; font-size:13px;">
                    <thead>
                        <tr style="background:#f9fafb; border-bottom:2px solid #e5e7eb;">
                            <th style="text-align:left; padding:8px;">Общежитие</th>
                            <th style="text-align:center; padding:8px;">Сдали</th>
                            <th style="text-align:center; padding:8px;">Не сдали</th>
                            <th style="text-align:center; padding:8px;">%</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${data.dormitories.map(d => `
                            <tr style="border-bottom:1px solid #f3f4f6;">
                                <td style="padding:8px; font-weight:500;">${esc(d.name)}</td>
                                <td style="text-align:center; padding:8px; color:#10b981; font-weight:600;">${d.submitted}</td>
                                <td style="text-align:center; padding:8px; color:${d.missing > 0 ? '#ef4444' : '#ccc'}; font-weight:600;">${d.missing}</td>
                                <td style="text-align:center; padding:8px;">
                                    <div style="background:#e5e7eb; border-radius:4px; height:8px; width:80px; display:inline-block; vertical-align:middle;">
                                        <div style="background:${d.percent >= 80 ? '#10b981' : d.percent >= 50 ? '#f59e0b' : '#ef4444'}; height:100%; width:${d.percent}%; border-radius:4px;"></div>
                                    </div>
                                    <span style="margin-left:6px; font-size:12px;">${d.percent}%</span>
                                </td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            `;
        }

        this.dom.closePreviewContainer.innerHTML = `
            <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap:12px; margin-bottom:16px;">
                <div style="background:#f0fdf4; padding:14px; border-radius:8px; text-align:center;">
                    <div style="font-size:28px; font-weight:700; color:#10b981;">${data.rooms_with_readings}</div>
                    <div style="font-size:12px; color:#6b7280;">Комнат сдали</div>
                </div>
                <div style="background:${data.rooms_without_readings > 0 ? '#fef2f2' : '#f0fdf4'}; padding:14px; border-radius:8px; text-align:center;">
                    <div style="font-size:28px; font-weight:700; color:${data.rooms_without_readings > 0 ? '#ef4444' : '#10b981'};">${data.rooms_without_readings}</div>
                    <div style="font-size:12px; color:#6b7280;">Авто-генерация</div>
                </div>
                <div style="background:${data.anomalies_count > 0 ? '#fffbeb' : '#f0fdf4'}; padding:14px; border-radius:8px; text-align:center;">
                    <div style="font-size:28px; font-weight:700; color:${data.anomalies_count > 0 ? '#f59e0b' : '#10b981'};">${data.anomalies_count}</div>
                    <div style="font-size:12px; color:#6b7280;">Аномалий</div>
                </div>
                <div style="background:#eff6ff; padding:14px; border-radius:8px; text-align:center;">
                    <div style="font-size:28px; font-weight:700; color:#3b82f6;">${data.safe_drafts}</div>
                    <div style="font-size:12px; color:#6b7280;">Авто-утверждение</div>
                </div>
                <div style="background:#f9fafb; padding:14px; border-radius:8px; text-align:center;">
                    <div style="font-size:22px; font-weight:700; color:#1f2937;">${Number(data.estimated_total).toFixed(2)} ₽</div>
                    <div style="font-size:12px; color:#6b7280;">Предв. итого</div>
                </div>
            </div>
            <div style="background:#f9fafb; padding:10px 14px; border-radius:6px; margin-bottom:8px; display:flex; align-items:center; gap:12px;">
                <div style="flex:1; background:#e5e7eb; border-radius:4px; height:12px;">
                    <div style="background:${progressColor}; height:100%; width:${pct}%; border-radius:4px; transition: width 0.5s;"></div>
                </div>
                <span style="font-weight:600; font-size:14px; color:${progressColor};">${pct}%</span>
                <span style="font-size:12px; color:#6b7280;">комнат сдали показания</span>
            </div>
            ${dormHtml}
        `;
    },

    // =====================================================
    // СРАВНИТЕЛЬНАЯ АНАЛИТИКА
    // =====================================================

    populateCompareSelectors(periods) {
        if (!this.dom.comparePeriodA || !this.dom.comparePeriodB) return;

        const makeOptions = (selectEl) => {
            selectEl.innerHTML = '<option value="">Выберите период...</option>';
            periods.forEach(p => {
                selectEl.appendChild(el('option', { value: p.id }, `${p.name}${p.is_active ? ' (Акт.)' : ''}`));
            });
        };

        makeOptions(this.dom.comparePeriodA);
        makeOptions(this.dom.comparePeriodB);

        // По умолчанию: A = предпоследний, B = последний (если есть хотя бы 2 периода)
        if (periods.length >= 2) {
            this.dom.comparePeriodB.value = periods[0].id;
            this.dom.comparePeriodA.value = periods[1].id;
        }
    },

    async runComparison() {
        const idA = this.dom.comparePeriodA?.value;
        const idB = this.dom.comparePeriodB?.value;

        if (!idA || !idB) return toast('Выберите оба периода для сравнения', 'warning');
        if (idA === idB) return toast('Выберите два разных периода', 'warning');

        setLoading(this.dom.btnCompare, true, 'Анализ...');
        this.dom.compareContainer.innerHTML = '<div style="text-align:center; padding:30px; color:#888;">Сравниваем данные...</div>';

        try {
            const data = await api.get(`/admin/periods/compare?period_a=${idA}&period_b=${idB}`);
            this.renderComparison(data);
        } catch (e) {
            this.dom.compareContainer.innerHTML = `<div style="color:red; padding:16px;">Ошибка: ${esc(e.message)}</div>`;
        } finally {
            setLoading(this.dom.btnCompare, false, 'Сравнить');
        }
    },

    renderComparison(data) {
        const LABELS = {
            cost_hot_water: 'ГВС', cost_cold_water: 'ХВС', cost_sewage: 'Водоотв.',
            cost_electricity: 'Электр.', cost_maintenance: 'Содержание', cost_social_rent: 'Наём',
            cost_waste: 'ТКО', cost_fixed_part: 'Отопление', total_cost: 'ИТОГО'
        };

        const deltaCell = (val, pct) => {
            const color = val > 0 ? '#ef4444' : val < 0 ? '#10b981' : '#9ca3af';
            const arrow = val > 0 ? '▲' : val < 0 ? '▼' : '—';
            const sign = val > 0 ? '+' : '';
            return `<span style="color:${color}; font-weight:600;">${arrow} ${sign}${val.toFixed(2)}</span>
                    <span style="color:${color}; font-size:11px; margin-left:4px;">(${sign}${pct}%)</span>`;
        };

        let html = `
            <div style="padding:12px 16px; background:#eff6ff; border-radius:8px; margin:16px; font-size:13px; display:flex; gap:20px; align-items:center;">
                <span><strong>A:</strong> ${esc(data.period_a.name)}</span>
                <span style="color:#6b7280;">→</span>
                <span><strong>B:</strong> ${esc(data.period_b.name)}</span>
                <span style="color:#6b7280; margin-left:auto;">Красный = рост расходов, Зелёный = экономия</span>
            </div>
        `;

        // Таблица по общежитиям
        data.dormitories.forEach(dorm => {
            const tc = dorm.details.total_cost;
            const dormDeltaColor = tc.delta > 0 ? '#ef4444' : tc.delta < 0 ? '#10b981' : '#6b7280';

            html += `
                <div style="margin:0 16px 16px 16px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; padding:10px 14px; background:#f9fafb; border-radius:8px 8px 0 0; border:1px solid #e5e7eb; border-bottom:none;">
                        <strong style="font-size:14px;">🏢 ${esc(dorm.dormitory)}</strong>
                        <span style="color:${dormDeltaColor}; font-weight:700; font-size:14px;">
                            ${tc.delta > 0 ? '+' : ''}${tc.delta.toFixed(2)} ₽ (${tc.delta > 0 ? '+' : ''}${tc.percent}%)
                        </span>
                    </div>
                    <table style="width:100%; border-collapse:collapse; font-size:13px; border:1px solid #e5e7eb; border-top:none;">
                        <thead>
                            <tr style="background:#f9fafb;">
                                <th style="text-align:left; padding:6px 10px;">Ресурс</th>
                                <th style="text-align:right; padding:6px 10px;">${esc(data.period_a.name)}</th>
                                <th style="text-align:right; padding:6px 10px;">${esc(data.period_b.name)}</th>
                                <th style="text-align:right; padding:6px 10px;">Изменение</th>
                            </tr>
                        </thead>
                        <tbody>
            `;

            for (const [key, label] of Object.entries(LABELS)) {
                const d = dorm.details[key];
                if (!d) continue;
                const isTotalRow = key === 'total_cost';
                const rowStyle = isTotalRow ? 'background:#f0f9ff; font-weight:700;' : 'border-bottom:1px solid #f3f4f6;';
                html += `
                    <tr style="${rowStyle}">
                        <td style="padding:6px 10px;">${label}</td>
                        <td style="text-align:right; padding:6px 10px;">${d.period_a.toFixed(2)}</td>
                        <td style="text-align:right; padding:6px 10px;">${d.period_b.toFixed(2)}</td>
                        <td style="text-align:right; padding:6px 10px;">${deltaCell(d.delta, d.percent)}</td>
                    </tr>
                `;
            }

            html += '</tbody></table></div>';
        });

        // Итоговый блок
        const gt = data.totals.details.total_cost;
        const gtColor = gt.delta > 0 ? '#ef4444' : gt.delta < 0 ? '#10b981' : '#6b7280';

        html += `
            <div style="margin:16px; padding:16px; background:${gt.delta > 0 ? '#fef2f2' : gt.delta < 0 ? '#f0fdf4' : '#f9fafb'}; border-radius:8px; border:2px solid ${gtColor}40; display:flex; justify-content:space-between; align-items:center;">
                <div>
                    <div style="font-size:13px; color:#6b7280;">Общий итог по всем объектам</div>
                    <div style="font-size:13px; margin-top:4px;">
                        ${esc(data.period_a.name)}: <strong>${gt.period_a.toFixed(2)} ₽</strong>
                        &nbsp;→&nbsp;
                        ${esc(data.period_b.name)}: <strong>${gt.period_b.toFixed(2)} ₽</strong>
                    </div>
                </div>
                <div style="text-align:right;">
                    <div style="font-size:24px; font-weight:700; color:${gtColor};">${gt.delta > 0 ? '+' : ''}${gt.delta.toFixed(2)} ₽</div>
                    <div style="font-size:14px; color:${gtColor};">${gt.delta > 0 ? '+' : ''}${gt.percent}%</div>
                </div>
            </div>
        `;

        this.dom.compareContainer.innerHTML = html;
    },

    // =====================================================
    // СУЩЕСТВУЮЩИЕ ФУНКЦИИ (без изменений)
    // =====================================================

    async loadData() {
        if (this.state.controller) this.state.controller.abort();
        this.state.controller = new AbortController();
        this.dom.container.innerHTML = '<div style="text-align:center; padding:40px; color:#666;">Загрузка...</div>';

        try {
            const data = await api.get(`/admin/summary?period_id=${this.state.selectedPeriodId}`, { signal: this.state.controller.signal });
            this.renderData(data);
        } catch (e) {
            if (e.name === 'AbortError') return;
            this.dom.container.innerHTML = `<div style="text-align:center; padding:20px; color:red;">Ошибка: ${esc(e.message)}</div>`;
        }
    },

    renderData(data) {
        this.dom.container.innerHTML = '';
        if (!Object.keys(data).length) {
            this.dom.container.innerHTML = '<div style="text-align:center; padding:40px;">Нет данных за этот период.</div>';
            return;
        }

        const fragment = document.createDocumentFragment();
        Object.keys(data).sort().forEach(dormName => {
            const records = data[dormName];
            fragment.appendChild(el('h3', { style: { margin: '20px 0 10px 0', borderBottom: '1px solid #ccc', paddingBottom: '5px' } }, `🏢 ${dormName}`));

            const table = el('table');
            table.innerHTML = `
                <thead><tr><th>Жилец</th><th class="text-right">Счет 209 (Комм.)</th><th class="text-right">Счет 205 (Найм)</th><th class="text-right">ИТОГО</th><th class="text-center">Действия</th></tr></thead>
            `;
            const tbody = el('tbody');
            const totals = { total_209: 0, total_205: 0, total_cost: 0 };

            records.forEach(r => {
                Object.keys(totals).forEach(k => totals[k] += Number(r[k] || 0));
                tbody.appendChild(el('tr', { class: 'hover:bg-gray-50' },
                    el('td', {},
                        el('div', { class: 'font-bold' }, r.username),
                        el('div', { style: { fontSize: '11px', color: '#888' } }, `${r.area}м² / ${r.residents} чел.`)
                    ),
                    el('td', { class: 'text-right' }, Number(r.total_209).toFixed(2)),
                    el('td', { class: 'text-right' }, Number(r.total_205).toFixed(2)),
                    el('td', { class: 'text-right font-bold', style: { color: '#059669' } }, Number(r.total_cost).toFixed(2)),
                    el('td', { class: 'text-center' },
                        el('button', {
                            class: 'action-btn secondary-btn', style: { padding: '2px 8px', fontSize: '12px' },
                            onclick: () => this.downloadReceipt(r.reading_id)
                        }, 'PDF')
                    )
                ));
            });

            tbody.appendChild(el('tr', { style: { background: '#f8f8f8', fontWeight: 'bold' } },
                el('td', { class: 'text-right' }, 'ИТОГО по объекту:'),
                el('td', { class: 'text-right' }, totals.total_209.toFixed(2)),
                el('td', { class: 'text-right' }, totals.total_205.toFixed(2)),
                el('td', { class: 'text-right', style: { color: '#059669' } }, totals.total_cost.toFixed(2)),
                el('td')
            ));

            table.appendChild(tbody);
            fragment.appendChild(table);
        });
        this.dom.container.appendChild(fragment);
    },

    async downloadReceipt(id) {
        toast('Подготовка PDF...', 'info');
        try {
            const res = await api.get(`/admin/receipts/${id}`);
            if (res.url) {
                window.open(res.url, '_blank');
            } else {
                throw new Error('Сервер не вернул ссылку на файл.');
            }
        } catch (e) {
            toast('Ошибка скачивания: ' + e.message, 'error');
        }
    },

    async downloadExcel() {
        if (!this.state.selectedPeriodId) return toast('Сначала выберите период!', 'warning');
        setLoading(this.dom.btnExcel, true, 'Формирование...');
        try {
            const url = `/admin/export_report?period_id=${this.state.selectedPeriodId}`;
            await api.download(url, `Svodnaya_vedomost_${this.state.selectedPeriodId}.xlsx`);
        } catch (e) {
            toast('Ошибка скачивания Excel: ' + e.message, 'error');
        } finally {
            setLoading(this.dom.btnExcel, false);
        }
    },

    async downloadZip() {
        if (!this.state.selectedPeriodId) return toast('Сначала выберите период!', 'warning');
        setLoading(this.dom.btnZip, true, 'Запуск задачи...');
        try {
            toast('Архив формируется на сервере. Это может занять до минуты...', 'info');
            const res = await api.post(`/admin/reports/bulk-zip?period_id=${this.state.selectedPeriodId}`);
            await this.pollTask(res.task_id, this.dom.btnZip);
        } catch (e) {
            toast('Ошибка запуска: ' + e.message, 'error');
            setLoading(this.dom.btnZip, false);
        }
    },

    async pollTask(taskId, button) {
        if (this.state.pollTimer) clearInterval(this.state.pollTimer);
        const originalText = button.textContent;

        setLoading(button, true, 'Обработка...');

        return new Promise((resolve, reject) => {
            let attempts = 0;
            const maxAttempts = 150;

            this.state.pollTimer = setInterval(async () => {
                attempts++;
                if (attempts > maxAttempts) {
                    clearInterval(this.state.pollTimer);
                    setLoading(button, false, originalText);
                    return reject(new Error('Время ожидания истекло.'));
                }

                try {
                    const data = await api.get(`/admin/tasks/${taskId}`);

                    if (data.status === 'done' || data.state === 'SUCCESS') {
                        clearInterval(this.state.pollTimer);
                        setLoading(button, false, originalText);

                        if (data.download_url) {
                            window.open(data.download_url, '_blank');
                            toast('Архив готов и скачивается!', 'success');
                            resolve(data);
                        } else if (data.result && data.result.status === 'error') {
                            reject(new Error(data.result.message || 'Ошибка сборки архива на сервере.'));
                        } else {
                            reject(new Error('Неожиданный ответ от сервера.'));
                        }
                    } else if (data.state === 'FAILURE') {
                        clearInterval(this.state.pollTimer);
                        setLoading(button, false, originalText);
                        reject(new Error(data.error || 'Ошибка выполнения задачи.'));
                    }
                } catch (e) {
                    if (e.name === 'AbortError') return;
                }
            }, 2000);
        });
    }
};