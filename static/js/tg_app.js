// Инициализация Telegram Web App
const tg = window.Telegram.WebApp;
tg.expand(); // Разворачиваем на весь экран
tg.ready();

// Глобальные переменные состояния
const initData = tg.initData;
let jwtToken = null;
let currentReadingId = null; // Нужно для скачивания квитанции

// Ссылки на экраны
const loginScreen = document.getElementById('loginScreen');
const dashboardScreen = document.getElementById('dashboardScreen');

/**
 * Базовая функция для запросов к нашему FastAPI
 */
async function apiFetch(url, options = {}) {
    const headers = { 'Content-Type': 'application/json' };

    // В Telegram мы не можем полагаться на куки, поэтому передаем токен в заголовке
    if (jwtToken) {
        headers['Authorization'] = `Bearer ${jwtToken}`;
    }

    options.headers = { ...headers, ...options.headers };

    const res = await fetch(url, options);
    if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Ошибка сервера' }));
        throw new Error(err.detail || 'Ошибка сервера');
    }
    return res.json();
}

/**
 * 1. Запуск приложения: Попытка входа по Telegram ID
 */
async function initApp() {
    // Если открыли не в телеграме (например, в браузере на ПК для теста)
    if (!initData) {
        console.warn("Telegram initData отсутствует. Отображается форма логина.");
        loginScreen.classList.add('active');
        bindEvents();
        return;
    }

    try {
        // Пробуем войти автоматически (бэкенд проверит Telegram ID)
        const data = await apiFetch('/api/tg/auto-login', {
            method: 'POST',
            body: JSON.stringify({ initData: initData })
        });

        // Успех! Аккаунт уже привязан.
        jwtToken = data.access_token;
        document.getElementById('userNameDisplay').innerText = data.username;
        showDashboard();
    } catch (e) {
        // Ошибка 404 (Не привязан) или другая ошибка — показываем форму входа
        loginScreen.classList.add('active');
    }

    bindEvents();
}

/**
 * 2. Загрузка данных в Личный Кабинет
 */
async function showDashboard() {
    loginScreen.classList.remove('active');
    dashboardScreen.classList.add('active');

    try {
        const state = await apiFetch('/api/readings/state');

        // Заполняем текстовые поля
        document.getElementById('periodDisplay').innerText = state.period_name;
        document.getElementById('prevHot').innerText = Number(state.prev_hot).toFixed(3);
        document.getElementById('prevCold').innerText = Number(state.prev_cold).toFixed(3);
        document.getElementById('prevElect').innerText = Number(state.prev_elect).toFixed(3);

        // Если есть сохраненный черновик — подставляем в поля ввода
        if (state.is_draft) {
            document.getElementById('inpHot').value = state.current_hot;
            document.getElementById('inpCold').value = state.current_cold;
            document.getElementById('inpElect').value = state.current_elect;
        }

        document.getElementById('totalCost').innerText = (state.total_cost || 0).toFixed(2);

        // Получаем историю, чтобы найти ID последней утвержденной квитанции для скачивания
        const history = await apiFetch('/api/readings/history');
        if (history && history.length > 0) {
            currentReadingId = history[0].id; // Берем самую последнюю квитанцию
        }

    } catch (e) {
        tg.showAlert('Ошибка загрузки данных: ' + e.message);
    }
}

/**
 * 3. Отправка показаний на сервер
 */
async function saveReadings() {
    const data = {
        hot_water: parseFloat(document.getElementById('inpHot').value),
        cold_water: parseFloat(document.getElementById('inpCold').value),
        electricity: parseFloat(document.getElementById('inpElect').value)
    };

    if (isNaN(data.hot_water) || isNaN(data.cold_water) || isNaN(data.electricity)) {
        tg.showAlert('Пожалуйста, заполните все три поля корректно.');
        return;
    }

    tg.MainButton.showProgress(); // Показываем крутилку загрузки

    try {
        const res = await apiFetch('/api/calculate', {
            method: 'POST',
            body: JSON.stringify(data)
        });

        // Обновляем итоговую сумму
        document.getElementById('totalCost').innerText = res.total_cost.toFixed(2);

        tg.HapticFeedback.notificationOccurred('success'); // Вибрация
        tg.showAlert('Показания успешно сохранены!');
        tg.MainButton.hide();

    } catch (e) {
        tg.HapticFeedback.notificationOccurred('error');
        tg.showAlert('Ошибка: ' + e.message);
    } finally {
        tg.MainButton.hideProgress();
    }
}

