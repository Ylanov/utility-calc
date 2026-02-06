// static/js/modules/summary.js
import { api } from '../core/api.js';
import { el, clear, setLoading } from '../core/dom.js';

export const SummaryModule = {
    init() {
        // Кнопка Обновить
        const btnRefresh = document.getElementById('btnRefreshSummary');
        if (btnRefresh) btnRefresh.addEventListener('click', () => this.load());

        // Кнопка Скачать Excel
        const btnExcel = document.getElementById('btnDownloadExcel');
        if (btnExcel) btnExcel.addEventListener('click', () => this.downloadExcel(btnExcel));

        this.load();
    },

    async load() {
        const container = clear('summaryContainer');
        // Спиннер
        container.appendChild(el('p', { style: { textAlign: 'center', color: '#888' } }, 'Загрузка данных...'));

        try {
            const data = await api.get('/admin/summary');
            container.innerHTML = ''; // Очищаем спиннер

            if (!data || Object.keys(data).length === 0) {
                container.appendChild(el('p', { style: { textAlign: 'center', padding: '20px' } }, 'Нет данных о начислениях.'));
                return;
            }

            // Группировка по общежитиям
            for (const [dormName, records] of Object.entries(data)) {
                this.renderDormBlock(container, dormName, records);
            }

        } catch (error) {
            container.innerHTML = '';
            container.appendChild(el('p', { style: { color: 'red', textAlign: 'center' } }, 'Ошибка загрузки: ' + error.message));
        }
    },

    renderDormBlock(container, dormName, records) {
        // Заголовок общежития
        const section = el('div', {
            style: {
                marginBottom: "40px", background: "#fff", borderRadius: "8px",
                boxShadow: "0 2px 5px rgba(0,0,0,0.05)", padding: "15px"
            }
        });

        section.appendChild(el('h3', {
            style: {
                background: "#f8f9fa", padding: "10px", borderRadius: "5px",
                marginBottom: "15px", borderLeft: "5px solid #4a90e2"
            }
        }, `🏠 Общежитие: ${dormName}`));

        // Таблица
        const table = el('table', { style: { width: "100%", borderCollapse: "collapse", fontSize: "13px" } });

        // Шапка
        const thead = el('thead', {}, el('tr', { style: { background: "#f1f1f1" } },
            el('th', {}, 'Дата'), el('th', {}, 'Жилец'),
            el('th', {}, 'Г.В.'), el('th', {}, 'Х.В.'), el('th', {}, 'Канал.'), el('th', {}, 'Свет'),
            el('th', {}, 'Содерж.'), el('th', {}, 'Наем'), el('th', {}, 'Мусор'), el('th', {}, 'Отопл.'),
            el('th', {}, 'ИТОГО'), el('th', {}, 'PDF')
        ));
        table.appendChild(thead);

        const tbody = el('tbody', {});

        // Суммы
        const totals = { hot: 0, cold: 0, sew: 0, el: 0, main: 0, rent: 0, waste: 0, fix: 0, sum: 0 };

        records.forEach(r => {
            // Агрегация сумм
            totals.hot += r.hot; totals.cold += r.cold; totals.sew += r.sewage;
            totals.el += r.electric; totals.main += r.maintenance; totals.rent += r.rent;
            totals.waste += r.waste; totals.fix += r.fixed; totals.sum += r.total;

            const tr = el('tr', {},
                el('td', {}, r.date.split(' ')[0]),
                el('td', {},
                    el('strong', {}, r.username),
                    el('br'),
                    el('span', { style: {fontSize: '10px', color: '#777'} }, `${r.area}м² / ${r.residents} чел`)
                ),
                el('td', {}, r.hot.toFixed(2)), el('td', {}, r.cold.toFixed(2)),
                el('td', {}, r.sewage.toFixed(2)), el('td', {}, r.electric.toFixed(2)),
                el('td', {}, r.maintenance.toFixed(2)), el('td', {}, r.rent.toFixed(2)),
                el('td', {}, r.waste.toFixed(2)), el('td', {}, r.fixed.toFixed(2)),
                el('td', { style: {fontWeight: 'bold'} }, r.total.toFixed(2)),
                el('td', {},
                    // Кнопка PDF
                    el('button', {
                        title: 'Сформировать квитанцию',
                        style: { cursor: 'pointer', marginRight: '5px' },
                        onclick: (e) => this.downloadReceipt(r.reading_id, e.target)
                    }, '📄'),
                    // Кнопка Удалить
                    el('button', {
                        title: 'Удалить запись',
                        style: { cursor: 'pointer' },
                        onclick: () => this.deleteRecord(r.reading_id)
                    }, '🗑')
                )
            );
            tbody.appendChild(tr);
        });

        // Строка итогов
        const footer = el('tr', { style: { background: "#e8f5e9", fontWeight: "bold" } },
            el('td', { colspan: 2 }, 'ИТОГО:'),
            el('td', {}, totals.hot.toFixed(2)), el('td', {}, totals.cold.toFixed(2)),
            el('td', {}, totals.sew.toFixed(2)), el('td', {}, totals.el.toFixed(2)),
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

    // Удаление записи
    async deleteRecord(id) {
        if (!confirm("Удалить эту запись?")) return;
        try {
            await api.delete(`/admin/readings/${id}`);
            this.load();
        } catch (error) {
            alert("Ошибка сети: " + error.message);
        }
    },

    // Генерация и скачивание PDF через Celery
    async downloadReceipt(readingId, btn) {
        const originalText = btn.innerHTML;
        // Простой CSS спиннер
        btn.disabled = true;
        btn.innerHTML = '⏳';

        try {
            // 1. Запуск задачи
            const startData = await api.post(`/admin/receipts/${readingId}/generate`, {});
            const taskId = startData.task_id;

            // 2. Ожидание (polling)
            const result = await this.pollTaskStatus(taskId);

            // 3. Скачивание готового файла по ссылке от бэкенда
            // Ссылка приходит вида /static/generated_files/filename.pdf
            // Мы используем обычный download через создание ссылки
            const link = document.createElement('a');
            link.href = result.download_url;
            link.download = `receipt_${readingId}.pdf`;
            link.target = '_blank';
            document.body.appendChild(link);
            link.click();
            link.remove();

        } catch (error) {
            alert("Ошибка скачивания: " + error.message);
        } finally {
            btn.disabled = false;
            btn.innerHTML = originalText;
        }
    },

    async pollTaskStatus(taskId) {
        const pollInterval = 1000;
        const maxAttempts = 60; // 60 секунд

        for (let i = 0; i < maxAttempts; i++) {
            const data = await api.get(`/admin/tasks/${taskId}`);

            if (data.status === 'done' || data.state === 'SUCCESS') {
                return data;
            }
            if (data.state === 'FAILURE') {
                throw new Error(data.error || "Ошибка генерации");
            }
            // Ждем перед следующим опросом
            await new Promise(r => setTimeout(r, pollInterval));
        }
        throw new Error("Таймаут: сервер долго формирует файл.");
    },

    async downloadExcel(btn) {
        setLoading(btn, true, 'Скачивание...');
        try {
            await api.download('/admin/export_report', 'report.xlsx');
        } catch (error) {
            alert("Ошибка скачивания: " + error.message);
        } finally {
            setLoading(btn, false);
        }
    }
};