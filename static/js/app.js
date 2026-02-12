// static/js/app.js
import { Auth } from './core/auth.js';
import { toast } from './core/dom.js';

// Подключаем все модули бизнес-логики
import { ReadingsModule } from './modules/readings.js';
import { UsersModule } from './modules/users.js';
import { TariffsModule } from './modules/tariffs.js';
import { SummaryModule } from './modules/summary.js';

// --- 1. Глобальная проверка авторизации ---
// Если в памяти нет токена, сразу выкидываем на страницу логина.
// Используем replace, чтобы сломанная страница не оставалась в истории браузера.
if (!Auth.isAuthenticated()) {
    window.location.replace('login.html');
}

// --- 2. Глобальный перехватчик ошибок (Error Boundary) ---
// Ловит ошибки в Promise (fetch, async/await)
window.addEventListener('unhandledrejection', (event) => {
    console.error('Unhandled Rejection:', event.reason);

    // Игнорируем ошибки отмены запросов (AbortController), это нормальное поведение
    if (event.reason && event.reason.name === 'AbortError') return;

    const msg = event.reason?.message || 'Неизвестная ошибка сервера';
    toast(`Системная ошибка: ${msg}`, 'error');
});

// Ловит обычные JS ошибки
window.addEventListener('error', (event) => {
    console.error('Global Error:', event.error);
    toast(`Ошибка приложения: ${event.message}`, 'error');
});

// --- 3. Инициализация приложения ---
document.addEventListener('DOMContentLoaded', () => {
    console.log('Admin App Initialized');

    setupHeader();
    setupGlobalEvents();
    setupRouting();
});

function setupHeader() {
    // Выводим имя текущего админа в шапку
    const adminNameEl = document.getElementById('adminName');
    const username = Auth.getUsername();

    if (adminNameEl && username) {
        adminNameEl.textContent = `Администратор: ${username}`;
    }
}

function setupGlobalEvents() {
    // Обработка кнопок выхода
    const logoutBtns = document.querySelectorAll('.logout-btn');
    logoutBtns.forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            Auth.logout();
        });
    });

    // Делегирование событий для переключения табов
    const tabsContainer = document.querySelector('.tabs');
    if (tabsContainer) {
        tabsContainer.addEventListener('click', (e) => {
            const btn = e.target.closest('.tab-btn');
            if (!btn) return;

            const tabId = btn.dataset.tab;
            if (tabId) {
                // Меняем URL (хэш). Событие hashchange (ниже) сделает всё остальное.
                window.location.hash = tabId;
            }
        });
    }
}

function setupRouting() {
    // Слушаем изменение URL (когда пользователь жмет "Назад"/"Вперед" в браузере)
    window.addEventListener('hashchange', handleRoute);

    // Вызываем первый раз при загрузке страницы
    handleRoute();
}

function handleRoute() {
    // Убираем символ '#' из URL
    const hash = window.location.hash.substring(1);
    const defaultTab = 'readings';

    // Если хэш пустой или такого таба нет, используем дефолтный
    const tabToLoad = (hash && isValidTab(hash)) ? hash : defaultTab;

    switchTab(tabToLoad);
}

function isValidTab(tabId) {
    return !!document.querySelector(`.tab-btn[data-tab="${tabId}"]`);
}

function switchTab(tabId) {
    if (!tabId) return;

    // 1. Скрываем весь контент
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));

    // 2. Деактивируем все кнопки вкладок
    document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));

    // 3. Показываем нужный контент
    const content = document.getElementById(tabId);
    if (content) content.classList.add('active');

    // 4. Активируем нужную кнопку
    const btn = document.querySelector(`.tab-btn[data-tab="${tabId}"]`);
    if (btn) btn.classList.add('active');

    // 5. Инициализируем соответствующий модуль (загрузка данных)
    initModule(tabId);
}

// Маршрутизатор: какой модуль запускать для какой вкладки
function initModule(tabId) {
    switch (tabId) {
        case 'readings':
            ReadingsModule.init();
            break;
        case 'users':
            UsersModule.init();
            break;
        case 'tariffs':
            TariffsModule.init();
            break;
        case 'accountant':
            SummaryModule.init();
            break;
        default:
            console.warn(`Модуль для вкладки "${tabId}" не найден.`);
    }
}