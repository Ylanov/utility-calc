// static/js/app.js
import { api } from './core/api.js';
import { Auth } from './core/auth.js';
import { toast, setLoading } from './core/dom.js';
import { TotpSetup } from './core/totp.js';

// --- ГЛОБАЛЬНЫЙ РЕЕСТР ЗАГРУЖЕННЫХ МОДУЛЕЙ (LAZY LOADING) ---
// Сюда мы будем сохранять скачанные JS-модули, чтобы не качать их повторно
const loadedModules = {};

// --- 1. Глобальная проверка авторизации ---
// Если в памяти нет токена (роли), сразу выкидываем на страницу логина.
if (!Auth.isAuthenticated()) {
    window.location.replace('login.html');
}

// --- 2. Глобальный перехватчик ошибок (Error Boundary) ---
window.addEventListener('unhandledrejection', (event) => {
    console.error('Unhandled Rejection:', event.reason);

    // Игнорируем ошибки отмены запросов (AbortController)
    if (event.reason && event.reason.name === 'AbortError') return;

    const msg = event.reason?.message || 'Неизвестная ошибка сервера';
    toast(`Системная ошибка: ${msg}`, 'error');
});

window.addEventListener('error', (event) => {
    console.error('Global Error:', event.error);
    toast(`Ошибка приложения: ${event.message}`, 'error');
});

// --- 3. Инициализация приложения ---
document.addEventListener('DOMContentLoaded', async () => {
    console.log('Admin App Initialized (SPA Lazy Mode)');

    setupHeader();
    setupGlobalEvents();
    setupAdminProfile();

    // Проверяем, нужно ли принудительно сменить пароль
    await checkAdminSecurity();

    // Инициализируем 2FA логику (глобальная модалка находится в admin.html)
    TotpSetup.init();

    // Запускаем роутинг в самом конце, чтобы интерфейс загрузился
    setupRouting();
});

function setupHeader() {
    // Выводим имя текущего админа в шапку
    const adminNameEl = document.getElementById('adminName');
    const username = Auth.getUsername();

    if (adminNameEl && username) {
        adminNameEl.textContent = `${username}`;
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

    // Делегирование событий для переключения табов в шапке
    const tabsContainer = document.querySelector('.tabs');
    if (tabsContainer) {
        tabsContainer.addEventListener('click', (e) => {
            const btn = e.target.closest('.tab-btn');
            if (!btn) return;

            const tabId = btn.dataset.tab;
            if (tabId) {
                // Изменение хэша автоматически вызовет handleRoute()
                window.location.hash = tabId;
            }
        });
    }

    // --- Глобальные Горячие Клавиши для Админки ---
    document.addEventListener('keydown', (e) => {
        // 1. Закрытие модалок по Escape
        if (e.key === 'Escape') {
            const openModals = document.querySelectorAll('.modal-overlay.open');
            openModals.forEach(modal => {
                // Модалку обязательной настройки закрывать нельзя
                if (modal.id !== 'firstSetupModal') {
                    modal.classList.remove('open');
                }
            });
        }

        // 2. Быстрый поиск по "/"
        if (e.key === '/' && document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'TEXTAREA') {
            e.preventDefault(); // Предотвращаем ввод слеша в поле

            // Ищем видимый инпут поиска на текущей активной вкладке
            const activeTab = document.querySelector('.tab-content.active');
            if (activeTab) {
                const searchInput = activeTab.querySelector('input[type="text"][id$="SearchInput"]');
                if (searchInput) {
                    searchInput.focus();
                }
            }
        }
    });
}

// --- ЛОГИКА ПРОФИЛЯ АДМИНИСТРАТОРА И БЕЗОПАСНОСТИ ---

async function checkAdminSecurity() {
    try {
        const user = await api.get('/users/me');
        // Если это первый вход, показываем принудительное окно
        if (user.is_initial_setup_done === false) {
            const fsModal = document.getElementById('firstSetupModal');
            if (fsModal) {
                fsModal.classList.add('open');
            }
        }
    } catch (e) {
        console.warn('Security check failed', e);
    }
}

function setupAdminProfile() {
    // 1. Форма первичной настройки (Обязательная)
    const fsForm = document.getElementById('firstSetupForm');
    if (fsForm) {
        fsForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const btn = document.getElementById('btnFsSave');
            const newLogin = document.getElementById('fsNewLogin')?.value.trim();
            const newPass = document.getElementById('fsNewPassword').value;

            setLoading(btn, true, 'Сохранение...');
            try {
                const payload = { new_password: newPass };
                if (newLogin) payload.new_username = newLogin;

                await api.post('/users/me/setup', payload);

                alert('Данные безопасности успешно обновлены. Пожалуйста, войдите в систему с новыми данными.');
                Auth.logout(); // Выкидываем на логин для проверки
            } catch (err) {
                toast(err.message, 'error');
                setLoading(btn, false, 'Сохранить и войти');
            }
        });
    }

    // 2. Открытие профиля по кнопке из шапки
    const profileBtn = document.getElementById('btnOpenAdminProfile');
    const profileModal = document.getElementById('adminProfileModal');
    if (profileBtn && profileModal) {
        profileBtn.addEventListener('click', () => {
            profileModal.classList.add('open');
        });
    }

    // 3. Форма смены пароля/логина внутри профиля
    const changeCredsForm = document.getElementById('adminChangeCredsForm');
    if (changeCredsForm) {
        changeCredsForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const btn = document.getElementById('btnSaveAdminCreds');

            const newLogin = document.getElementById('adminNewLogin').value.trim();
            const oldPass = document.getElementById('adminOldPass').value;
            const newPass = document.getElementById('adminNewPass').value;

            setLoading(btn, true, 'Сохранение...');
            try {
                const user = await api.get('/users/me');

                // Если введен новый пароль — меняем пароль
                if (newPass) {
                    await api.post('/users/me/change-password', {
                        old_password: oldPass,
                        new_password: newPass
                    });
                }

                // Если введен новый логин — меняем логин
                if (newLogin && newLogin !== user.username) {
                    await api.put(`/users/${user.id}`, {
                        username: newLogin
                    });
                    // Обновляем данные в сессии и шапке
                    Auth.setSession(user.role, newLogin);
                    const adminNameEl = document.getElementById('adminName');
                    if(adminNameEl) adminNameEl.textContent = newLogin;
                }

                toast('Профиль успешно обновлен!', 'success');

                // Очищаем форму
                document.getElementById('adminOldPass').value = '';
                document.getElementById('adminNewPass').value = '';
                document.getElementById('adminNewLogin').value = '';

                profileModal.classList.remove('open');

            } catch (err) {
                toast(err.message, 'error');
            } finally {
                setLoading(btn, false, 'Сохранить изменения');
            }
        });
    }
}

