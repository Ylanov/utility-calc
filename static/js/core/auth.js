// static/js/core/auth.js

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
        window.location.replace('portal.html');
    }
};