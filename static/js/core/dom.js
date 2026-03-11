// static/js/core/dom.js

/**
 * Создает HTML элемент безопасным способом (защита от XSS).
 */
export function el(tag, attributes = {}, ...children) {
    const element = document.createElement(tag);

    for (const [key, value] of Object.entries(attributes)) {
        if (key.startsWith('on') && typeof value === 'function') {
            element.addEventListener(key.substring(2).toLowerCase(), value);
        } else if (key === 'style' && typeof value === 'object') {
            Object.assign(element.style, value);
        } else if (key === 'dataset' && typeof value === 'object') {
            Object.assign(element.dataset, value);
        } else if (value !== null && value !== undefined && value !== false) {
            element.setAttribute(key, value);
        }
    }

    children.forEach(child => {
        if (child === null || child === undefined || child === false) return;

        if (typeof child === 'string' || typeof child === 'number') {
            // Создаем текстовый узел — предотвращает выполнение <script> (XSS)
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
 * Предотвращает множественные отправки форм (Double-click prevention).
 */
export function setLoading(btn, isLoading, loadingText = 'Загрузка...') {
    if (!btn) return;

    if (isLoading) {
        // Сохраняем оригинальные ширину и текст, чтобы кнопка не прыгала
        if (!btn.dataset.originalText) {
            btn.dataset.originalText = btn.innerText;
            // Фиксируем ширину (опционально, убрано для гибкости, но можно добавить btn.style.width = btn.offsetWidth + 'px')
        }

        btn.disabled = true;
        btn.classList.add('loading-state'); // Класс для CSS (потускнение)
        btn.innerText = loadingText;
    } else {
        btn.disabled = false;
        btn.classList.remove('loading-state');
        btn.innerText = btn.dataset.originalText || 'OK';

        // Удаляем сохраненный текст для будущих вызовов
        delete btn.dataset.originalText;
    }
}

/**
 * Показывает красивое всплывающее уведомление (Toast).
 */
export function toast(message, type = 'success') {
    let container = document.getElementById('toast-container');
    if (!container) {
        container = el('div', { id: 'toast-container' });
        document.body.appendChild(container);
    }

    const colors = {
        success: '#10b981', // Изумрудный (Tailwind green-500)
        error: '#ef4444',   // Красный (Tailwind red-500)
        info: '#3b82f6',    // Синий (Tailwind blue-500)
        warning: '#f59e0b'  // Желтый (Tailwind amber-500)
    };

    // Определяем иконку
    const icons = {
        success: '✅',
        error: '❌',
        info: 'ℹ️',
        warning: '⚠️'
    };

    const icon = icons[type] || icons.success;

    const toastEl = el('div', {
        class: 'toast show',
        style: {
            backgroundColor: colors[type] || colors.success,
            display: 'flex',
            alignItems: 'center',
            gap: '10px'
        }
    },
        el('span', { style: { fontSize: '18px' } }, icon),
        el('span', {}, message)
    );

    container.appendChild(toastEl);

    // Анимация удаления
    setTimeout(() => {
        toastEl.style.opacity = '0';
        toastEl.style.transform = 'translateX(100%)';
        setTimeout(() => toastEl.remove(), 300);
    }, 4000); // Держим 4 секунды (чуть дольше для длинных текстов)
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
            style: { width: '100%', marginBottom: '20px', fontSize: '16px' }
        });

        const btnConfirm = el('button', { class: 'action-btn primary-btn' }, 'OK');
        const btnCancel = el('button', { class: 'action-btn secondary-btn' }, 'Отмена');

        const modal = el('div', { class: 'modal-window', style: { width: '400px' } },
            el('div', { class: 'modal-header' }, el('h3', {}, title)),
            el('div', { class: 'modal-form' },
                el('p', { style: { marginBottom: '15px', color: '#4b5563', fontSize: '14px' } }, message),
                input
            ),
            el('div', { class: 'modal-footer' }, btnCancel, btnConfirm)
        );

        overlay.appendChild(modal);
        document.body.appendChild(overlay);

        // Фокус на поле ввода с небольшой задержкой для рендера
        setTimeout(() => input.focus(), 50);

        const close = (value) => {
            if (document.body.contains(overlay)) {
                document.body.removeChild(overlay);
            }
            resolve(value);
        };

        btnConfirm.onclick = () => close(input.value.trim());
        btnCancel.onclick = () => close(null);

        // Обработка клавиш
        input.onkeydown = (e) => {
            if (e.key === 'Enter') {
                e.preventDefault(); // Защита от сабмита форм на фоне
                close(input.value.trim());
            }
            if (e.key === 'Escape') {
                e.preventDefault();
                close(null);
            }
        };

        // Закрытие по клику вне модалки
        overlay.onmousedown = (e) => {
            if (e.target === overlay) close(null);
        };
    });
}