// static/js/core/auth.js

export const Auth = {
    /**
     * Сохраняет данные сессии.
     * Токен больше не сохраняем, он автоматически хранится браузером в HttpOnly куке!
     */
    setSession(role, username) {
        if (role) sessionStorage.setItem('role', role);
        if (username) sessionStorage.setItem('username', username);
    },

    /**
     * Проверяем авторизацию по наличию роли в хранилище.
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
     */
    async logout() {
        console.log('Logging out...');
        try {
            // Если эндпоинт в корне
            await fetch('/api/logout', { method: 'POST' }); // <---- Исправлено здесь
        } catch (e) {
            console.warn('Ошибка при выходе:', e);
        }

        sessionStorage.clear();
        localStorage.removeItem('token'); // Зачищаем старые артефакты

        // Редирект на вход
        window.location.replace('login.html');
    }
};