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
            // Вызываем эндпоинт логаута на бэкенде, чтобы он удалил HttpOnly Cookie
            await fetch('/api/auth/logout', {
                method: 'POST',
                credentials: 'include' // Обязательно, чтобы отправить текущую куку на удаление
            });
        } catch (e) {
            console.warn('Сервер не ответил при выходе, продолжаем локальную очистку:', e);
        }

        // Зачищаем все локальные данные
        sessionStorage.clear();
        localStorage.clear(); // Чистим всё на случай мусора от старых версий

        // Редирект на портал (или login.html)
        // Используем replace, чтобы нельзя было вернуться кнопкой "Назад"
        window.location.replace('portal.html');
    }
};