/**
 * 4. Скачивание PDF квитанции
 */
async function downloadReceipt() {
    if (!currentReadingId) {
        tg.showAlert('У вас пока нет сформированных квитанций.');
        return;
    }

    tg.showAlert('Генерация квитанции... Пожалуйста, подождите.');

    try {
        // Делаем запрос напрямую через fetch, чтобы вытащить Blob (файл)
        // Так как эндпоинт отдает редирект на S3, браузер скачает файл автоматически
        const response = await fetch(`/api/client/receipts/${currentReadingId}`, {
            method: 'GET',
            headers: { 'Authorization': `Bearer ${jwtToken}` }
        });

        if (!response.ok) {
            throw new Error('Не удалось получить файл');
        }

        const blob = await response.blob();
        const url = window.URL.createObjectURL(blob);

        // Создаем скрытую ссылку и эмулируем клик для старта скачивания
        const a = document.createElement('a');
        a.style.display = 'none';
        a.href = url;
        a.download = `receipt_${currentReadingId}.pdf`;
        document.body.appendChild(a);
        a.click();

        window.URL.revokeObjectURL(url);
        document.body.removeChild(a);

        tg.HapticFeedback.notificationOccurred('success');

    } catch (error) {
        tg.HapticFeedback.notificationOccurred('error');
        tg.showAlert('Ошибка скачивания: ' + error.message);
    }
}

/**
 * 5. Настройка событий интерфейса
 */
function bindEvents() {
    // --- Логин ---
    document.getElementById('btnSubmitLogin').addEventListener('click', async () => {
        const u = document.getElementById('username').value;
        const p = document.getElementById('password').value;
        const btn = document.getElementById('btnSubmitLogin');
        const err = document.getElementById('loginError');

        if(!u || !p) {
            err.innerText = 'Заполните все поля';
            return;
        }

        btn.disabled = true;
        btn.innerText = 'Проверка...';
        err.innerText = '';

        try {
            const data = await apiFetch('/api/tg/login-and-link', {
                method: 'POST',
                // Если запущен локально без ТГ, initData пустая, бэкенд должен это обрабатывать
                body: JSON.stringify({ initData: initData || "TEST", username: u, password: p })
            });

            jwtToken = data.access_token;
            document.getElementById('userNameDisplay').innerText = data.username;

            tg.HapticFeedback.notificationOccurred('success');
            showDashboard();

        } catch (e) {
            err.innerText = e.message;
            tg.HapticFeedback.notificationOccurred('error');
        } finally {
            btn.disabled = false;
            btn.innerText = 'Привязать и войти';
        }
    });

    // --- Кнопки сохранения показаний ---
    document.getElementById('btnSaveReadings').addEventListener('click', saveReadings);

    // Настраиваем Главную Кнопку Telegram (снизу экрана)
    tg.MainButton.setText("СОХРАНИТЬ ПОКАЗАНИЯ");
    tg.MainButton.onClick(saveReadings);

    // Показываем Главную Кнопку ТГ, когда пользователь начинает вводить показания
    ['inpHot', 'inpCold', 'inpElect'].forEach(id => {
        document.getElementById(id).addEventListener('input', () => {
            if(!tg.MainButton.isVisible) tg.MainButton.show();
        });
    });

    // --- Кнопка скачивания ---
    document.getElementById('btnDownloadReceipt').addEventListener('click', downloadReceipt);
}

// ЗАПУСК ПРИЛОЖЕНИЯ
initApp();