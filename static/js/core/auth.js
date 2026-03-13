// static/js/core/auth.js

export const Auth = {
    /**
     * Сохраняет базовые данные сессии в sessionStorage.
     * Сам токен (access_token) хранится браузером в HttpOnly Cookie для безопасности!
     */
    setSession(role, username) {
        if (role) sessionStorage.setItem('role', role);
        if (username) sessionStorage.setItem('username', username);
    },

    /**
     * Проверяем авторизацию (косвенно, по наличию роли в хранилище).
     * Истинная проверка происходит на бэкенде при каждом API запросе.
     */
    isAuthenticated() {
        return !!sessionStorage.getItem('role');
    },

    getRole() {
        return sessionStorage.getItem('role');
    },

    getUsername() {
        return sessionStorage.getItem('username');
    },

    /**
     * Полный выход из системы.
     * Удаляет куку на сервере и чистит локальные хранилища.
     */
    async logout() {
        console.log('Выполняется выход из системы...');
        try {
            // ИСПРАВЛЕНИЕ: Убрано /auth/ из URL
            await fetch('/api/logout', {
                method: 'POST',
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