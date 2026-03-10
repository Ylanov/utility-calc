// static/js/modules/summary.js
import { api } from '../core/api.js';
import { el, setLoading, toast } from '../core/dom.js';

export const SummaryModule = {
    state: {
        selectedPeriodId: null,
        controller: null
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
        this.dom.periodSelector.innerHTML = '<span class="text-gray-500">Загрузка периодов...</span>';

        try {
            const periods = await api.get('/admin/periods/history');
            this.dom.periodSelector.innerHTML = '';
            if (!periods || !periods.length) {
                this.dom.periodSelector.textContent = "Нет доступных периодов";
                return;
            }

            const select = el('select', {
                class: 'border p-2 rounded bg-white',
                style: { fontSize: '14px', minWidth: '200px' },
                onchange: (e) => {
                    this.state.selectedPeriodId = e.target.value;
                    this.loadData();
                }
            });
            periods.forEach(p => {
                const isActiveText = p.is_active ? ' (Активен)' : '';
                select.appendChild(el('option', { value: p.id }, `${p.name}${isActiveText}`));
            });
            this.dom.periodSelector.appendChild(el('div', { class: 'flex items-center gap-2' },
                el('span', { class: 'font-bold' }, 'Период: '), select
            ));

            if (periods.length > 0) {
                this.state.selectedPeriodId = periods[0].id;
                select.value = periods[0].id;
                this.loadData();
            }
        } catch (e) {
            console.error(e);
            this.dom.periodSelector.textContent = "Ошибка загрузки периодов";
        }
    },

    async loadData() {
        if (this.state.controller) this.state.controller.abort();
        this.state.controller = new AbortController();

        this.dom.container.innerHTML = `<div style="text-align:center; padding:40px; color:#666;"><div class="spinner mb-2"></div>⏳ Загрузка сводной таблицы...</div>`;

        try {
            const periodParam = this.state.selectedPeriodId ? `?period_id=${this.state.selectedPeriodId}` : '';
            const data = await api.get(`/admin/summary${periodParam}`, { signal: this.state.controller.signal });
            this.renderData(data);
        } catch (e) {
            if (e.name === 'AbortError') return;

            // БЕЗОПАСНЫЙ ВЫВОД ОШИБКИ (Защита от XSS)
            this.dom.container.innerHTML = '';
            const errorBox = el('div', {
                style: { textAlign: 'center', color: '#e74c3c', padding: '20px', border: '1px solid #e74c3c', borderRadius: '8px', margin: '20px' }
            },
                el('strong', {}, 'Ошибка загрузки:'),
                el('br', {}),
                e.message
            );
            this.dom.container.appendChild(errorBox);
        }
    },

    renderData(data) {
        this.dom.container.innerHTML = '';
        if (!data || Object.keys(data).length === 0) {
            this.dom.container.innerHTML = '<div style="text-align:center; padding:40px; color:#888;">Нет данных для отображения за выбранный период</div>';
            return;
        }

        const fragment = document.createDocumentFragment();
        const sortedDorms = Object.keys(data).sort();

        for (const dormName of sortedDorms) {
            const records = data[dormName];
            const card = el('div', { class: 'bg-white shadow rounded-lg mb-6 overflow-hidden' });
            card.appendChild(el('div', { class: 'bg-gray-100 px-4 py-3 border-b' },
                el('h3', { class: 'font-bold text-lg text-gray-700' }, `🏠 ${dormName}`)
            ));

            const tableContainer = el('div', { class: 'overflow-x-auto' });
            const table = el('table', { class: 'min-w-full text-sm' });

            table.appendChild(el('thead', { class: 'bg-gray-50' }, el('tr', {},
                el('th', { class: 'px-3 py-2 text-left' }, 'Жилец'),
                el('th', { class: 'px-3 py-2 text-right' }, 'ГВС'),
                el('th', { class: 'px-3 py-2 text-right' }, 'ХВС'),
                el('th', { class: 'px-3 py-2 text-right' }, 'Свет'),
                el('th', { class: 'px-3 py-2 text-right' }, 'Содерж.'),
                el('th', { class: 'px-3 py-2 text-right' }, 'Наем'),
                el('th', { class: 'px-3 py-2 text-right font-bold' }, 'Счет 209'),
                el('th', { class: 'px-3 py-2 text-right font-bold' }, 'Счет 205'),
                el('th', { class: 'px-3 py-2 text-right font-bold text-red-600' }, 'ИТОГО'),
                el('th', { class: 'px-3 py-2 text-center' }, 'Действия')
            )));

            const tbody = el('tbody', { class: 'divide-y divide-gray-200' });
            const totals = { hot: 0, cold: 0, el: 0, main: 0, rent: 0, sum_209: 0, sum_205: 0, sum_total: 0 };

            records.forEach(r => {
                totals.hot += Number(r.hot || 0);
                totals.cold += Number(r.cold || 0);
                totals.el += Number(r.electric || 0);
                totals.main += Number(r.maintenance || 0);
                totals.rent += Number(r.rent || 0);
                totals.sum_209 += Number(r.total_209 || 0);
                totals.sum_205 += Number(r.total_205 || 0);
                totals.sum_total += Number(r.total_cost || 0);

                tbody.appendChild(el('tr', { class: 'hover:bg-gray-50' },
                    el('td', { class: 'px-3 py-2' },
                        el('div', { class: 'font-medium' }, r.username),
                        el('div', { class: 'text-xs text-gray-500' }, `${r.area}м² / ${r.residents} чел`)
                    ),
                    el('td', { class: 'px-3 py-2 text-right' }, Number(r.hot).toFixed(2)),
                    el('td', { class: 'px-3 py-2 text-right' }, Number(r.cold).toFixed(2)),
                    el('td', { class: 'px-3 py-2 text-right' }, Number(r.electric).toFixed(2)),
                    el('td', { class: 'px-3 py-2 text-right' }, Number(r.maintenance).toFixed(2)),
                    el('td', { class: 'px-3 py-2 text-right' }, Number(r.rent).toFixed(2)),
                    el('td', { class: 'px-3 py-2 text-right font-bold' }, Number(r.total_209).toFixed(2)),
                    el('td', { class: 'px-3 py-2 text-right font-bold' }, Number(r.total_205).toFixed(2)),
                    el('td', { class: 'px-3 py-2 text-right font-bold text-red-600' }, Number(r.total_cost).toFixed(2)),
                    el('td', { class: 'px-3 py-2 text-center' },
                        el('button', {
                            class: 'text-blue-600 hover:text-blue-900',
                            title: 'Скачать квитанцию',
                            onclick: () => this.downloadReceipt(r.reading_id)
                        }, '📄')
                    )
                ));
            });

            tbody.appendChild(el('tr', { class: 'bg-blue-50 font-bold' },
                el('td', { class: 'px-3 py-2 text-right' }, 'ИТОГО:'),
                el('td', { class: 'px-3 py-2 text-right' }, totals.hot.toFixed(2)),
                el('td', { class: 'px-3 py-2 text-right' }, totals.cold.toFixed(2)),
                el('td', { class: 'px-3 py-2 text-right' }, totals.el.toFixed(2)),
                el('td', { class: 'px-3 py-2 text-right' }, totals.main.toFixed(2)),
                el('td', { class: 'px-3 py-2 text-right' }, totals.rent.toFixed(2)),
                el('td', { class: 'px-3 py-2 text-right' }, totals.sum_209.toFixed(2)),
                el('td', { class: 'px-3 py-2 text-right' }, totals.sum_205.toFixed(2)),
                el('td', { class: 'px-3 py-2 text-right text-red-600' }, totals.sum_total.toFixed(2)),
                el('td', {}, '')
            ));

            table.appendChild(tbody);
            tableContainer.appendChild(table);
            card.appendChild(tableContainer);
            fragment.appendChild(card);
        }

        this.dom.container.appendChild(fragment);
    },

    async downloadReceipt(id) {
        toast('Генерация квитанции...', 'info');
        try {
            const res = await api.post(`/admin/receipts/${id}/generate`, {});
            const result = await this.pollTask(res.task_id);
            if (result.download_url) {
                this.triggerFileDownload(result.download_url, `receipt_${id}.pdf`);
                toast('Файл скачивается', 'success');
            } else {
                throw new Error('Ссылка на файл не получена');
            }
        } catch (e) {
            toast('Ошибка скачивания: ' + e.message, 'error');
        }
    },

    async downloadExcel() {
        if (!this.state.selectedPeriodId) return toast('Выберите период', 'warning');
        setLoading(this.dom.btnExcel, true, 'Скачивание...');
        try {
            const url = `/api/admin/export_report?period_id=${this.state.selectedPeriodId}`;
            await api.download(url.replace('/api', ''), `report_${this.state.selectedPeriodId}.xlsx`);
            toast('Отчет скачан', 'success');
        } catch (e) {
            toast('Ошибка скачивания Excel: ' + e.message, 'error');
        } finally {
            setLoading(this.dom.btnExcel, false);
        }
    },

    async downloadZip() {
        if (!this.state.selectedPeriodId) return toast('Выберите период', 'warning');
        setLoading(this.dom.btnZip, true, 'Формирование...');
        try {
            toast('Архив формируется. Это может занять до минуты...', 'info');
            const res = await api.post(`/admin/reports/bulk-zip?period_id=${this.state.selectedPeriodId}`, {});
            const result = await this.pollTask(res.task_id);
            if (result.download_url) {
                this.triggerFileDownload(result.download_url, `archive_${this.state.selectedPeriodId}.zip`);
                toast('Архив скачивается', 'success');
            }
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        } finally {
            setLoading(this.dom.btnZip, false);
        }
    },

    triggerFileDownload(url, filename) {
        const link = document.createElement('a');
        link.href = url;
        link.setAttribute('download', filename || '');
        link.style.display = 'none';
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
    },

    async pollTask(taskId) {
        let attempts = 0;
        const maxAttempts = 150; // 5 минут (150 * 2 сек)

        const check = async () => {
            attempts++;
            if (attempts > maxAttempts) {
                throw new Error('Время ожидания генерации файла истекло (5 минут)');
            }

            const data = await api.get(`/admin/tasks/${taskId}`);

            if (data.status === 'done' || data.status === 'ok' || data.state === 'SUCCESS') {
                return data;
            }
            if (data.state === 'FAILURE') {
                throw new Error(data.error || 'Ошибка выполнения задачи на сервере');
            }

            // Если все еще обрабатывается, ждем 2 секунды
            await new Promise(resolve => setTimeout(resolve, 2000));
            return check();
        };

        return check();
    }
};