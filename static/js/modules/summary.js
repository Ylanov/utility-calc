// static/js/modules/summary.js
import { api } from '../core/api.js';
import { el, setLoading, toast } from '../core/dom.js';

export const SummaryModule = {
    isInitialized: false,
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
            btnZip: document.getElementById('btnDownloadZip')
        };
    },

    bindEvents() {
        if (this.dom.btnRefresh) this.dom.btnRefresh.addEventListener('click', () => this.loadData());
        if (this.dom.btnExcel) this.dom.btnExcel.addEventListener('click', () => this.downloadExcel());
        if (this.dom.btnZip) this.dom.btnZip.addEventListener('click', () => this.downloadZip());
    },

    async loadPeriods() {
        if (!this.dom.periodSelector) return;
        this.dom.periodSelector.innerHTML = '<span>Загрузка периодов...</span>';

        try {
            const periods = await api.get('/admin/periods/history');
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

            // Выбираем самый свежий период (первый в списке) по умолчанию
            this.state.selectedPeriodId = periods[0].id;
            this.loadData();
        } catch (e) {
            this.dom.periodSelector.textContent = "Ошибка загрузки периодов.";
        }
    },

    async loadData() {
        if (this.state.controller) this.state.controller.abort();
        this.state.controller = new AbortController();
        this.dom.container.innerHTML = `<div style="text-align:center; padding:40px; color:#666;">Загрузка...</div>`;

        try {
            const data = await api.get(`/admin/summary?period_id=${this.state.selectedPeriodId}`, { signal: this.state.controller.signal });
            this.renderData(data);
        } catch (e) {
            if (e.name === 'AbortError') return;
            this.dom.container.innerHTML = `<div style="text-align:center; padding:20px; color:red;">Ошибка: ${e.message}</div>`;
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
            const maxAttempts = 150; // 5 минут

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

                        // ИСПРАВЛЕНИЕ ЛОГИКИ ПРОВЕРКИ
                        if (data.download_url) {
                            window.open(data.download_url, '_blank');
                            toast('Архив готов и скачивается!', 'success');
                            resolve(data);
                        } else if (data.result && data.result.status === 'error') {
                            // Если Celery вернул логическую ошибку внутри результата (например, "Нет файлов")
                            reject(new Error(data.result.message || 'Ошибка сборки архива на сервере.'));
                        } else {
                            reject(new Error('Сервер завершил задачу, но не вернул ссылку (возможно, нет утвержденных квитанций).'));
                        }
                    } else if (data.state === 'FAILURE') {
                        clearInterval(this.state.pollTimer);
                        setLoading(button, false, originalText);
                        reject(new Error(data.error || 'Критическая ошибка на сервере.'));
                    }
                } catch (e) {
                    clearInterval(this.state.pollTimer);
                    setLoading(button, false, originalText);
                    reject(e);
                }
            }, 2000);
        });
    }
};