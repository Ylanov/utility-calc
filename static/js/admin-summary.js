// =========================================================
// 5. СВОДКА (ACCOUNTANT - БУХГАЛТЕРИЯ)
// =========================================================

async function loadAccountantSummary() {

    const container = document.getElementById('summaryContainer');
    if (!container) return;

    container.innerHTML = '<p style="text-align:center; color:#888;">Загрузка...</p>';

    try {

        const response = await fetch('/api/admin/summary', {
            headers: { 'Authorization': `Bearer ${token}` }
        });

        if (!response.ok) {

            if (response.status === 401) {
                logout();
                return;
            }

            throw new Error("Ошибка сервера");
        }

        const data = await response.json();

        container.innerHTML = '';

        if (Object.keys(data).length === 0) {

            container.innerHTML =
                '<p style="text-align:center; padding: 20px;">Нет данных о начислениях.</p>';
            return;
        }


        for (const [dormName, records] of Object.entries(data)) {

            const section = document.createElement('div');

            section.style.marginBottom = "40px";
            section.style.background = "#fff";
            section.style.borderRadius = "8px";
            section.style.boxShadow = "0 2px 5px rgba(0,0,0,0.05)";
            section.style.padding = "15px";

            section.innerHTML = `
                <h3 style="background:#f8f9fa; padding:10px; border-radius:5px; 
                margin-bottom:15px; border-left: 5px solid #4a90e2;">
                    🏠 Общежитие: ${dormName}
                </h3>
            `;


            const table = document.createElement('table');

            table.style.width = "100%";
            table.style.borderCollapse = "collapse";
            table.style.fontSize = "13px";


            table.innerHTML = `
                <thead>
                    <tr style="background:#f1f1f1;">
                        <th>Дата</th>
                        <th>Жилец</th>
                        <th>Г.В.</th>
                        <th>Х.В.</th>
                        <th>Канал.</th>
                        <th>Свет</th>
                        <th>Содерж.</th>
                        <th>Наем</th>
                        <th>Мусор</th>
                        <th>Отопл.</th>
                        <th>ИТОГО</th>
                        <th>PDF</th>
                    </tr>
                </thead>
                <tbody></tbody>
            `;

            const tbody = table.querySelector('tbody');


            let tHot = 0, tCold = 0, tSew = 0, tEl = 0,
                tMain = 0, tRent = 0, tWaste = 0,
                tFix = 0, tTotal = 0;


            records.forEach(r => {

                tHot += r.hot;
                tCold += r.cold;
                tSew += r.sewage;
                tEl += r.electric;
                tMain += r.maintenance;
                tRent += r.rent;
                tWaste += r.waste;
                tFix += r.fixed;
                tTotal += r.total;


                const tr = document.createElement('tr');

                tr.innerHTML = `
                    <td>${r.date.split(' ')[0]}</td>

                    <td>
                        <strong>${r.username}</strong><br>
                        <span style="font-size:10px;color:#777">
                            ${r.area}м² / ${r.residents} чел
                        </span>
                    </td>

                    <td>${r.hot.toFixed(2)}</td>
                    <td>${r.cold.toFixed(2)}</td>
                    <td>${r.sewage.toFixed(2)}</td>
                    <td>${r.electric.toFixed(2)}</td>
                    <td>${r.maintenance.toFixed(2)}</td>
                    <td>${r.rent.toFixed(2)}</td>
                    <td>${r.waste.toFixed(2)}</td>
                    <td>${r.fixed.toFixed(2)}</td>

                    <td style="font-weight:bold">
                        ${r.total.toFixed(2)}
                    </td>

                    <td>
                        <!-- ОБНОВЛЕНО: Передаем 'this' для анимации кнопки -->
                        <button onclick="downloadReceipt(${r.reading_id}, this)" style="cursor: pointer;" title="Сформировать квитанцию">
                            📄
                        </button>

                        <button onclick="deleteRecord(${r.reading_id})" style="cursor: pointer;" title="Удалить запись">
                            🗑
                        </button>
                    </td>
                `;

                tbody.appendChild(tr);

            });


            const footer = document.createElement('tr');

            footer.style.background = "#e8f5e9";
            footer.style.fontWeight = "bold";

            footer.innerHTML = `
                <td colspan="2">ИТОГО:</td>

                <td>${tHot.toFixed(2)}</td>
                <td>${tCold.toFixed(2)}</td>
                <td>${tSew.toFixed(2)}</td>
                <td>${tEl.toFixed(2)}</td>
                <td>${tMain.toFixed(2)}</td>
                <td>${tRent.toFixed(2)}</td>
                <td>${tWaste.toFixed(2)}</td>
                <td>${tFix.toFixed(2)}</td>

                <td style="color:#c0392b">
                    ${tTotal.toFixed(2)}
                </td>

                <td></td>
            `;

            tbody.appendChild(footer);


            section.appendChild(table);
            container.appendChild(section);

        }

    } catch (err) {

        console.error(err);

        container.innerHTML =
            '<p style="color:red;text-align:center;">Ошибка загрузки</p>';
    }
}

// =========================================================
// УТИЛИТА: ОПРОС СТАТУСА ЗАДАЧИ (POLLING)
// =========================================================

