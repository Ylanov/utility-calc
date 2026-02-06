import { Auth } from './core/auth.js';
import { ReadingsModule } from './modules/readings.js';
import { UsersModule } from './modules/users.js';
import { TariffsModule } from './modules/tariffs.js';
import { SummaryModule } from './modules/summary.js';

// --- Глобальная проверка авторизации ---
if (!Auth.isAuthenticated()) {
    window.location.href = 'login.html';
}

document.addEventListener('DOMContentLoaded', () => {
    console.log('App initialized (Final Version)');

    // Инициализация глобальных событий (табы, логаут)
    initGlobalEvents();

    // Проверяем, какая вкладка активна при загрузке страницы, и запускаем нужный модуль
    const activeTab = document.querySelector('.tab-btn.active');
    if (activeTab) {
        const tabId = activeTab.dataset.tab;
        initModule(tabId);
    }
});

function initGlobalEvents() {
    // Обработка кнопки выхода
    const logoutBtns = document.querySelectorAll('.logout-btn');
    logoutBtns.forEach(btn => btn.addEventListener('click', (e) => {
        e.preventDefault();
        Auth.logout();
    }));

    // Логика переключения табов (делегирование событий)
    const tabsContainer = document.querySelector('.tabs');
    if (tabsContainer) {
        tabsContainer.addEventListener('click', (e) => {
            const btn = e.target.closest('.tab-btn');
            if (!btn) return;

            const tabId = btn.dataset.tab;
            if (tabId) {
                switchTab(tabId);
            }
        });
    }
}

function switchTab(tabId) {
    if (!tabId) return;

    // 1. Скрываем все контенты
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
    // 2. Деактивируем все кнопки
    document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));

    // 3. Показываем нужный контент
    const content = document.getElementById(tabId);
    if (content) content.classList.add('active');

    // 4. Активируем кнопку
    const btn = document.querySelector(`.tab-btn[data-tab="${tabId}"]`);
    if (btn) btn.classList.add('active');

    // 5. Инициализируем модуль для этой вкладки
    initModule(tabId);
}

// Маршрутизатор модулей
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

        // Вкладка system удалена
        default:
            console.warn(`Module for tab "${tabId}" not found or disabled.`);
    }
}