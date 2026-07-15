// static/js/app.js
import { api } from './core/api.js';
import { Auth } from './core/auth.js';
import { toast, setLoading, showAlert } from './core/dom.js';
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
// E3-B: дополнительно шлём ошибку на бэк в /api/errors/frontend, чтобы
// она оказалась в админской «Копилке ошибок» рядом с серверными
// 500/celery-failures. Простой беспроверочный POST без await — UX не
// должен зависеть от логирования. Дедуп по message за последние 5с —
// чтобы цикл «while(true){throw}» в багнутом компоненте не залил БД.
const _frontendErrorRecent = new Map();  // message → timestamp
function _reportFrontendError(payload) {
    try {
        const key = (payload.message || '') + '|' + (payload.source || '');
        const now = Date.now();
        const last = _frontendErrorRecent.get(key);
        if (last && (now - last) < 5000) return;
        _frontendErrorRecent.set(key, now);
        // Очистим если карта разрослась.
        if (_frontendErrorRecent.size > 100) {
            const cutoff = now - 5000;
            for (const [k, t] of _frontendErrorRecent) {
                if (t < cutoff) _frontendErrorRecent.delete(k);
            }
        }
        // Fire-and-forget; не используем api.js обёртку, чтоб не упасть
        // на её же ошибках (никаких retry, никаких toast).
        fetch('/api/errors/frontend', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
            keepalive: true,  // дойдёт даже если страница закрывается
        }).catch(() => {});
    } catch { /* безопасно игнорируем */ }
}

window.addEventListener('unhandledrejection', (event) => {
    console.error('Unhandled Rejection:', event.reason);

    // Игнорируем ошибки отмены запросов (AbortController)
    if (event.reason && event.reason.name === 'AbortError') return;

    const reason = event.reason || {};
    const msg = reason.message || String(reason) || 'Неизвестная ошибка сервера';
    toast(`Системная ошибка: ${msg}`, 'error');
    _reportFrontendError({
        message: String(msg).slice(0, 5000),
        stack: (reason.stack || '').slice(0, 20000) || null,
        url: window.location.href.slice(0, 500),
        user_agent: (navigator.userAgent || '').slice(0, 500),
    });
});

window.addEventListener('error', (event) => {
    console.error('Global Error:', event.error);
    toast(`Ошибка приложения: ${event.message}`, 'error');
    _reportFrontendError({
        message: String(event.message || '').slice(0, 5000),
        source: (event.filename || '').slice(0, 500) || null,
        lineno: event.lineno || null,
        colno: event.colno || null,
        stack: (event.error?.stack || '').slice(0, 20000) || null,
        url: window.location.href.slice(0, 500),
        user_agent: (navigator.userAgent || '').slice(0, 500),
    });
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

    // Глобальный баннер здоровья системы (диск/релей ГИС/1С/beat) — виден на
    // любой вкладке. Обновление раз в 5 минут (сторож пишет каждые 10).
    loadSystemHealth();
    setInterval(loadSystemHealth, 5 * 60 * 1000);

    // Запускаем роутинг в самом конце, чтобы интерфейс загрузился
    setupRouting();
});

