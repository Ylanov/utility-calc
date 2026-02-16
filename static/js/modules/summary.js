// static/js/modules/summary.js
import { api } from '../core/api.js';
import { el, setLoading, toast } from '../core/dom.js';

export const SummaryModule = {
    state: {
        selectedPeriodId: null,
        controller: null // Контроллер для отмены предыдущих HTTP-запросов
    },

    dom: {},

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
        if (this.dom.btnRefresh) {
            this.dom.btnRefresh.addEventListener('click', () => this.loadData());
        }
        if (this.dom.btnExcel) {
            this.dom.btnExcel.addEventListener('click', () => this.downloadExcel());
        }
        if (this.dom.btnZip) {
            this.dom.btnZip.addEventListener('click', () => this.downloadZip());
        }
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

            // Создаем выпадающий список
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
                const opt = el('option', { value: p.id }, `${p.name}${isActiveText}`);
                select.appendChild(opt);
            });

            // Контейнер для лейбла и селекта
            const wrapper = el('div', { class: 'flex items-center gap-2' },
                el('span', { class: 'font-bold' }, 'Период: '),
                select
            );

            this.dom.periodSelector.appendChild(wrapper);

            // Выбираем первый (самый новый) период по умолчанию
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
        // 1. Отменяем предыдущий запрос, если он еще выполняется
        if (this.state.controller) {
            this.state.controller.abort();
        }
        // 2. Создаем новый контроллер
        this.state.controller = new AbortController();

        this.dom.container.innerHTML = `
            <div style="text-align:center; padding:40px; color:#666;">
                <div class="spinner mb-2"></div>
                ⏳ Загрузка сводной таблицы...
            </div>`;

        try {
            const periodParam = this.state.selectedPeriodId
                ? `?period_id=${this.state.selectedPeriodId}`
                : '';

            const url = `/admin/summary${periodParam}`;

            // Передаем signal в запрос
            const data = await api.get(url, { signal: this.state.controller.signal });

            this.renderData(data);

        } catch (e) {
            // Если ошибка вызвана отменой запроса — ничего не делаем
            if (e.name === 'AbortError') {
                console.log('Запрос отменен пользователем');
                return;
            }
            // Реальные ошибки показываем
            this.dom.container.innerHTML = `
                <div style="text-align:center; color:#e74c3c; padding:20px; border:1px solid #e74c3c; border-radius:8px; margin:20px;">
                    <strong>Ошибка загрузки данных:</strong><br>
                    ${e.message}
                </div>`;
        }
    },

    renderData(data) {
        this.dom.container.innerHTML = '';

        if (!data || Object.keys(data).length === 0) {
            this.dom.container.innerHTML = '<div style="text-align:center; padding:40px; color:#888;">Нет данных для отображения за выбранный период</div>';
            return;
        }

        const fragment = document.createDocumentFragment();

        // Сортируем общежития по алфавиту
        const sortedDorms = Object.keys(data).sort();

        for (const dormName of sortedDorms) {
            const records = data[dormName];
            const card = el('div', { class: 'bg-white shadow rounded-lg mb-6 overflow-hidden' });

            // Заголовок общежития
            const header = el('div', { class: 'bg-gray-100 px-4 py-3 border-b' },
                el('h3', { class: 'font-bold text-lg text-gray-700' }, `🏠 ${dormName}`)
            );
            card.appendChild(header);

            // Таблица
            const tableContainer = el('div', { class: 'overflow-x-auto' });
            const table = el('table', { class: 'min-w-full text-sm' });

            // Шапка таблицы
            table.appendChild(el('thead', { class: 'bg-gray-50' }, el('tr', {},
                el('th', { class: 'px-3 py-2 text-left' }, 'Дата'),
                el('th', { class: 'px-3 py-2 text-left' }, 'Жилец'),
                el('th', { class: 'px-3 py-2 text-right' }, 'ГВС'),
                el('th', { class: 'px-3 py-2 text-right' }, 'ХВС'),
                el('th', { class: 'px-3 py-2 text-right' }, 'Свет'),
                el('th', { class: 'px-3 py-2 text-right' }, 'Содерж.'),
                el('th', { class: 'px-3 py-2 text-right' }, 'Наем'),
                el('th', { class: 'px-3 py-2 text-right' }, 'ТКО'),
                el('th', { class: 'px-3 py-2 text-right' }, 'Отопл.'),
                el('th', { class: 'px-3 py-2 text-right font-bold' }, 'ИТОГО'),
                el('th', { class: 'px-3 py-2 text-center' }, 'Действия')
            )));

            const tbody = el('tbody', { class: 'divide-y divide-gray-200' });

            // Инициализация сумм
            const totals = {
                hot: 0, cold: 0, el: 0,
                main: 0, rent: 0, waste: 0, fix: 0,
                sum: 0
            };

            records.forEach(r => {
                // Приводим к числу для суммирования
                totals.hot += Number(r.hot || 0);
                totals.cold += Number(r.cold || 0);
                totals.el += Number(r.electric || 0);
                totals.main += Number(r.maintenance || 0);
                totals.rent += Number(r.rent || 0);
                totals.waste += Number(r.waste || 0);
                totals.fix += Number(r.fixed || 0);
                totals.sum += Number(r.total || 0);

                const dateStr = r.date ? r.date.split(' ')[0] : '-';

                const tr = el('tr', { class: 'hover:bg-gray-50' },
                    el('td', { class: 'px-3 py-2' }, dateStr),
                    el('td', { class: 'px-3 py-2' },
                        el('div', { class: 'font-medium' }, r.username),
                        el('div', { class: 'text-xs text-gray-500' }, `${r.area}м² / ${r.residents} чел`)
                    ),
                    el('td', { class: 'px-3 py-2 text-right' }, Number(r.hot).toFixed(2)),
                    el('td', { class: 'px-3 py-2 text-right' }, Number(r.cold).toFixed(2)),
                    el('td', { class: 'px-3 py-2 text-right' }, Number(r.electric).toFixed(2)),
                    el('td', { class: 'px-3 py-2 text-right' }, Number(r.maintenance).toFixed(2)),
                    el('td', { class: 'px-3 py-2 text-right' }, Number(r.rent).toFixed(2)),
                    el('td', { class: 'px-3 py-2 text-right' }, Number(r.waste).toFixed(2)),
                    el('td', { class: 'px-3 py-2 text-right' }, Number(r.fixed).toFixed(2)),
                    el('td', { class: 'px-3 py-2 text-right font-bold' }, Number(r.total).toFixed(2)),
                    el('td', { class: 'px-3 py-2 text-center' },
                        el('button', {
                            class: 'text-blue-600 hover:text-blue-900',
                            title: 'Скачать квитанцию',
                            onclick: () => this.downloadReceipt(r.reading_id)
                        }, '📄')
                    )
                );
                tbody.appendChild(tr);
            });

            // Строка итогов
            tbody.appendChild(el('tr', { class: 'bg-blue-50 font-bold' },
                el('td', { colspan: 2, class: 'px-3 py-2 text-right' }, 'ИТОГО:'),
                el('td', { class: 'px-3 py-2 text-right' }, totals.hot.toFixed(2)),
                el('td', { class: 'px-3 py-2 text-right' }, totals.cold.toFixed(2)),
                el('td', { class: 'px-3 py-2 text-right' }, totals.el.toFixed(2)),
                el('td', { class: 'px-3 py-2 text-right' }, totals.main.toFixed(2)),
                el('td', { class: 'px-3 py-2 text-right' }, totals.rent.toFixed(2)),
                el('td', { class: 'px-3 py-2 text-right' }, totals.waste.toFixed(2)),
                el('td', { class: 'px-3 py-2 text-right' }, totals.fix.toFixed(2)),
                el('td', { class: 'px-3 py-2 text-right text-red-600' }, totals.sum.toFixed(2)),
                el('td', {}, '')
            ));

            table.appendChild(tbody);
            tableContainer.appendChild(table);
            card.appendChild(tableContainer);
            fragment.appendChild(card);
        }

        this.dom.container.appendChild(fragment);
    },

    // --- ФУНКЦИОНАЛ СКАЧИВАНИЯ ---

    async downloadReceipt(id) {
        toast('Генерация квитанции, подождите...', 'info');
        try {
            // 1. Инициируем задачу на бэкенде
            const res = await api.post(`/admin/receipts/${id}/generate`, {});

            // 2. Ждем завершения задачи
            const result = await this.pollTask(res.task_id);

            // 3. Скачиваем файл (ИСПРАВЛЕННЫЙ МЕТОД)
            if (result.download_url) {
                this.triggerFileDownload(result.download_url, `receipt_${id}.pdf`);
                toast('Файл скачивается', 'success');
            } else {
                throw new Error('Ссылка на файл не получена');
            }

        } catch (e) {
            console.error(e);
            toast('Ошибка при скачивании: ' + e.message, 'error');
        }
    },

    async downloadExcel() {
        if (!this.state.selectedPeriodId) {
            toast('Выберите период', 'warning');
            return;
        }

        const btn = this.dom.btnExcel;
        setLoading(btn, true, 'Скачивание...');

        try {
            // Excel скачивается напрямую через StreamingResponse, здесь полинг не нужен
            // Но используем triggerFileDownload для безопасности
            const url = `/api/admin/export_report?period_id=${this.state.selectedPeriodId}`;

            // Вариант 1: Прямой переход (может блокироваться)
            // window.location.href = url;

            // Вариант 2: Через api.download (blob)
            await api.download(url.replace('/api', ''), `report_${this.state.selectedPeriodId}.xlsx`);

            toast('Отчет скачан успешно', 'success');

        } catch (e) {
            console.error(e);
            toast('Ошибка скачивания Excel: ' + e.message, 'error');
        } finally {
            setLoading(btn, false);
        }
    },

    async downloadZip() {
        if (!this.state.selectedPeriodId) {
            toast('Выберите период', 'warning');
            return;
        }

        const btn = this.dom.btnZip;
        setLoading(btn, true, 'Формирование...');

        try {
            toast('Архив формируется. Это может занять до минуты...', 'info');

            // 1. Старт задачи
            const res = await api.post(`/admin/reports/bulk-zip?period_id=${this.state.selectedPeriodId}`, {});

            // 2. Ожидание
            const result = await this.pollTask(res.task_id);

            // 3. Скачивание
            if (result.download_url) {
                this.triggerFileDownload(result.download_url, `archive_${this.state.selectedPeriodId}.zip`);
                toast('Архив готов и скачивается', 'success');
            }

        } catch (e) {
            console.error(e);
            toast('Ошибка: ' + e.message, 'error');
        } finally {
            setLoading(btn, false);
        }
    },

    /**
     * Создает невидимую ссылку и кликает по ней.
     * Это обходит блокировку "Insecure Content" в некоторых браузерах при скачивании по HTTP.
     */
    triggerFileDownload(url, filename) {
        const link = document.createElement('a');
        link.href = url;
        link.setAttribute('download', filename || ''); // Атрибут download важен!
        link.style.display = 'none';
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
    },

    /**
     * Опрашивает сервер о статусе задачи Celery.
     */
    async pollTask(taskId) {
        const check = async () => {
            const data = await api.get(`/admin/tasks/${taskId}`);

            // Поддержка разных статусов успеха
            if (data.status === 'done' || data.status === 'ok' || data.state === 'SUCCESS') {
                return data;
            }

            if (data.state === 'FAILURE') {
                throw new Error(data.error || 'Ошибка выполнения задачи на сервере');
            }

            // Если еще выполняется - ждем 1 секунду и повторяем
            await new Promise(resolve => setTimeout(resolve, 1000));
            return check();
        };

        // Таймаут 5 минут (300000 мс) для больших архивов
        const timeout = new Promise((_, reject) =>
            setTimeout(() => reject(new Error('Время ожидания задачи истекло')), 300000)
        );

        return Promise.race([check(), timeout]);
    }
};