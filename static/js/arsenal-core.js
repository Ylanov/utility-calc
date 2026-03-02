/**
 * МОДУЛЬ: CORE
 * API, Глобальное состояние (State) и UI-утилиты.
 */

const AppState = {
    nomenclatures: [],
    objects: [],
    userRole: localStorage.getItem('arsenal_role') || 'unit_head'
};

// Централизованная функция запросов к API
async function apiFetch(url, options = {}) {
    const defaultHeaders = { 'Content-Type': 'application/json' };
    options.headers = { ...defaultHeaders, ...options.headers };
    options.credentials = 'same-origin';

    try {
        const response = await fetch(url, options);
        if (response.status === 401) {
            // Автоматический редирект при потере сессии
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
    // 🔥 НОВОЕ: Всплывающие уведомления (Toasts) вместо alert()
    showToast: (message, type = 'success') => {
        const container = document.getElementById('toast-container');
        if (!container) return;

        const toast = document.createElement('div');

        // Стили в зависимости от типа (success / error / info)
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

        // Преобразуем \n в <br> для корректного переноса строк (нужно для импорта из Excel)
        const formattedMessage = message.replace(/\n/g, '<br>');

        toast.innerHTML = `
            <div class="text-lg">${icon}</div>
            <div class="text-sm font-medium leading-snug">${formattedMessage}</div>
            <button onclick="this.parentElement.remove()" class="ml-auto text-white/60 hover:text-white transition focus:outline-none">
                <i class="fa-solid fa-xmark"></i>
            </button>
        `;

        container.appendChild(toast);

        // Автоматически скрываем через 5 секунд
        setTimeout(() => {
            if (toast.parentElement) {
                toast.classList.replace('toast-show', 'toast-hide');
                setTimeout(() => toast.remove(), 300); // Ждем окончания анимации скрытия
            }
        }, 5000);
    },

    // Управление модалками
    openModal: (id) => {
        const modal = document.getElementById(id);
        if (modal) {
            modal.style.display = 'flex';
            // Фокус на первое поле ввода, если есть
            const firstInput = modal.querySelector('input:not([disabled]), select');
            if (firstInput) firstInput.focus();
        }
    },

    closeModal: (id) => {
        const modal = document.getElementById(id);
        if (modal) modal.style.display = 'none';
    },

    // Хелпер для отображения загрузки в таблицах
    setLoading: (elementId, message = 'Загрузка данных...', colspan = 1) => {
        const el = document.getElementById(elementId);
        if (el) {
            el.innerHTML = `<tr><td colspan="${colspan}" class="text-center p-8 text-slate-500">
                <i class="fa-solid fa-spinner fa-spin text-blue-600 mb-2 text-xl"></i><br>${message}
            </td></tr>`;
        }
    },

    // Динамическое добавление поля "Источник" (если его нет в HTML)
    injectSourceSelectIfNeeded: () => {
        if (document.getElementById('newDocSource')) return;
        const targetContainer = document.getElementById('targetSelectContainer');
        if (!targetContainer) return;

        const sourceContainer = document.createElement('div');
        sourceContainer.id = 'sourceSelectContainer';
        sourceContainer.innerHTML = `
            <label class="block text-xs font-bold text-slate-500 uppercase mb-1.5">Отправитель / Источник</label>
            <select id="newDocSource" class="w-full border border-slate-300 p-2.5 rounded-lg bg-white focus:border-blue-500 outline-none">
                <option value="">Загрузка...</option>
            </select>
        `;
        targetContainer.parentElement.insertBefore(sourceContainer, targetContainer);
    },

    // Модалка с логином/паролем после создания объекта
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