// --- РОУТИНГ (SPA: ПЕРЕКЛЮЧЕНИЕ И ДИНАМИЧЕСКАЯ ЗАГРУЗКА ВКЛАДОК) ---

function setupRouting() {
    window.addEventListener('hashchange', handleRoute);
    handleRoute(); // Вызываем первый раз при загрузке страницы
}

function handleRoute() {
    const hash = window.location.hash.substring(1);

    // ИСПРАВЛЕНИЕ: дефолтная вкладка — dashboard (ранее readings).
    // Дашборд — первое что видит администратор при входе: KPI, метрики, журнал.
    const defaultTab = 'dashboard';

    // ИСПРАВЛЕНИЕ: dashboard добавлен в список валидных вкладок.
    const validTabs = ['dashboard', 'readings', 'housing', 'users', 'tariffs', 'accountant', 'debts', 'manual'];
    const tabToLoad = validTabs.includes(hash) ? hash : defaultTab;

    switchTab(tabToLoad);
}

async function switchTab(tabId) {
    if (!tabId) return;

    // 1. Деактивируем все кнопки вкладок
    document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
    const btn = document.querySelector(`.tab-btn[data-tab="${tabId}"]`);
    if (btn) btn.classList.add('active');

    const contentArea = document.getElementById('content-area');
    if (!contentArea) return;

    // Скрываем все вкладки
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));

    // 2. Проверяем, загружена ли уже эта вкладка в DOM
    let content = document.getElementById(tabId);

    if (!content) {
        // Если вкладки нет в DOM, показываем красивый лоадер вместо нее
        const loaderId = `loader-${tabId}`;
        contentArea.insertAdjacentHTML('beforeend', `
            <div id="${loaderId}" class="tab-content active" style="display:flex; flex-direction:column; justify-content:center; align-items:center; height: 400px; color: var(--primary-color);">
                <i class="fa-solid fa-spinner fa-spin" style="font-size: 3rem; margin-bottom: 16px;"></i>
                <p style="color: var(--text-secondary); font-weight: 500;">Загрузка модуля...</p>
            </div>
        `);

        try {
            const response = await fetch(`components/admin/tab_${tabId}.html`);
            if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);

            const htmlString = await response.text();

            // Удаляем лоадер
            const loaderEl = document.getElementById(loaderId);
            if (loaderEl) loaderEl.remove();

            // Вставляем полученный HTML
            contentArea.insertAdjacentHTML('beforeend', htmlString);
            content = document.getElementById(tabId);

            // Делаем вкладку видимой
            if (content) content.classList.add('active');

            // Загружаем JS-модуль
            await initModule(tabId);

        } catch (error) {
            console.error('Ошибка загрузки компонента:', error);
            toast('Ошибка загрузки интерфейса. Проверьте соединение.', 'error');
            const loaderEl = document.getElementById(loaderId);
            if (loaderEl) loaderEl.remove();
            return;
        }
    } else {
        // Если HTML вкладки уже в DOM (например вкладка readings встроена изначально)
        content.classList.add('active');

        // Загружаем JS-модуль (если он еще не был скачан)
        await initModule(tabId);

        // Если модуль уже был инициализирован ранее, пнем его обновить таблицу
        refreshModuleData(tabId);
    }
}

