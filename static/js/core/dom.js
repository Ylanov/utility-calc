// static/js/core/dom.js

/**
 * Создает HTML элемент безопасным способом (защита от XSS).
 *
 * Использование:
 * el('div', { class: 'my-class' }, 'Текст', el('span', {}, 'Вложенный'))
 */
export function el(tag, attributes = {}, ...children) {
    const element = document.createElement(tag);

    for (const [key, value] of Object.entries(attributes)) {
        if (key.startsWith('on') && typeof value === 'function') {
            // Например: onclick
            element.addEventListener(key.substring(2).toLowerCase(), value);
        } else if (key === 'style' && typeof value === 'object') {
            // Например: style: { color: 'red' }
            Object.assign(element.style, value);
        } else if (key === 'dataset' && typeof value === 'object') {
            // Например: dataset: { id: 1 }
            Object.assign(element.dataset, value);
        } else if (value !== null && value !== undefined && value !== false) {
            // Обычные атрибуты
            element.setAttribute(key, value);
        }
    }

    children.forEach(child => {
        if (child === null || child === undefined || child === false) return;

        if (typeof child === 'string' || typeof child === 'number') {
            // Создаем текстовый узел — это предотвращает внедрение HTML-тегов (XSS)
            element.appendChild(document.createTextNode(child));
        } else if (child instanceof Node) {
            element.appendChild(child);
        } else if (Array.isArray(child)) {
            child.forEach(c => c && element.appendChild(c));
        }
    });

    return element;
}

/**
 * Очищает содержимое элемента по ID.
 */
export function clear(elementId) {
    const element = document.getElementById(elementId);
    if (element) element.innerHTML = '';
    return element;
}

/**
 * Управляет состоянием кнопки (Загрузка/Обычное).
 */
export function setLoading(btn, isLoading, loadingText = 'Загрузка...') {
    if (!btn) return;

    if (isLoading) {
        btn.dataset.originalText = btn.innerText;
        btn.disabled = true;
        // Можно добавить иконку спиннера, если есть CSS
        btn.innerText = loadingText;
    } else {
        btn.disabled = false;
        btn.innerText = btn.dataset.originalText || 'OK';
    }
}

/**
 * Показывает красивое всплывающее уведомление.
 * @param {string} message - Текст
 * @param {string} type - 'success' | 'error' | 'info'
 */
export function toast(message, type = 'success') {
    let container = document.getElementById('toast-container');
    if (!container) {
        container = el('div', { id: 'toast-container' });
        document.body.appendChild(container);
    }

    const colors = {
        success: '#27ae60',
        error: '#c0392b',
        info: '#2980b9'
    };

    const toastEl = el('div', {
        class: 'toast show',
        style: { backgroundColor: colors[type] || colors.success }
    }, message);

    container.appendChild(toastEl);

    // Удаляем через 3.5 секунды
    setTimeout(() => {
        toastEl.classList.remove('show');
        setTimeout(() => toastEl.remove(), 300);
    }, 3500);
}

/**
 * Асинхронное модальное окно с вводом данных (Замена window.prompt).
 * Возвращает Promise.
 */
export function showPrompt(title, message, defaultValue = '', placeholder = '') {
    return new Promise((resolve) => {
        const overlay = el('div', { class: 'modal-overlay open', style: { zIndex: 10000 } });
        const input = el('input', {
            type: 'text',
            value: defaultValue,
            placeholder: placeholder,
            class: 'modal-input' // Стилизуйте этот класс в CSS
        });

        const btnConfirm = el('button', { class: 'confirm-btn' }, 'OK');
        const btnCancel = el('button', { class: 'close-btn' }, 'Отмена');

        const modal = el('div', { class: 'modal-window', style: { width: '400px' } },
            el('div', { class: 'modal-header' }, title),
            el('p', { style: { marginBottom: '10px', color: '#555' } }, message),
            input,
            el('div', { class: 'modal-actions' }, btnCancel, btnConfirm)
        );

        overlay.appendChild(modal);
        document.body.appendChild(overlay);
        input.focus();

        const close = (value) => {
            document.body.removeChild(overlay);
            resolve(value);
        };

        btnConfirm.onclick = () => close(input.value.trim());
        btnCancel.onclick = () => close(null);

        input.onkeyup = (e) => {
            if (e.key === 'Enter') close(input.value.trim());
            if (e.key === 'Escape') close(null);
        };
    });
}