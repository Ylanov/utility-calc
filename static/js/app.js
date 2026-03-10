// static/js/app.js
import { api } from './core/api.js';
import { Auth } from './core/auth.js';
import { toast, setLoading } from './core/dom.js';
import { TotpSetup } from './core/totp.js';

// Подключаем все модули бизнес-логики
import { ReadingsModule } from './modules/readings.js';
import { UsersModule } from './modules/users.js';
import { TariffsModule } from './modules/tariffs.js';
import { SummaryModule } from './modules/summary.js';
import { DebtsModule } from './modules/debts.js';     // Модуль долгов
import { ManualModule } from './modules/manual.js';   // Модуль ручного ввода показаний

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
    console.log('Admin App Initialized (SPA Mode)');

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

    // --- НОВОЕ: Глобальные Горячие Клавиши для Админки ---
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
                // Ищем инпуты, id которых заканчивается на SearchInput (как usersSearchInput, debtsSearchInput и т.д.)
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

                // Если введен новый логин — меняем логин через обычный PUT (права админа позволяют)
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
    // Слушаем изменение хэша в URL (например, #users, #tariffs)
    window.addEventListener('hashchange', handleRoute);
    handleRoute(); // Вызываем первый раз при загрузке страницы
}

function handleRoute() {
    const hash = window.location.hash.substring(1);
    const defaultTab = 'readings';

    // Массив разрешенных вкладок. ДОБАВЛЕН 'manual'
    const validTabs = ['readings', 'users', 'tariffs', 'accountant', 'debts', 'manual'];
    const tabToLoad = validTabs.includes(hash) ? hash : defaultTab;

    switchTab(tabToLoad);
}

async function switchTab(tabId) {
    if (!tabId) return;

    // 1. Деактивируем все кнопки вкладок в шапке
    document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));

    // Активируем нужную кнопку
    const btn = document.querySelector(`.tab-btn[data-tab="${tabId}"]`);
    if (btn) btn.classList.add('active');

    // Главный контейнер, куда мы будем вставлять загруженный HTML
    const contentArea = document.getElementById('content-area');
    if (!contentArea) {
        console.error("Критическая ошибка: не найден #content-area");
        return;
    }

    // 2. Проверяем, загружена ли уже эта вкладка в DOM
    let content = document.getElementById(tabId);

    if (!content) {
        // Если вкладки еще нет в DOM, загружаем её HTML фрагмент по сети
        try {
            // Показываем индикатор загрузки (опционально)
            contentArea.style.opacity = '0.5';

            const response = await fetch(`components/admin/tab_${tabId}.html`);
            if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);

            const htmlString = await response.text();

            // Вставляем полученный HTML в конец content-area
            contentArea.insertAdjacentHTML('beforeend', htmlString);

            // Находим свежезагруженный элемент
            content = document.getElementById(tabId);

            // Инициализируем JS-модуль ТОЛЬКО ПОСЛЕ того, как HTML появился в DOM
            initModule(tabId);

        } catch (error) {
            console.error('Ошибка загрузки компонента:', error);
            toast('Ошибка загрузки интерфейса. Проверьте соединение.', 'error');
            contentArea.style.opacity = '1';
            return;
        } finally {
            contentArea.style.opacity = '1';
        }
    }

    // 3. Скрываем все вкладки и показываем текущую
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
    if (content) {
        content.classList.add('active');

        // Вызываем обновление данных при каждом переходе на вкладку (опционально)
        // Если модуль уже был инициализирован, мы можем просто пнуть его обновить таблицу
        refreshModuleData(tabId);
    }
}

// Первичная инициализация модуля (навешивание событий)
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
        case 'debts':
            DebtsModule.init();
            break;
        case 'manual':
            ManualModule.init(); // Инициализация ручного ввода
            break;
        default:
            console.warn(`Модуль для вкладки "${tabId}" не найден.`);
    }
}

// Обновление данных (если пользователь ушел на другую вкладку и вернулся)
function refreshModuleData(tabId) {
    switch (tabId) {
        case 'readings':
            if (ReadingsModule.isInitialized && ReadingsModule.table) {
                ReadingsModule.table.refresh();
            }
            break;
        case 'users':
            if (UsersModule.isInitialized && UsersModule.table) {
                UsersModule.table.refresh();
            }
            break;
        case 'tariffs':
            if (TariffsModule.isInitialized) {
                TariffsModule.load();
            }
            break;
        case 'accountant':
            // Сводку не обновляем автоматически, так как это тяжелый запрос,
            // бухгалтер нажмет кнопку "Обновить" сам, если нужно.
            break;
        case 'debts':
            if (DebtsModule.isInitialized) {
                DebtsModule.reload();
            }
            break;
        case 'manual':
            if (ManualModule.isInitialized) {
                // При возврате на вкладку обновляем список пользователей на всякий случай
                ManualModule.searchUsers('');
            }
            break;
    }
}