/**
 * Toast уведомления — лёгкий API без зависимостей.
 * Использование: toast.show('Сохранено', 'success')
 */

const HOST_ID = 'toast-host';
const DEFAULT_DURATION = 3000;

function ensureHost() {
    let host = document.getElementById(HOST_ID);
    if (!host) {
        host = document.createElement('div');
        host.id = HOST_ID;
        host.className = 'toast-host';
        host.setAttribute('role', 'status');
        host.setAttribute('aria-live', 'polite');
        document.body.appendChild(host);
    }
    return host;
}

function show(message, type = 'info', duration = DEFAULT_DURATION) {
    const host = ensureHost();
    const el = document.createElement('div');
    el.className = `toast toast--${type}`;
    el.textContent = message;
    host.appendChild(el);
    // Авто-удаление по таймеру + после анимации
    setTimeout(() => {
        el.style.transition = 'opacity 200ms ease';
        el.style.opacity = '0';
        setTimeout(() => el.remove(), 220);
    }, duration);
}

export const toast = {
    show,
    success: (m, d) => show(m, 'success', d),
    error: (m, d) => show(m, 'error', d ?? 4500),
    info: (m, d) => show(m, 'info', d),
};
