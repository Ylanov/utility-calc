/**
 * МОДУЛЬ: CORE
 * API, Глобальное состояние (State) и UI-утилиты.
 */

const AppState = {
    nomenclatures: [],
    objects: [],
    // Читаем роль из sessionStorage (изолирован на вкладку)
    // вместо localStorage (общий для всех вкладок)
    userRole: sessionStorage.getItem('arsenal_role') || 'unit_head'
};

/**
 * Централизованная функция запросов к API.
 * Токен берётся из sessionStorage и передаётся
 * в заголовке Authorization: Bearer <token>.
 * Это позволяет разным пользователям работать в разных вкладках
 * без смешивания сессий.
 */
async function apiFetch(url, options = {}) {
    const token = sessionStorage.getItem('access_token');

    const defaultHeaders = { 'Content-Type': 'application/json' };

    // Добавляем токен в заголовок если он есть
    if (token) {
        defaultHeaders['Authorization'] = `Bearer ${token}`;
    }

    options.headers = { ...defaultHeaders, ...options.headers };
    options.credentials = 'same-origin';

    try {
        const response = await fetch(url, options);

        if (response.status === 401) {
            // Токен истёк или недействителен — очищаем сессию и редиректим на вход
            sessionStorage.removeItem('access_token');
            sessionStorage.removeItem('arsenal_role');
            sessionStorage.removeItem('arsenal_username');
            window.location.href = 'arsenal_login.html';
            return null;
        }

        return response;
    } catch (error) {
        console.error("Critical API Error:", error);
        UI.showToast("Сетевая ошибка. Проверьте подключение к серверу.", "error");
        return null;
    }
}

const UI = {
    showToast: (message, type = 'success') => {
        const container = document.getElementById('toast-container');
        if (!container) return;

        const toast = document.createElement('div');

        let bgClass = 'bg-slate-800';
        let icon = '<i class="fa-solid fa-circle-info text-blue-400"></i>';

        if (type === 'error') {
            bgClass = 'bg-rose-600';
            icon = '<i class="fa-solid fa-circle-exclamation text-white"></i>';
        } else if (type === 'success') {
            bgClass = 'bg-emerald-600';
            icon = '<i class="fa-solid fa-circle-check text-white"></i>';
        }

        toast.className = `${bgClass} text-white px-5 py-3 rounded-xl shadow-2xl flex items-center gap-3 toast-show pointer-events-auto max-w-sm border border-white/10`;

        const formattedMessage = message.replace(/\n/g, '<br>');

        toast.innerHTML = `
            <div class="text-lg">${icon}</div>
            <div class="text-sm font-medium leading-snug">${formattedMessage}</div>
            <button onclick="this.parentElement.remove()" class="ml-auto text-white/60 hover:text-white transition focus:outline-none">
                <i class="fa-solid fa-xmark"></i>
            </button>
        `;

        container.appendChild(toast);

        setTimeout(() => {
            if (toast.parentElement) {
                toast.classList.replace('toast-show', 'toast-hide');
                setTimeout(() => toast.remove(), 400);
            }
        }, 5000);
    },

    openModal: (id) => {
        const modal = document.getElementById(id);
        if (modal) modal.style.display = 'flex';
    },

    closeModal: (id) => {
        const modal = document.getElementById(id);
        if (modal) modal.style.display = 'none';
    },

    setLoading: (btnIdOrElement, isLoading, loadingText = 'Загрузка...') => {
        const btn = typeof btnIdOrElement === 'string' ? document.getElementById(btnIdOrElement) : btnIdOrElement;
        if (!btn) return;

        if (isLoading) {
            btn._originalText = btn.innerHTML;
            btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> ${loadingText}`;
            btn.disabled = true;
        } else {
            btn.innerHTML = btn._originalText || loadingText;
            btn.disabled = false;
        }
    },

    buildSelect: (selectEl, items, valueKey, labelKey, placeholder = 'Выберите...') => {
        if (!selectEl) return;
        selectEl.innerHTML = `<option value="">${placeholder}</option>`;
        items.forEach(item => {
            const option = document.createElement('option');
            option.value = item[valueKey];
            option.textContent = item[labelKey];
            selectEl.appendChild(option);
        });
    },

    showCredentialsModal: (title, username, password) => {
        const titleEl = document.getElementById('credModalTitle');
        const userEl = document.getElementById('credUsername');
        const passEl = document.getElementById('credPassword');
        const copyBtn = document.getElementById('btnCopyCreds');

        if (titleEl) titleEl.innerText = title;
        if (userEl) userEl.innerText = username;
        if (passEl) passEl.innerText = password;

        if (copyBtn) {
            copyBtn.innerHTML = '<i class="fa-regular fa-copy"></i> Копировать';
            copyBtn.className = "absolute top-4 right-4 bg-white border border-slate-300 text-slate-600 hover:bg-slate-50 hover:text-blue-600 px-3 py-1.5 rounded-lg text-xs font-bold shadow-sm transition flex items-center gap-2";

            copyBtn.onclick = () => {
                navigator.clipboard.writeText(`Логин: ${username}\nПароль: ${password}`).then(() => {
                    copyBtn.innerHTML = '<i class="fa-solid fa-check text-green-600"></i> Скопировано';
                    copyBtn.classList.add('border-green-300', 'bg-green-50');
                    setTimeout(() => {
                        copyBtn.innerHTML = '<i class="fa-regular fa-copy"></i> Копировать';
                        copyBtn.classList.remove('border-green-300', 'bg-green-50');
                    }, 2000);
                });
            };
        }
        UI.openModal('credentialsModal');
    }
};