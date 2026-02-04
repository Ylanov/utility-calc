// =========================================================
// 0. ГЛАВНЫЙ УПРАВЛЯЮЩИЙ СКРИПТ
// =========================================================

// Проверка авторизации: если токена нет, редирект
if (!localStorage.getItem('token')) {
    window.location.href = 'login.html';
}

const token = localStorage.getItem('token');

/**
 * Переключение между вкладками
 * @param {string} tabId - ID вкладки для открытия
 */
function openTab(tabId) {
    // Скрыть контент всех вкладок
    document.querySelectorAll('.tab-content').forEach(tab => {
        tab.classList.remove('active');
    });
    // Убрать класс active у всех кнопок
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.classList.remove('active');
    });

    // Показать нужную вкладку
    const activeTab = document.getElementById(tabId);
    if (activeTab) activeTab.classList.add('active');

    // Найти и подсветить нужную кнопку
    const activeButton = Array.from(document.querySelectorAll('.tab-btn')).find(btn => btn.getAttribute('onclick').includes(`'${tabId}'`));
    if (activeButton) activeButton.classList.add('active');


    // Загрузка данных для специфичных вкладок при их открытии
    if (tabId === 'accountant') {
        loadAccountantSummary();
    }
}

// =========================================================
// ИНИЦИАЛИЗАЦИЯ ПРИ ЗАГРУЗКЕ СТРАНИЦЫ
// =========================================================

document.addEventListener('DOMContentLoaded', () => {
    // Открываем первую вкладку по умолчанию
    openTab('readings');

    // Загружаем данные для вкладок, которые должны быть готовы сразу
    loadReadings(1); // Загружаем первую страницу показаний
    loadUsers();
    loadTariffs();

    // ИСПРАВЛЕНИЕ: Инициализация кнопок управления периодом (Открыть/Закрыть месяц)
    // Проверяем наличие контейнеров управления периодом и запускаем загрузку статуса
    if (document.getElementById('periodActiveState') || document.getElementById('periodClosedState')) {
        loadActivePeriod();
    }
});