// АСИНХРОННАЯ Инициализация модуля (Динамический импорт / Lazy Loading)
async function initModule(tabId) {
    try {
        switch (tabId) {
            // НОВОЕ: Модуль дашборда с KPI и журналом действий
            case 'dashboard':
                if (!loadedModules.dashboard) {
                    const { DashboardModule } = await import('./modules/dashboard.js');
                    loadedModules.dashboard = DashboardModule;
                }
                loadedModules.dashboard.init();
                break;
            case 'readings':
                if (!loadedModules.readings) {
                    const { ReadingsModule } = await import('./modules/readings.js');
                    loadedModules.readings = ReadingsModule;
                }
                loadedModules.readings.init();
                break;
            case 'housing':
                if (!loadedModules.housing) {
                    const { HousingModule } = await import('./modules/housing.js');
                    loadedModules.housing = HousingModule;
                }
                loadedModules.housing.init();
                break;
            case 'users':
                if (!loadedModules.users) {
                    const { UsersModule } = await import('./modules/users.js');
                    loadedModules.users = UsersModule;
                }
                loadedModules.users.init();
                break;
            case 'tariffs':
                if (!loadedModules.tariffs) {
                    const { TariffsModule } = await import('./modules/tariffs.js');
                    loadedModules.tariffs = TariffsModule;
                }
                loadedModules.tariffs.init();
                break;
            case 'accountant':
                if (!loadedModules.accountant) {
                    const { SummaryModule } = await import('./modules/summary.js');
                    loadedModules.accountant = SummaryModule;
                }
                loadedModules.accountant.init();
                break;
            case 'debts':
                if (!loadedModules.debts) {
                    const { DebtsModule } = await import('./modules/debts.js');
                    loadedModules.debts = DebtsModule;
                }
                loadedModules.debts.init();
                break;
            case 'manual':
                if (!loadedModules.manual) {
                    const { ManualModule } = await import('./modules/manual.js');
                    loadedModules.manual = ManualModule;
                }
                loadedModules.manual.init();
                break;
            default:
                console.warn(`Модуль для вкладки "${tabId}" не найден.`);
        }
    } catch (error) {
        console.error(`Ошибка динамической загрузки модуля ${tabId}:`, error);
        toast('Ошибка загрузки скриптов компонента', 'error');
    }
}

// Обновление данных при возврате на вкладку
function refreshModuleData(tabId) {
    const mod = loadedModules[tabId];
    if (!mod || !mod.isInitialized) return;

    switch (tabId) {
        // НОВОЕ: При возврате на дашборд — обновляем KPI (лёгкий запрос)
        case 'dashboard':
            if (typeof mod.loadKPI === 'function') mod.loadKPI();
            break;
        case 'readings':
        case 'housing':
        case 'users':
            if (mod.table) mod.table.refresh();
            break;
        case 'tariffs':
            mod.load();
            break;
        case 'accountant':
            // Сводку не обновляем автоматически, так как это тяжелый запрос
            break;
        case 'debts':
            if (typeof mod.reload === 'function') mod.reload();
            break;
        case 'manual':
            if (typeof mod.searchUsers === 'function') mod.searchUsers('');
            break;
    }
}