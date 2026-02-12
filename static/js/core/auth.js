// static/js/core/auth.js

export const Auth = {
    /**
     * Сохраняет данные сессии в sessionStorage.
     * sessionStorage живет, пока открыта вкладка.
     * При закрытии вкладки данные удаляются (это безопаснее localStorage).
     */
    setSession(token, role, username) {
        if (token) sessionStorage.setItem('token', token);
        if (role) sessionStorage.setItem('role', role);
        if (username) sessionStorage.setItem('username', username);
    },

    /**
     * Возвращает текущий токен для заголовков API.
     */
    getToken() {
        return sessionStorage.getItem('token');
    },

    /**
     * Проверяет, авторизован ли пользователь.
     */
    isAuthenticated() {
        const token = this.getToken();
        // Можно добавить проверку на срок действия токена (JWT exp),
        // но базовая проверка - наличие строки.
        return !!token;
    },

    /**
     * Возвращает роль (для проверки прав доступа в UI).
     */
    getRole() {
        return sessionStorage.getItem('role');
    },

    /**
     * Возвращает логин текущего пользователя.
     */
    getUsername() {
        return sessionStorage.getItem('username');
    },

    /**
     * Полный выход из системы.
     */
    logout() {
        console.log('Logging out...');
        sessionStorage.clear();
        localStorage.removeItem('token'); // На всякий случай чистим и старое

        // Редирект на вход
        window.location.replace('login.html');
    }
};