async function pollTaskStatus(taskId) {
    const pollInterval = 1000; // Опрос каждую секунду
    const maxAttempts = 60; // Максимум 60 секунд ожидания (1 минута)

    for (let i = 0; i < maxAttempts; i++) {
        try {
            const res = await fetch(`/api/admin/tasks/${taskId}`, {
                headers: { 'Authorization': `Bearer ${token}` }
            });

            if (!res.ok) throw new Error("Ошибка проверки статуса");

            const data = await res.json();

            // Если задача готова
            if (data.status === 'done' || data.state === 'SUCCESS') {
                return data; // Возвращаем результат (ссылку на файл)
            }

            // Если ошибка
            if (data.state === 'FAILURE') {
                throw new Error(data.error || "Ошибка генерации на сервере");
            }

            // Если еще делается (PENDING/STARTED/RETRY) - ждем
            await new Promise(resolve => setTimeout(resolve, pollInterval));

        } catch (e) {
            console.error("Polling error:", e);
            throw e;
        }
    }
    throw new Error("Таймаут ожидания задачи (сервер перегружен)");
}


// =========================================================
// УДАЛЕНИЕ
// =========================================================

async function deleteRecord(id) {

    if (!confirm("Удалить запись?")) return;

    try {

        const res = await fetch(`/api/admin/readings/${id}`, {
            method: 'DELETE',
            headers: { 'Authorization': `Bearer ${token}` }
        });

        if (!res.ok) {
            throw new Error();
        }

        loadAccountantSummary();

    } catch {

        alert("Ошибка сети");
    }
}


// =========================================================
// СКАЧИВАНИЕ PDF (АСИНХРОННОЕ / CELERY)
// =========================================================

async function downloadReceipt(readingId, btnElement) {

    // 1. Сохраняем исходное состояние кнопки
    const originalContent = btnElement ? btnElement.innerHTML : '📄';

    // 2. Включаем индикацию загрузки
    if (btnElement) {
        btnElement.disabled = true;
        // Простой CSS спиннер внутри кнопки
        btnElement.innerHTML = '<span style="display:inline-block; width:12px; height:12px; border:2px solid #ccc; border-top-color:#333; border-radius:50%; animation: spin 1s linear infinite;"></span>';
        // Добавляем стиль анимации, если его нет глобально
        if (!document.getElementById('spinStyle')) {
            const style = document.createElement('style');
            style.id = 'spinStyle';
            style.innerHTML = '@keyframes spin { to { transform: rotate(360deg); } }';
            document.head.appendChild(style);
        }
    }

    try {

        // 3. Запускаем задачу генерации на сервере
        const startRes = await fetch(`/api/admin/receipts/${readingId}/generate`, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            }
        });

        if (!startRes.ok) {
            if (startRes.status === 401) {
                logout();
                return;
            }
            throw new Error("Не удалось запустить генерацию");
        }

        const startData = await startRes.json();
        const taskId = startData.task_id;

        // 4. Ждем выполнения задачи (Polling)
        const result = await pollTaskStatus(taskId);

        // 5. Скачиваем файл
        // result.download_url приходит с бэкенда (например: "/static/generated_files/receipt_1.pdf")
        const link = document.createElement('a');
        link.href = result.download_url;
        link.download = result.filename || `receipt_${readingId}.pdf`;
        link.target = '_blank'; // Открываем в новой вкладке для надежности
        document.body.appendChild(link);
        link.click();
        link.remove();

    } catch (err) {

        console.error(err);
        alert("Ошибка скачивания: " + err.message);

    } finally {

        // 6. Возвращаем кнопку в исходное состояние
        if (btnElement) {
            btnElement.disabled = false;
            btnElement.innerHTML = originalContent;
        }
    }
}


// =========================================================
// ЭКСПОРТ В EXCEL
// =========================================================

function exportTableToExcel() {

    let html = document.getElementById('summaryContainer').innerHTML;

    html = '<meta charset="UTF-8">' + html;

    const blob = new Blob([html], {
        type: 'application/vnd.ms-excel'
    });

    const a = document.createElement('a');

    a.href = URL.createObjectURL(blob);

    a.download =
        `Svodka_${new Date().toISOString().slice(0, 10)}.xls`;

    document.body.appendChild(a);

    a.click();

    a.remove();
}

async function downloadRealExcel() {
    try {
        const res = await fetch('/api/admin/export_report', {
            headers: { 'Authorization': `Bearer ${token}` }
        });

        if (res.ok) {
            const blob = await res.blob();

            // Пытаемся достать имя файла из заголовков
            let filename = "report.xlsx";
            const disposition = res.headers.get('content-disposition');
            if (disposition && disposition.indexOf('attachment') !== -1) {
                const filenameRegex = /filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/;
                const matches = filenameRegex.exec(disposition);
                if (matches != null && matches[1]) {
                    filename = matches[1].replace(/['"]/g, '');
                }
            }

            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            a.remove();
        } else {
            alert("Не удалось скачать отчет");
        }
    } catch (e) {
        console.error(e);
        alert("Ошибка сети");
    }
}