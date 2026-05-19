/**
 * Авторизация / профиль текущего жильца.
 * Кэшируем `me` в памяти на время сессии, чтобы не дёргать /api/me на
 * каждой смене экрана.
 */
import { api, getToken, setToken } from './api.js';

let _meCache = null;

/**
 * Проверяет наличие токена и грузит профиль. Если токена нет — редирект
 * на старый login.html (его пока переиспользуем в Фазе 1).
 *
 * Заодно «оживляет» токен для PWA-сценария «добавить на главный экран»:
 * sessionStorage не переживает закрытие приложения, а localStorage —
 * переживает. После логина на старом портале токен попадает только
 * в sessionStorage — копируем его в localStorage чтобы при следующем
 * запуске PWA (с домашнего экрана) сессия осталась.
 */
export async function ensureAuthenticated() {
    const token = getToken();
    if (!token) {
        window.location.href = '/login.html?next=/app/';
        // Возвращаем promise который никогда не резолвится — чтобы код не пошёл дальше.
        return new Promise(() => {});
    }
    // Синхронизируем хранилища — пишем токен и в session, и в local,
    // чтобы любая последующая навигация (старый портал / PWA) видела его.
    setToken(token);

    if (_meCache) return _meCache;
    try {
        _meCache = await api.get('/me');
        return _meCache;
    } catch (e) {
        // 401 обработается в api.js (редирект). Любая другая ошибка — кидаем.
        if (e.status !== 401) throw e;
        return new Promise(() => {});
    }
}

export function getCachedMe() {
    return _meCache;
}

/**
 * Инициалы для аватарки. «Иванов Иван Иванович» → «ИИ».
 * Для нестандартных логинов (Л/С, например «209450») — первые 2 цифры.
 */
export function initialsFor(name) {
    if (!name) return '?';
    const parts = name.trim().split(/\s+/);
    if (parts.length >= 2) {
        return (parts[0][0] + parts[1][0]).toUpperCase();
    }
    return parts[0].slice(0, 2).toUpperCase();
}
