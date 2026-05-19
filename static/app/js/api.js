/**
 * API клиент — fetch wrapper с JWT-авторизацией.
 *
 * Существующий портал использует `static/js/core/api.js` со store, но для PWA
 * хочется минимум зависимостей и собственный код в `/app/js/`, чтобы:
 *   - SW мог точно прекэшировать всё /app/ как app-shell
 *   - не тянуть монолитный API клиент админки
 */

// Старый портал использует `sessionStorage.access_token` (см. static/js/core/auth.js).
// Для совместимости читаем оттуда же. Но для PWA это плохо — sessionStorage
// не переживает «домашний экран → возврат». Поэтому при чтении подтягиваем
// и в localStorage (long-lived), и обратно — для соседних вкладок старого портала.
const TOKEN_KEY = 'access_token';

export function getToken() {
    try {
        return (
            sessionStorage.getItem(TOKEN_KEY) ||
            localStorage.getItem(TOKEN_KEY) ||
            null
        );
    } catch {
        return null;
    }
}

export function setToken(token) {
    if (!token) {
        try { sessionStorage.removeItem(TOKEN_KEY); } catch {}
        try { localStorage.removeItem(TOKEN_KEY); } catch {}
        return;
    }
    try { sessionStorage.setItem(TOKEN_KEY, token); } catch {}
    try { localStorage.setItem(TOKEN_KEY, token); } catch {}
}

/**
 * Базовый wrapper: добавляет Authorization, парсит JSON, кидает ошибки.
 * На 401 — единый redirect на login (а не показ модалки).
 */
async function request(path, { method = 'GET', body, headers = {} } = {}) {
    const token = getToken();
    const finalHeaders = { ...headers };
    if (token) finalHeaders['Authorization'] = `Bearer ${token}`;
    if (body && !(body instanceof FormData)) {
        finalHeaders['Content-Type'] = 'application/json';
    }
    const res = await fetch(`/api${path}`, {
        method,
        headers: finalHeaders,
        body: body instanceof FormData ? body : (body ? JSON.stringify(body) : undefined),
        credentials: 'same-origin',
    });

    // 401 → токен протух или невалиден. Чистим и редиректим на логин.
    // Старый портал использует /login.html — переиспользуем его пока (Фаза 1).
    // В Фазе 2 сделаем свой PWA-логин внутри /app/.
    if (res.status === 401) {
        setToken(null);
        window.location.href = '/login.html?next=/app/';
        // Чтобы код после await не выполнялся (промис никогда не резолвится).
        return new Promise(() => {});
    }

    const isJson = (res.headers.get('content-type') || '').includes('application/json');
    const data = isJson ? await res.json().catch(() => null) : await res.text();

    if (!res.ok) {
        const err = new Error(
            (data && (data.detail || data.message)) || `HTTP ${res.status}`
        );
        err.status = res.status;
        err.data = data;
        throw err;
    }
    return data;
}

export const api = {
    get: (path) => request(path, { method: 'GET' }),
    post: (path, body) => request(path, { method: 'POST', body }),
    put: (path, body) => request(path, { method: 'PUT', body }),
    delete: (path) => request(path, { method: 'DELETE' }),
};

/**
 * Удобный форматтер денег для UI — «12 345,67 ₽» (русская локаль).
 * Принимает Decimal-строки от сервера или числа.
 */
export function formatMoney(value, { withSign = false } = {}) {
    const num = Number(value || 0);
    const formatted = new Intl.NumberFormat('ru-RU', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
    }).format(Math.abs(num));
    if (!withSign) return formatted;
    if (num > 0) return '+' + formatted;
    if (num < 0) return '−' + formatted;
    return formatted;
}
