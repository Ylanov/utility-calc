// =========================================================
// 1. РАБОТА С ПОКАЗАНИЯМИ (READINGS - СВЕРКА)
// =========================================================

// Переменные, специфичные для этого модуля
let currentPage = 1;
const pageSize = 50;
let currentReadings = []; // Хранилище загруженных показаний для модального окна
let anomalyFilter = false; // Состояние фильтра аномалий

/**
 * Карта для визуализации тегов аномалий.
 */
const anomalyMap = {
    "NEGATIVE_HOT": { text: "ГВС<0", color: "#e74c3c", title: "Ошибка: Показания ГВС меньше предыдущих!" },
    "HIGH_VS_PEERS_HOT": { text: "ГВС Peers↑", color: "#9b59b6", title: "Расход ГВС значительно выше среднего по общежитию" },
    "HIGH_HOT": { text: "ГВС↑", color: "#e74c3c", title: "Очень высокий расход горячей воды" },
    "HIGH_COLD": { text: "ХВС↑", color: "#e74c3c", title: "Очень высокий расход холодной воды" },
    "HIGH_ELECT": { text: "Свет↑", color: "#e74c3c", title: "Очень высокий расход электричества" },
    "ZERO_HOT": { text: "ГВС=0", color: "#f39c12", title: "Нулевой расход горячей воды" },
    "ZERO_COLD": { text: "ХВС=0", color: "#f39c12", title: "Нулевой расход холодной воды" },
    "ZERO_ELECT": { text: "Свет=0", color: "#f39c12", title: "Нулевой расход электричества" },
    "FROZEN_HOT": { text: "ГВС❄️", color: "#3498db", title: "Счетчик ГВС не менялся 3+ мес." },
    "FROZEN_COLD": { text: "ХВС❄️", color: "#3498db", title: "Счетчик ХВС не менялся 3+ мес." },
    "FROZEN_ELECT": { text: "Свет❄️", color: "#3498db", title: "Счетчик света не менялся 3+ мес." }
};

/**
 * Генерирует HTML для отображения флагов аномалий.
 * @param {string | null} flags - Строка с флагами через запятую.
 * @returns {string} HTML-код с цветными метками.
 */
function renderAnomalies(flags) {
    if (!flags) return '<span style="color:#27ae60; font-weight: bold;">OK</span>';

    return flags.split(',').map(flag => {
        const details = anomalyMap[flag];
        if (!details) return '';
        return `<span title="${details.title}" style="display:inline-block; background:${details.color}; color:white; padding: 2px 5px; border-radius:3px; font-size:10px; margin: 1px; font-weight: bold;">
            ${details.text}
        </span>`;
    }).join(' ');
}


/**
 * Включает/выключает фильтр по аномальным показаниям.
 * @param {boolean} isChecked - Состояние чекбокса.
 */
function toggleAnomalyFilter(isChecked) {
    anomalyFilter = isChecked;
    loadReadings(1); // Перезагружаем данные с первой страницы с учетом фильтра
}


/**
 * Смена страницы для списка показаний.
 * @param {number} delta - +1 (вперед) или -1 (назад).
 */
function changePage(delta) {
    const newPage = currentPage + delta;
    if (newPage < 1) return;
    loadReadings(newPage);
}

/**
 * Загружает список неподтвержденных показаний с пагинацией.
 * @param {number} page - Номер страницы для загрузки.
 */
