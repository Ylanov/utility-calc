// static/js/modules/summary.js
import { api } from '../core/api.js';
import { el, clear, setLoading, toast } from '../core/dom.js';

export const SummaryModule = {
    state: {
        selectedPeriodId: null,
        controller: null // Для отмены запросов
    },

    init() {
        this.cacheDOM();
        this.bindEvents();
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
        this.dom.periodSelector.innerHTML = '<span class="text-gray-500">Загрузка...</span>';

        try {
            const periods = await api.get('/admin/periods/history');
            this.dom.periodSelector.innerHTML = '';

            if (!periods || !periods.length) {
                this.dom.periodSelector.textContent = "Нет периодов";
                return;
            }

            const select = el('select', {
                class: 'border p-2 rounded',
                style: { fontSize: '14px', minWidth: '200px' },
                onchange: (e) => {
                    this.state.selectedPeriodId = e.target.value;
                    this.loadData();
                }
            });

            periods.forEach(p => {
                const opt = el('option', { value: p.id }, `${p.name} ${p.is_active ? '(Активен)' : ''}`);
                select.appendChild(opt);
            });

            this.dom.periodSelector.appendChild(el('span', { style: { marginRight: '10px', fontWeight: 'bold' } }, 'Период: '));
            this.dom.periodSelector.appendChild(select);

            // Выбираем первый по умолчанию
            this.state.selectedPeriodId = periods[0].id;
            select.value = periods[0].id;

            // Загружаем данные для первого периода
            this.loadData();

        } catch (e) {
            console.error(e);
            this.dom.periodSelector.textContent = "Ошибка загрузки";
        }
    },

    async loadData() {
        // Отменяем предыдущий запрос, если он еще идет
        if (this.state.controller) {
            this.state.controller.abort();
        }
        this.state.controller = new AbortController();

        this.dom.container.innerHTML = '<div style="text-align:center; padding:40px; color:#888;">⏳ Загрузка сводки...</div>';

        try {
            const url = this.state.selectedPeriodId
                ? `/admin/summary?period_id=${this.state.selectedPeriodId}`
                : '/admin/summary';

            const data = await api.get(url, { signal: this.state.controller.signal });

            this.renderData(data);
        } catch (e) {
            if (e.name === 'AbortError') return; // Игнорируем отмену
            this.dom.container.innerHTML = `<div style="text-align:center; color:red; padding:20px;">Ошибка: ${e.message}</div>`;
        }
    },

    renderData(data) {
        this.dom.container.innerHTML = '';

        if (!data || Object.keys(data).length === 0) {
            this.dom.container.innerHTML = '<div style="text-align:center; padding:40px; color:#888;">Нет данных за этот период</div>';
            return;
        }

        const fragment = document.createDocumentFragment();

        for (const [dormName, records] of Object.entries(data)) {
            const card = el('div', { class: 'card' });

            // Заголовок общежития
            card.appendChild(el('h3', {
                style: {
                    borderLeft: '4px solid #4a90e2',
                    paddingLeft: '10px',
                    marginBottom: '15px'
                }
            }, `🏠 ${dormName}`));

            // Таблица
            const table = el('table', { style: { fontSize: '13px' } });

            // Шапка
            table.appendChild(el('thead', {}, el('tr', { style: { background: '#f8f9fa' } },
                el('th', {}, 'Дата'),
                el('th', {}, 'Жилец'),
                el('th', {}, 'ГВС'),
                el('th', {}, 'ХВС'),
                el('th', {}, 'Свет'),
                el('th', {}, 'Содерж.'),
                el('th', {}, 'Наем'),
                el('th', {}, 'ТКО'),
                el('th', {}, 'Отопл.'),
                el('th', {}, 'ИТОГО'),
                el('th', {}, '')
            )));

            const tbody = el('tbody', {});
            const totals = { hot:0, cold:0, el:0, main:0, rent:0, waste:0, fix:0, sum:0 };

            records.forEach(r => {
                // Суммируем
                totals.hot += Number(r.hot);
                totals.cold += Number(r.cold);
                totals.el += Number(r.electric);
                totals.main += Number(r.maintenance);
                totals.rent += Number(r.rent);
                totals.waste += Number(r.waste);
                totals.fix += Number(r.fixed);
                totals.sum += Number(r.total);

                const tr = el('tr', {},
                    el('td', {}, r.date.split(' ')[0]),
                    el('td', {},
                        el('div', { style: { fontWeight: 'bold' } }, r.username),
                        el('div', { style: { fontSize: '11px', color: '#999' } }, `${r.area}м² / ${r.residents} чел`)
                    ),
                    el('td', {}, Number(r.hot).toFixed(2)),
                    el('td', {}, Number(r.cold).toFixed(2)),
                    el('td', {}, Number(r.electric).toFixed(2)),
                    el('td', {}, Number(r.maintenance).toFixed(2)),
                    el('td', {}, Number(r.rent).toFixed(2)),
                    el('td', {}, Number(r.waste).toFixed(2)),
                    el('td', {}, Number(r.fixed).toFixed(2)),
                    el('td', { style: { fontWeight: 'bold' } }, Number(r.total).toFixed(2)),
                    el('td', {},
                        el('button', {
                            class: 'btn-icon btn-doc',
                            title: 'Скачать квитанцию',
                            onclick: () => this.downloadReceipt(r.reading_id)
                        }, '📄')
                    )
                );
                tbody.appendChild(tr);
            });

            // Итоговая строка
            tbody.appendChild(el('tr', { style: { background: '#e8f5e9', fontWeight: 'bold' } },
                el('td', { colspan: 2 }, 'ИТОГО ПО ОБЩЕЖИТИЮ:'),
                el('td', {}, totals.hot.toFixed(2)),
                el('td', {}, totals.cold.toFixed(2)),
                el('td', {}, totals.el.toFixed(2)),
                el('td', {}, totals.main.toFixed(2)),
                el('td', {}, totals.rent.toFixed(2)),
                el('td', {}, totals.waste.toFixed(2)),
                el('td', {}, totals.fix.toFixed(2)),
                el('td', { style: { color: '#c0392b' } }, totals.sum.toFixed(2)),
                el('td', {}, '')
            ));

            table.appendChild(tbody);
            card.appendChild(table);
            fragment.appendChild(card);
        }

        this.dom.container.appendChild(fragment);
    },

    // --- СКАЧИВАНИЕ ---

    async downloadReceipt(id) {
        toast('Генерация квитанции...', 'info');
        try {
            // 1. Запускаем задачу
            const res = await api.post(`/admin/receipts/${id}/generate`, {});

            // 2. Ждем готовности (поллинг)
            const result = await this.pollTask(res.task_id);

            // 3. Скачиваем
            if (result.download_url) {
                // Создаем временную ссылку
                const link = document.createElement('a');
                link.href = result.download_url;
                link.target = '_blank';
                link.download = `receipt_${id}.pdf`;
                document.body.appendChild(link);
                link.click();
                link.remove();
                toast('Скачивание началось', 'success');
            }
        } catch (e) {
            toast('Ошибка скачивания: ' + e.message, 'error');
        }
    },

    async downloadExcel() {
        if (!this.state.selectedPeriodId) return;
        setLoading(this.dom.btnExcel, true, 'Скачивание...');

        try {
            await api.download(`/admin/export_report?period_id=${this.state.selectedPeriodId}`, `report_${this.state.selectedPeriodId}.xlsx`);
            toast('Отчет скачан', 'success');
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        } finally {
            setLoading(this.dom.btnExcel, false);
        }
    },

    async downloadZip() {
        if (!this.state.selectedPeriodId) return;
        setLoading(this.dom.btnZip, true, 'Формирование...');

        try {
            toast('Архив формируется, это может занять время...', 'info');

            const res = await api.post(`/admin/reports/bulk-zip?period_id=${this.state.selectedPeriodId}`, {});
            const result = await this.pollTask(res.task_id);

            if (result.download_url) {
                const link = document.createElement('a');
                link.href = result.download_url;
                link.target = '_blank';
                document.body.appendChild(link);
                link.click();
                link.remove();
                toast('Архив готов!', 'success');
            }
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        } finally {
            setLoading(this.dom.btnZip, false);
        }
    },

    // Вспомогательная функция ожидания задачи
    async pollTask(taskId) {
        const poll = async () => {
            const data = await api.get(`/admin/tasks/${taskId}`);
            if (data.status === 'done' || data.state === 'SUCCESS') {
                return data;
            }
            if (data.state === 'FAILURE') {
                throw new Error(data.error || 'Ошибка сервера');
            }
            // Ждем 1.5 сек и повторяем
            await new Promise(r => setTimeout(r, 1500));
            return poll();
        };

        // Timeout 3 минуты
        const timeoutPromise = new Promise((_, reject) =>
            setTimeout(() => reject(new Error('Время ожидания истекло')), 180000)
        );

        return Promise.race([poll(), timeoutPromise]);
    }
};