async function loadSystemHealth() {
    const box = document.getElementById('systemHealthBanner');
    if (!box) return;
    const escT = (s) => String(s ?? '').replace(/[&<>"']/g,
        c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    try {
        const d = await api.get('/admin/system-health');
        const alerts = d.alerts || [];
        if (!alerts.length) { box.style.display = 'none'; box.innerHTML = ''; return; }
        // ✕ скрывает ЭТОТ набор алертов (fingerprint в localStorage) — при
        // изменении состава/текста баннер вернётся сам (жалоба «не убрать»).
        const fp = alerts.map(a => a.code + '|' + a.message).join(';');
        if (localStorage.getItem('healthBannerDismissed') === fp) {
            box.style.display = 'none'; box.innerHTML = ''; return;
        }
        const hasCrit = alerts.some(a => a.level === 'crit');
        const bd = hasCrit ? '#ef4444' : '#f59e0b';
        const bg = hasCrit ? 'rgba(239,68,68,0.10)' : 'rgba(245,158,11,0.10)';
        const rows = alerts.map(a =>
            `<div style="margin:2px 0;">${a.level === 'crit' ? '🛑' : '⚠️'} ${escT(a.message)}</div>`
        ).join('');
        box.innerHTML =
            `<div style="position:relative; border:1px solid ${bd}; background:${bg}; border-radius:10px; padding:10px 38px 10px 14px; font-size:13px;">` +
            `<button id="healthBannerClose" title="Скрыть (вернётся при новых проблемах)" ` +
            `style="position:absolute; top:6px; right:8px; border:none; background:none; cursor:pointer; font-size:15px; color:inherit; opacity:.6;">✕</button>` +
            `<div style="font-weight:700; margin-bottom:4px;">${hasCrit ? '🛑' : '⚠️'} Система требует внимания</div>${rows}</div>`;
        box.style.display = 'block';
        document.getElementById('healthBannerClose')?.addEventListener('click', () => {
            localStorage.setItem('healthBannerDismissed', fp);
            box.style.display = 'none';
        });
    } catch (e) {
        box.style.display = 'none'; // вспомогательный элемент — тихо
    }
}

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

    // Делегирование событий для переключения табов в шапке.
    // Поддерживаем оба контейнера: legacy .tabs (старая шапка, скрыт через
    // CSS, но в DOM) и новый .header-tabs (compact header v2). После
    // полного удаления legacy-блока selector можно будет упростить.
    document.querySelectorAll('.tabs, .header-tabs').forEach((tabsContainer) => {
        tabsContainer.addEventListener('click', (e) => {
            const btn = e.target.closest('.tab-btn');
            if (!btn) return;

            const tabId = btn.dataset.tab;
            if (tabId) {
                // Изменение хэша автоматически вызовет handleRoute()
                window.location.hash = tabId;
            }
        });
    });

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

                await showAlert('Данные безопасности успешно обновлены. Пожалуйста, войдите в систему с новыми данными.', { title: 'Данные обновлены' });
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

    // 2b. Закрытие модалки профиля по крестику.
    // Раньше в admin.html было onclick="document.getElementById(...).classList.remove('open')"
    // — мешало убрать 'unsafe-inline' из CSP. Теперь — через data-атрибут.
    if (profileModal) {
        profileModal.querySelectorAll('[data-close-admin-profile]').forEach(btn => {
            btn.addEventListener('click', () => profileModal.classList.remove('open'));
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

                // Если введён новый ЛОГИН — меняем ТОЛЬКО логин входа.
                // РАНЬШЕ слали PUT /users/{id} {username:newLogin} → это
                // переписывало ФИО (username = ключ сверки 1С/ГИС!) значением
                // логина → ломало сверку. Теперь через /me/change-login:
                // меняется только login, ФИО/сверка не трогаются. Шапку
                // (adminName = ФИО) НЕ обновляем — сменился лишь логин.
                if (newLogin && newLogin !== user.login) {
                    await api.post('/users/me/change-login', {
                        new_login: newLogin,
                        old_password: oldPass
                    });
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

    // Дашборд ВЫРЕЗАН (2026-07-14) — главный экран админа = «Реестр показаний».
    const defaultTab = 'readings';

    // ВАЖНО: при добавлении новой вкладки — обязательно добавить её сюда,
    // иначе clickByHash сделает fallback на readings.
    // 'audit'/'errors' валидны, но БЕЗ кнопок в навбаре — открываются из
    // Операции → Система (2026-07-14). 'certs' удалён (Справки вырезаны).
    const validTabs = ['readings', 'tools', 'housing', 'users', 'passport', 'debts', 'safety', 'audit', 'tickets', 'errors'];
    let tabToLoad = validTabs.includes(hash) ? hash : defaultTab;
    // Старые хеши — обратная совместимость закладок/дип-линков.
    if (hash === 'security') tabToLoad = 'errors';
    if (hash === 'dashboard') tabToLoad = 'readings';
    if (hash === 'manual' || hash === 'tariffs' || hash === 'accountant') tabToLoad = 'tools';

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
            // Дашборд — объединённый экран: KPI + журнал действий + реестр показаний.
            // Инициализируем модули вкладки: ReadingsModule (детальная таблица), GSheets, Registry, ExcelReadings.
            // «Реестр показаний» вынесен из дашборда в отдельную вкладку
            // (2026-06-09): все показания MeterReading из всех источников.
            case 'readings':
                if (!loadedModules.readings) {
                    const { ReadingsModule } = await import('./modules/readings.js');
                    loadedModules.readings = ReadingsModule;
                }
                loadedModules.readings.init();
                // Объединение реестров (2026-06-09): буфер Google Sheets теперь
                // в этом же табе — инициализируем и его модуль (его DOM в
                // tab_readings.html). gsheets.js самодостаточен.
                if (!loadedModules.gsheets) {
                    const { GSheetsModule } = await import('./modules/gsheets.js');
                    loadedModules.gsheets = GSheetsModule;
                }
                loadedModules.gsheets.init();
                // Фаза 2: единый список (боевые ∪ буфер) — отдельный модуль.
                if (!loadedModules.registry) {
                    const { RegistryModule } = await import('./modules/registry.js');
                    loadedModules.registry = RegistryModule;
                }
                loadedModules.registry.init();
                // Импорт показаний из Excel (2026-06-15): модалка с разбором
                // и прямым утверждением в финотчётность. Самодостаточный модуль.
                if (!loadedModules.excelReadings) {
                    const { ExcelReadingsModule } = await import('./modules/excel_readings.js');
                    loadedModules.excelReadings = ExcelReadingsModule;
                }
                loadedModules.excelReadings.init();
                break;
            case 'housing':
                if (!loadedModules.housing) {
                    const { HousingModule } = await import('./modules/housing.js');
                    loadedModules.housing = HousingModule;
                }
                loadedModules.housing.init();
                break;
            case 'safety':
                if (!loadedModules.safety) {
                    const { SafetyModule } = await import('./modules/safety.js');
                    loadedModules.safety = SafetyModule;
                }
                loadedModules.safety.init();
                break;
            case 'security':
                if (!loadedModules.security) {
                    const { SecurityModule } = await import('./modules/security.js');
                    loadedModules.security = SecurityModule;
                }
                loadedModules.security.init();
                break;
            case 'users':
                if (!loadedModules.users) {
                    const { UsersModule } = await import('./modules/users.js');
                    loadedModules.users = UsersModule;
                }
                loadedModules.users.init();
                break;
            // Глобальный поиск по ФИО → карточка жильца 360° (всё из всех источников).
            case 'passport':
                if (!loadedModules.passport) {
                    const { PassportModule } = await import('./modules/passport.js');
                    loadedModules.passport = PassportModule;
                }
                loadedModules.passport.init();
                break;
            // E3-C: «Копилка ошибок» — список бэк/celery/frontend ошибок
            // с auto-investigation и кнопкой «Скопировать в Claude».
            case 'errors':
                if (!loadedModules.errors) {
                    const { ErrorsModule } = await import('./modules/errors.js');
                    loadedModules.errors = ErrorsModule;
                }
                loadedModules.errors.init();
                // Вкладка объединена с «Безопасность» — инициализируем и сводку
                // сканеров (её DOM теперь в tab_errors.html).
                if (!loadedModules.security) {
                    const { SecurityModule } = await import('./modules/security.js');
                    loadedModules.security = SecurityModule;
                }
                loadedModules.security.init();
                break;
            case 'debts':
                if (!loadedModules.debts) {
                    const { DebtsModule } = await import('./modules/debts.js');
                    loadedModules.debts = DebtsModule;
                }
                loadedModules.debts.init();
                break;
            // Операции — accordion из 7 секций. Раньше все 7 модулей
            // инициализировались при открытии вкладки → 7 параллельных
            // волн API-запросов даже когда оператору нужна одна секция.
            // Теперь — eager init ТОЛЬКО для уже открытых секций (по умолчанию
            // первая, manual), остальные — лениво по событию tools:section-opened.
            case 'tools': {
                // Tools-контроллер всегда нужен — он обрабатывает аккордеон.
                if (!loadedModules.tools) {
                    const { ToolsModule } = await import('./modules/tools.js');
                    loadedModules.tools = ToolsModule;
                }
                loadedModules.tools.init();

                // Маппинг секции → импорт модуля. Динамические импорты
                // выполняются только при первом раскрытии секции.
                const sectionLoaders = {
                    manual: async () => {
                        if (!loadedModules.manual) {
                            const { ManualModule } = await import('./modules/manual.js');
                            loadedModules.manual = ManualModule;
                        }
                        loadedModules.manual.init();
                    },
                    schedule: async () => {
                        // График подачи живёт внутри модуля Tariffs (общая секция настроек).
                        if (!loadedModules.tariffs) {
                            const { TariffsModule } = await import('./modules/tariffs.js');
                            loadedModules.tariffs = TariffsModule;
                        }
                        loadedModules.tariffs.init();
                    },
                    seasonal: async () => {
                        // Сезонные переключатели (отопление / подогрев ГВС) — управляются
                        // тем же модулем TariffsModule. Полная инициализация дешёвая
                        // (всего GET /settings/seasonal), но idempotent через isInitialized.
                        if (!loadedModules.tariffs) {
                            const { TariffsModule } = await import('./modules/tariffs.js');
                            loadedModules.tariffs = TariffsModule;
                        }
                        loadedModules.tariffs.init();
                    },
                    tariffs: async () => {
                        if (!loadedModules.tariffs) {
                            const { TariffsModule } = await import('./modules/tariffs.js');
                            loadedModules.tariffs = TariffsModule;
                        }
                        loadedModules.tariffs.init();
                    },
                    summary: async () => {
                        if (!loadedModules.summary) {
                            const { SummaryModule } = await import('./modules/summary.js');
                            loadedModules.summary = SummaryModule;
                        }
                        loadedModules.summary.init();
                    },
                    gsheets: async () => {
                        if (!loadedModules.gsheets) {
                            const { GSheetsModule } = await import('./modules/gsheets.js');
                            loadedModules.gsheets = GSheetsModule;
                        }
                        loadedModules.gsheets.init();
                    },
                    analyzer: async () => {
                        if (!loadedModules.analyzer) {
                            const { AnalyzerModule } = await import('./modules/analyzer.js');
                            loadedModules.analyzer = AnalyzerModule;
                        }
                        loadedModules.analyzer.init();
                    },
                    recalc: async () => {
                        if (!loadedModules.recalc) {
                            const { RecalcModule } = await import('./modules/recalc.js');
                            loadedModules.recalc = RecalcModule;
                        }
                        loadedModules.recalc.init();
                    },
                    'operator-info': async () => {
                        // Юр. реквизиты оператора (152-ФЗ) — секция в Операциях.
                        // Lazy-загружается при первом раскрытии секции.
                        if (!loadedModules.operatorInfo) {
                            const { OperatorInfoModule } = await import('./modules/operator-info.js');
                            loadedModules.operatorInfo = OperatorInfoModule;
                        }
                        loadedModules.operatorInfo.init();
                    },
                };

                const initialized = new Set();
                const initSection = async (name) => {
                    if (!name || initialized.has(name)) return;
                    const loader = sectionLoaders[name];
                    if (!loader) return;
                    initialized.add(name);
                    try { await loader(); }
                    catch (e) { console.error(`[tools] section "${name}" init failed:`, e); }
                };

                // 1) Eager: уже открытые секции (.open) — обычно одна (manual).
                document
                    .querySelectorAll('#toolsAccordion .accordion-section.open')
                    .forEach(s => initSection(s.dataset.section));

                // 2) Lazy: подписка на раскрытие секций (idempotent — тег на элементе).
                const root = document.getElementById('toolsAccordion');
                if (root && !root.dataset.lazyInitBound) {
                    root.dataset.lazyInitBound = '1';
                    root.addEventListener('tools:section-opened', (e) => {
                        initSection(e.detail?.section);
                    });
                }
                break;
            }
            // Журнал действий админов (multi-admin прозрачность).
            case 'audit':
                if (!loadedModules.audit) {
                    const { AuditModule } = await import('./modules/audit.js');
                    loadedModules.audit = AuditModule;
                }
                loadedModules.audit.init();
                break;
            // Обращения жильцов — вопросы в техподдержку.
            case 'tickets':
                if (!loadedModules.tickets) {
                    const { TicketsModule } = await import('./modules/tickets.js');
                    loadedModules.tickets = TicketsModule;
                }
                loadedModules.tickets.init();
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
        // При возврате на дашборд — обновляем KPI и виджет GSheets.
        // «Реестр показаний» — отдельная вкладка (вынесен из дашборда).
        // Объединена с буфером Google Sheets — обновляем и его.
        case 'readings':
            if (mod.table) mod.table.refresh();
            if (loadedModules.gsheets && typeof loadedModules.gsheets.refresh === 'function') {
                loadedModules.gsheets.refresh();
            }
            if (loadedModules.registry && typeof loadedModules.registry.refresh === 'function') {
                loadedModules.registry.refresh();
            }
            break;
        case 'housing':
        case 'users':
            if (mod.table) mod.table.refresh();
            break;
        case 'safety':
            if (typeof mod.refresh === 'function') mod.refresh();
            break;
        case 'security':
            if (typeof mod.refresh === 'function') mod.refresh();
            break;
        case 'debts':
            if (typeof mod.reload === 'function') mod.reload();
            break;
        case 'audit':
            if (typeof mod.refresh === 'function') mod.refresh();
            break;
        case 'tickets':
            if (typeof mod.refresh === 'function') mod.refresh();
            break;
        // Операции: перезагружаем тарифы (lightweight), gsheets, очищаем поиск жильца
        case 'tools': {
            const tariffsMod = loadedModules.tariffs;
            if (tariffsMod && typeof tariffsMod.load === 'function') tariffsMod.load();
            const manualMod = loadedModules.manual;
            if (manualMod && typeof manualMod.searchUsers === 'function') {
                manualMod.searchUsers('');
            }
            const gsheetsMod = loadedModules.gsheets;
            if (gsheetsMod && typeof gsheetsMod.refresh === 'function') {
                gsheetsMod.refresh();
            }
            break;
        }
    }
}