async function loadReadings(page = 1) {
    const tbody = document.querySelector('#readingsTable tbody');
    if (!tbody) return;

    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;">Загрузка...</td></tr>';

    const btnPrev = document.getElementById('btnPrev');
    if (btnPrev) btnPrev.disabled = (page <= 1);

    try {
        // Добавляем параметр `anomalies_only` в URL, если фильтр включен
        const url = `/api/admin/readings?page=${page}&limit=${pageSize}` + (anomalyFilter ? '&anomalies_only=true' : '');

        const response = await fetch(url, {
            headers: { 'Authorization': `Bearer ${token}` }
        });

        if (response.ok) {
            currentReadings = await response.json();
            tbody.innerHTML = '';

            currentPage = page;
            const pageIndicator = document.getElementById('pageIndicator');
            if (pageIndicator) pageIndicator.innerText = `Стр. ${currentPage}`;

            const btnNext = document.getElementById('btnNext');

            if (currentReadings.length === 0) {
                const message = anomalyFilter ? "Нет подозрительных показаний" : "Нет данных на этой странице";
                tbody.innerHTML = `<tr><td colspan="7" style="text-align:center; padding: 20px;">${message}</td></tr>`;
                if (btnNext) btnNext.disabled = true;
                return;
            } else {
                if (btnNext) btnNext.disabled = (currentReadings.length < pageSize);
            }

            currentReadings.forEach(r => {
                const tr = document.createElement('tr');
                tr.innerHTML = `
                    <td>
                        <strong>${r.username}</strong>
                        <div style="font-size:11px; color:#888;">${r.dormitory || ''}</div>
                    </td>
                    <td>${renderAnomalies(r.anomaly_flags)}</td>
                    <td>${r.cur_hot}</td>
                    <td>${r.cur_cold}</td>
                    <td>${r.cur_elect}</td>
                    <td style="color: green; font-weight: bold;">~ ${r.total_cost.toFixed(2)} ₽</td>
                    <td>
                        <button onclick="openModal(${r.id})" class="action-btn" style="padding: 5px 15px; margin: 0; font-size: 13px; background: #4a90e2;">
                            📝 Проверить
                        </button>
                    </td>
                `;
                tbody.appendChild(tr);
            });
        } else if (response.status === 401) {
            logout();
        }
    } catch (e) {
        console.error("Ошибка загрузки показаний:", e);
        tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; color:red;">Ошибка загрузки</td></tr>';
    }
}

/**
 * Открытие модального окна для коррекции.
 * @param {number} id - ID записи показаний.
 */
function openModal(id) {
    const reading = currentReadings.find(r => r.id === id);
    if (!reading) return;

    document.getElementById('modal_reading_id').value = id;
    document.getElementById('m_username').innerText = reading.username;

    const hotUsage = (reading.cur_hot - reading.prev_hot).toFixed(2);
    const coldUsage = (reading.cur_cold - reading.prev_cold).toFixed(2);
    const electUsage = (reading.cur_elect - reading.prev_elect).toFixed(2);

    document.getElementById('m_hot_usage').innerText = hotUsage;
    document.getElementById('m_cold_usage').innerText = coldUsage;
    document.getElementById('m_elect_usage').innerText = electUsage;

    document.getElementById('m_corr_hot').value = 0;
    document.getElementById('m_corr_cold').value = 0;
    document.getElementById('m_corr_elect').value = 0;
    document.getElementById('m_corr_sewage').value = 0;

    document.getElementById('approveModal').classList.add('open');
}

/**
 * Закрытие модального окна.
 */
function closeModal() {
    document.getElementById('approveModal').classList.remove('open');
}

/**
 * Отправка утвержденных данных с коррекциями на сервер.
 */
async function submitApproval() {
    const id = document.getElementById('modal_reading_id').value;
    const data = {
        hot_correction: parseFloat(document.getElementById('m_corr_hot').value) || 0,
        cold_correction: parseFloat(document.getElementById('m_corr_cold').value) || 0,
        electricity_correction: parseFloat(document.getElementById('m_corr_elect').value) || 0,
        sewage_correction: parseFloat(document.getElementById('m_corr_sewage').value) || 0
    };

    try {
        const response = await fetch(`/api/admin/approve/${id}`, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(data)
        });

        if (response.ok) {
            const result = await response.json();
            alert(`Показания утверждены!\nИтоговая сумма: ${result.new_total.toFixed(2)} руб.`);
            closeModal();
            loadReadings(currentPage); // Перезагружаем текущую страницу
        } else {
            const err = await response.json();
            alert("Ошибка: " + (err.detail || 'Неизвестная ошибка'));
        }
    } catch (e) {
        alert("Ошибка сети");
    }
}

// =========================================================
// МАССОВОЕ УТВЕРЖДЕНИЕ (BULK APPROVE)
// =========================================================

