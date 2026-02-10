// static/js/modules/summary.js
import { api } from '../core/api.js';
import { el, clear, setLoading, toast } from '../core/dom.js';

export const SummaryModule = {
    isInitialized: false,
    controller: null, // Для отмены предыдущих запросов загрузки

    // Храним текущий выбранный период
    state: {
        selectedPeriodId: null
    },

    // ============================================================
    // ИНИЦИАЛИЗАЦИЯ
    // ============================================================
    async init() {
        if (!this.isInitialized) {
            this.setupEventListeners();
            this.isInitialized = true;
        }

        // Загружаем список периодов.
        // Убрали проверку children.length, чтобы гарантированно перезаписать
        // текст-плейсхолдер "Загрузка периодов..." на реальный селект.
        const selectorContainer = document.getElementById('summaryPeriodSelector');
        if (selectorContainer) {
            await this.loadPeriods(selectorContainer);
        }

        // Загружаем данные (используя выбранный период или дефолтный)
        this.loadData();
    },

    setupEventListeners() {
        console.log('SummaryModule: Event listeners setup.');

        // Кнопка Обновить
        const btnRefresh = document.getElementById('btnRefreshSummary');
        if (btnRefresh) {
            btnRefresh.addEventListener('click', () => this.loadData());
        }

        // Кнопка Скачать Excel
        const btnExcel = document.getElementById('btnDownloadExcel');
        if (btnExcel) {
            btnExcel.addEventListener('click', (e) => this.downloadExcel(e.target));
        }

        // Кнопка Скачать ZIP архив
        const btnZip = document.getElementById('btnDownloadZip');
        if (btnZip) {
            btnZip.addEventListener('click', (e) => this.downloadZip(e.target));
        }
    },

    // ============================================================
    // ЗАГРУЗКА ДАННЫХ
    // ============================================================
    async loadPeriods(container) {
        // Очищаем контейнер перед загрузкой, чтобы убрать любой мусор или текст загрузки
        container.innerHTML = '';

        try {
            const periods = await api.get('/admin/periods/history');

            if (!periods || periods.length === 0) {
                container.textContent = "Нет доступных периодов";
                return;
            }

            // Создаем select
            const select = el('select', {
                class: 'border p-2 rounded',
                style: { fontSize: '14px', minWidth: '200px', cursor: 'pointer' },
                onchange: (e) => {
                    this.state.selectedPeriodId = e.target.value;
                    this.loadData();
                }
            });

            periods.forEach(p => {
                const isActive = p.is_active ? ' (Активен)' : '';
                const option = el('option', { value: p.id }, `${p.name}${isActive}`);
                select.appendChild(option);
            });

            // Выбираем первый (самый новый) по умолчанию
            if (!this.state.selectedPeriodId && periods.length > 0) {
                this.state.selectedPeriodId = periods[0].id;
                select.value = periods[0].id;
            } else if (this.state.selectedPeriodId) {
                select.value = this.state.selectedPeriodId;
            }

            container.appendChild(el('span', { class: 'mr-2 font-bold text-gray-600' }, 'Период: '));
            container.appendChild(select);

        } catch (e) {
            console.error("Ошибка загрузки периодов:", e);
            container.innerHTML = '<span style="color:red">Ошибка загрузки списка</span>';
            toast("Не удалось загрузить периоды", "error");
        }
    },

    async loadData() {
        // Отменяем предыдущий запрос, если он еще идет
        if (this.controller) {
            this.controller.abort();
        }
        this.controller = new AbortController();
        const signal = this.controller.signal;

        const container = clear('summaryContainer');
        container.appendChild(el('p', { style: { textAlign: 'center', color: '#888', padding: '40px' } }, '⏳ Загрузка сводки...'));

        try {
            let url = '/admin/summary';
            if (this.state.selectedPeriodId) {
                url += `?period_id=${this.state.selectedPeriodId}`;
            }

            const data = await api.get(url, { signal });

            container.innerHTML = ''; // Очищаем "Загрузка..."

            if (!data || Object.keys(data).length === 0) {
                container.appendChild(el('div', { class: 'text-center p-8 bg-gray-50 rounded-lg' },
                    el('p', { class: 'text-gray-500 text-lg' }, 'Нет начислений за выбранный период.')
                ));
                return;
            }

            for (const [dormName, records] of Object.entries(data)) {
                this.renderDormBlock(container, dormName, records);
            }

        } catch (error) {
            if (error.name === 'AbortError') return; // Игнорируем отмену

            container.innerHTML = '';
            container.appendChild(el('p', { style: { color: 'red', textAlign: 'center', padding: '20px' } }, 'Ошибка загрузки: ' + error.message));
        }
    },

    renderDormBlock(container, dormName, records) {
        // Используем card для красивого оформления блока
        const section = el('div', { class: 'card' });

        section.appendChild(el('h3', {
            style: { background: "#f8f9fa", padding: "10px", borderRadius: "5px", marginBottom: "15px", borderLeft: "5px solid #4a90e2" }
        }, `🏠 Общежитие: ${dormName}`));

        const table = el('table', { style: { width: "100%", borderCollapse: "collapse", fontSize: "13px" } });

        const thead = el('thead', {}, el('tr', { style: { background: "#f1f1f1" } },
            el('th', {}, 'Дата'), el('th', {}, 'Жилец'), el('th', {}, 'Г.В.'), el('th', {}, 'Х.В.'),
            el('th', {}, 'Свет'), el('th', {}, 'Содерж.'), el('th', {}, 'Наем'),
            el('th', {}, 'Мусор'), el('th', {}, 'Отопл.'), el('th', {}, 'ИТОГО'), el('th', {}, 'Действия')
        ));
        table.appendChild(thead);

        const tbody = el('tbody', {});
        const totals = { hot: 0, cold: 0, sew: 0, el: 0, main: 0, rent: 0, waste: 0, fix: 0, sum: 0 };

        records.forEach(r => {
            Object.keys(totals).forEach(key => totals[key] += Number(r[key] || r.sewage || r.electric || r.maintenance || r.total || 0));

            const tr = el('tr', {},
                el('td', {}, r.date.split(' ')[0]),
                el('td', {},
                    el('strong', {}, r.username), el('br'),
                    el('span', { style: {fontSize: '10px', color: '#777'} }, `${r.area}м² / ${r.residents} чел`)
                ),
                el('td', {}, Number(r.hot).toFixed(2)), el('td', {}, Number(r.cold).toFixed(2)),
                el('td', {}, Number(r.electric).toFixed(2)),
                el('td', {}, Number(r.maintenance).toFixed(2)), el('td', {}, Number(r.rent).toFixed(2)),
                el('td', {}, Number(r.waste).toFixed(2)), el('td', {}, Number(r.fixed).toFixed(2)),
                el('td', { style: {fontWeight: 'bold'} }, Number(r.total).toFixed(2)),
                el('td', {},
                    // Используем компактные кнопки-иконки для действий
                    el('div', { class: 'controls-group', style: { gap: '5px' } },
                        el('button', {
                            class: 'btn-icon btn-doc',
                            title: 'Скачать квитанцию',
                            onclick: (e) => this.downloadReceipt(r.reading_id, e.target)
                        }, '📄'),
                        el('button', {
                            class: 'btn-icon btn-delete',
                            title: 'Удалить запись',
                            onclick: () => this.deleteRecord(r.reading_id)
                        }, '🗑')
                    )
                )
            );
            tbody.appendChild(tr);
        });

        const footer = el('tr', { style: { background: "#e8f5e9", fontWeight: "bold" } },
            el('td', { colspan: 2 }, 'ИТОГО:'),
            el('td', {}, totals.hot.toFixed(2)), el('td', {}, totals.cold.toFixed(2)),
            el('td', {}, totals.el.toFixed(2)),
            el('td', {}, totals.main.toFixed(2)), el('td', {}, totals.rent.toFixed(2)),
            el('td', {}, totals.waste.toFixed(2)), el('td', {}, totals.fix.toFixed(2)),
            el('td', { style: { color: '#c0392b' } }, totals.sum.toFixed(2)),
            el('td', {}, '')
        );
        tbody.appendChild(footer);

        table.appendChild(tbody);
        section.appendChild(table);
        container.appendChild(section);
    },

    async deleteRecord(id) {
        if (!confirm("Удалить эту запись?")) return;
        try {
            await api.delete(`/admin/readings/${id}`);
            toast("Запись удалена", "success");
            this.loadData();
        } catch (error) {
            toast("Ошибка удаления: " + error.message, "error");
        }
    },

    // ============================================================
    // СКАЧИВАНИЕ ФАЙЛОВ
    // ============================================================
    async downloadReceipt(readingId, btn) {
        const originalText = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '⏳';

        try {
            const startData = await api.post(`/admin/receipts/${readingId}/generate`, {});
            const taskId = startData.task_id;

            // Запускаем опрос статуса задачи
            const result = await this.pollTaskStatus(taskId);

            const link = document.createElement('a');
            link.href = result.download_url;
            link.download = `receipt_${readingId}.pdf`;
            link.target = '_blank';
            document.body.appendChild(link);
            link.click();
            link.remove();

            toast("Квитанция скачана", "success");
        } catch (error) {
            toast("Ошибка скачивания: " + error.message, "error");
        } finally {
            btn.disabled = false;
            btn.innerHTML = originalText;
        }
    },

    async downloadZip(btn) {
        const originalText = btn.innerHTML;
        setLoading(btn, true, 'Формирование...');

        try {
            let url = '/admin/reports/bulk-zip';
            if (this.state.selectedPeriodId) {
                url += `?period_id=${this.state.selectedPeriodId}`;
            }

            const startData = await api.post(url, {});
            const taskId = startData.task_id;

            toast("Архив формируется, пожалуйста подождите...", "info");

            const result = await this.pollTaskStatus(taskId);

            if (result && result.download_url) {
                const link = document.createElement('a');
                link.href = result.download_url;
                link.download = result.filename || `receipts_${this.state.selectedPeriodId}.zip`;
                link.target = '_blank';
                document.body.appendChild(link);
                link.click();
                link.remove();
                toast(`Архив готов! Файлов: ${result.count}`, "success");
            } else {
                throw new Error("Не удалось получить ссылку на скачивание.");
            }
        } catch (error) {
            toast("Ошибка формирования архива: " + error.message, "error");
        } finally {
            setLoading(btn, false, originalText);
        }
    },

    async pollTaskStatus(taskId) {
        const pollInterval = 1500;
        const maxAttempts = 120; // 3 минуты макс

        for (let i = 0; i < maxAttempts; i++) {
            const data = await api.get(`/admin/tasks/${taskId}`);

            if (data.state === 'SUCCESS' || data.status === 'done') {
                return data.result || data;
            }
            if (data.state === 'FAILURE') {
                throw new Error(data.error || "Ошибка генерации на сервере");
            }
            // Ждем перед следующим опросом
            await new Promise(r => setTimeout(r, pollInterval));
        }
        throw new Error("Таймаут: сервер слишком долго формирует файл.");
    },

    async downloadExcel(btn) {
        setLoading(btn, true, 'Скачивание...');
        try {
            let url = '/admin/export_report';
            if (this.state.selectedPeriodId) {
                url += `?period_id=${this.state.selectedPeriodId}`;
            }
            await api.download(url, `report_${this.state.selectedPeriodId}.xlsx`);
            toast("Отчет Excel скачан", "success");
        } catch (error) {
            toast("Ошибка скачивания: " + error.message, "error");
        } finally {
            setLoading(btn, false);
        }
    }
};