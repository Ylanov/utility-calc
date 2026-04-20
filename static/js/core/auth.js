// static/js/core/auth.js

// =============================================================================
// MULTI-TAB SESSION SYNC через BroadcastChannel.
//
// Раньше: пользователь делал logout в одной вкладке → sessionStorage чистился
// только в ней. В других открытых вкладках токен оставался, юзер мог продолжать
// работать — но при первом 401-ответе получал loading/redirect.
//
// Теперь: при logout в любой вкладке отправляем событие 'logout' через
// BroadcastChannel. Все остальные вкладки приёмника тоже чистят sessionStorage
// и редиректят на portal.html. Аналогично при login — рассылаем 'login',
// чтобы вкладки на login.html сразу подхватили сессию.
// =============================================================================
const _bc = (typeof BroadcastChannel !== 'undefined')
    ? new BroadcastChannel('jkh-auth-sync')
    : null;

if (_bc) {
    _bc.addEventListener('message', (event) => {
        if (!event?.data?.type) return;

        if (event.data.type === 'logout') {
            // Чистим состояние и идём на логин — но БЕЗ повторной отправки
            // в канал, иначе будет бесконечный цикл.
            sessionStorage.clear();
            localStorage.clear();
            if (!window.location.pathname.includes('portal.html') &&
                !window.location.pathname.includes('login.html')) {
                window.location.replace('portal.html');
            }
        }
    });
}

export const Auth = {
    /**
     * Сохраняет данные сессии в sessionStorage.
     * sessionStorage изолирован на каждую вкладку браузера,
     * поэтому разные пользователи в разных вкладках не пересекаются.
     */
    setSession(role, username, token) {
        if (role) sessionStorage.setItem('role', role);
        if (username) sessionStorage.setItem('username', username);
        if (token) sessionStorage.setItem('access_token', token);
    },

    /**
     * Возвращает токен текущей вкладки.
     */
    getToken() {
        return sessionStorage.getItem('access_token');
    },

    /**
     * Проверяем авторизацию по наличию роли и токена в хранилище.
     * Истинная проверка происходит на бэкенде при каждом API запросе.
     */
    isAuthenticated() {
        return !!sessionStorage.getItem('role') && !!sessionStorage.getItem('access_token');
    },

    getRole() {
        return sessionStorage.getItem('role');
    },

    getUsername() {
        return sessionStorage.getItem('username');
    },

    /**
     * Полный выход из системы.
     * Удаляет куку на сервере и чистит sessionStorage текущей вкладки.
     */
    async logout() {
        console.log('Выполняется выход из системы...');
        try {
            const token = sessionStorage.getItem('access_token');
            await fetch('/api/logout', {
                method: 'POST',
                headers: token ? { 'Authorization': `Bearer ${token}` } : {},
                credentials: 'include'
            });
        } catch (e) {
            console.warn('Сервер не ответил при выходе, продолжаем локальную очистку:', e);
        }

        sessionStorage.clear();
        localStorage.clear();

        // Уведомляем другие вкладки об logout — синхронная очистка во всех.
        if (_bc) {
            try { _bc.postMessage({ type: 'logout' }); } catch (e) { /* ignore */ }
        }

        window.location.replace('portal.html');
    }
};