async function bulkApprove() {
    if (!confirm("ВНИМАНИЕ! \n\nЭто автоматически утвердит все черновики текущего месяца, где показания больше предыдущих.\nРучные коррекции не будут применены.\n\nПродолжить?")) {
        return;
    }

    const btn = document.querySelector('button[onclick="bulkApprove()"]');
    const oldText = btn ? btn.innerText : "Утвердить все";

    try {
        if (btn) {
            btn.innerText = "Обработка...";
            btn.disabled = true;
        }

        const response = await fetch('/api/admin/approve-bulk', {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}` }
        });

        if (response.ok) {
            const res = await response.json();
            alert(`Успешно утверждено записей: ${res.approved_count}`);
            loadReadings(1);
        } else {
            const err = await response.json();
            alert("Ошибка при массовом утверждении: " + (err.detail || "Неизвестная ошибка"));
        }
    } catch (e) {
        alert("Ошибка сети");
        console.error(e);
    } finally {
        if (btn) {
            btn.innerText = oldText;
            btn.disabled = false;
        }
    }
}

// =========================================================
// УПРАВЛЕНИЕ ПЕРИОДАМИ (ОТКРЫТИЕ / ЗАКРЫТИЕ)
// =========================================================

async function loadActivePeriod() {
    const activeDiv = document.getElementById('periodActiveState');
    const closedDiv = document.getElementById('periodClosedState');
    const label = document.getElementById('activePeriodLabel');

    try {
        const res = await fetch('/api/admin/periods/active', {
            headers: { 'Authorization': `Bearer ${token}` }
        });

        if (res.ok) {
            const data = await res.json();

            if (data && data.name) {
                // ПЕРИОД ЕСТЬ
                activeDiv.style.display = 'flex';
                closedDiv.style.display = 'none';
                label.innerText = data.name;
            } else {
                // ПЕРИОДА НЕТ (null)
                activeDiv.style.display = 'none';
                closedDiv.style.display = 'flex';
            }
        } else {
            // Если 401 или ошибка - считаем что закрыто или редирект
            console.error("Ошибка проверки периода");
        }
    } catch (e) {
        console.error("Ошибка сети:", e);
    }
}

// ФУНКЦИЯ ЗАКРЫТИЯ
async function closePeriodAction() {
    if (!confirm(`ВНИМАНИЕ!\n\nВы закрываете текущий месяц.\n\n1. Прием показаний остановится.\n2. Должникам будет начислено "по среднему".\n3. Все черновики утвердятся.\n\nПродолжить?`)) {
        return;
    }

    const btn = document.querySelector('button[onclick="closePeriodAction()"]');
    if(btn) { btn.disabled = true; btn.innerText = "Закрытие..."; }

    try {
        const response = await fetch('/api/admin/periods/close', {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}` }
        });

        if (response.ok) {
            const res = await response.json();
            alert(`Месяц успешно закрыт!\nАвто-показаний создано: ${res.auto_generated}`);
            location.reload();
        } else {
            const err = await response.json();
            alert("Ошибка: " + (err.detail || "Неизвестная ошибка"));
            if(btn) { btn.disabled = false; btn.innerText = "🔒 Закрыть месяц"; }
        }
    } catch (e) {
        alert("Ошибка сети");
        if(btn) { btn.disabled = false; btn.innerText = "🔒 Закрыть месяц"; }
    }
}

// ФУНКЦИЯ ОТКРЫТИЯ
async function openPeriodAction() {
    const nameInput = document.getElementById('newPeriodNameInput');
    const newName = nameInput ? nameInput.value.trim() : null;

    if (!newName) {
        alert("Введите название месяца (например: 'Март 2026')");
        return;
    }

    const btn = document.querySelector('button[onclick="openPeriodAction()"]');
    if(btn) { btn.disabled = true; btn.innerText = "Открытие..."; }

    try {
        const response = await fetch('/api/admin/periods/open', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${token}`
            },
            body: JSON.stringify({ name: newName })
        });

        if (response.ok) {
            alert(`Новый месяц "${newName}" успешно открыт!\nПользователи могут подавать показания.`);
            location.reload();
        } else {
            const err = await response.json();
            alert("Ошибка: " + (err.detail || "Неизвестная ошибка"));
            if(btn) { btn.disabled = false; btn.innerText = "📂 Открыть новый месяц"; }
        }
    } catch (e) {
        alert("Ошибка сети");
        if(btn) { btn.disabled = false; btn.innerText = "📂 Открыть новый месяц"; }
    }
}

// =========================================================
// ИНИЦИАЛИЗАЦИЯ
// =========================================================

document.addEventListener('DOMContentLoaded', () => {
    // Если мы на странице админки, загружаем период
    if (document.getElementById('currentPeriodLabel')) {
        loadActivePeriod();
    }
});