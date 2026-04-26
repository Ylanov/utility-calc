// static/js/modules/tools.js
//
// Контроллер accordion-секций на вкладке "Операции".
// Вся логика ручного ввода и тарифов живёт в ManualModule/TariffsModule;
// этот модуль отвечает только за раскрытие/сворачивание секций
// и синхронизацию с URL (?section=manual).

export const ToolsModule = {
    isInitialized: false,

    init() {
        const root = document.getElementById('toolsAccordion');
        if (!root) return;

        if (!this.isInitialized) {
            this._bindEvents(root);
            this.isInitialized = true;
        }

        // При повторном входе на вкладку — если в hash/search есть section=xxx,
        // откроем соответствующую секцию.
        this._applyInitialState(root);
    },

    _bindEvents(root) {
        // Делегируем клик с корня accordion — чтобы работало для всех секций.
        root.addEventListener('click', (e) => {
            const header = e.target.closest('.accordion-header');
            if (!header) return;
            const section = header.closest('.accordion-section');
            if (!section) return;

            this.toggle(section);
        });

        // Клавиатура: Enter/Space уже работают нативно на <button>,
        // но добавим стрелки для перехода между заголовками (лучше UX).
        root.addEventListener('keydown', (e) => {
            const header = e.target.closest('.accordion-header');
            if (!header) return;

            if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                e.preventDefault();
                const all = Array.from(root.querySelectorAll('.accordion-header'));
                const idx = all.indexOf(header);
                const nextIdx = e.key === 'ArrowDown'
                    ? (idx + 1) % all.length
                    : (idx - 1 + all.length) % all.length;
                all[nextIdx].focus();
            }
        });
    },

    toggle(section) {
        const isOpen = section.classList.contains('open');
        if (isOpen) {
            this.close(section);
        } else {
            this.open(section);
        }
    },

    open(section) {
        const wasOpen = section.classList.contains('open');
        section.classList.add('open');
        const header = section.querySelector('.accordion-header');
        if (header) header.setAttribute('aria-expanded', 'true');
        // Кастомное событие — позволяет app.js лениво инициализировать
        // модуль секции только при её первом раскрытии (а не при загрузке
        // вкладки «Операции»). Сильно экономит API-запросы для случая,
        // когда оператор пользуется только одной секцией.
        if (!wasOpen) {
            section.dispatchEvent(new CustomEvent('tools:section-opened', {
                bubbles: true,
                detail: { section: section.dataset.section },
            }));
        }
    },

    close(section) {
        section.classList.remove('open');
        const header = section.querySelector('.accordion-header');
        if (header) header.setAttribute('aria-expanded', 'false');
    },

    // Открывает секцию по её data-section и плавно прокручивает к ней.
    openByName(name) {
        const section = document.querySelector(
            `.accordion-section[data-section="${CSS.escape(name)}"]`
        );
        if (!section) return;
        this.open(section);
        // Небольшая задержка, чтобы анимация раскрытия успела начаться.
        setTimeout(() => {
            section.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }, 120);
    },

    _applyInitialState(root) {
        // Поддержка URL вида: #tools?section=tariffs (через query в hash)
        // или обычного query ?section=tariffs в адресной строке.
        const hash = window.location.hash || '';
        const queryFromHash = hash.includes('?') ? hash.split('?')[1] : '';
        const params = new URLSearchParams(
            queryFromHash || window.location.search
        );
        const target = params.get('section');
        if (target) {
            // Закрываем всё кроме выбранной
            root.querySelectorAll('.accordion-section').forEach(s => {
                if (s.dataset.section === target) {
                    this.open(s);
                } else {
                    this.close(s);
                }
            });
            setTimeout(() => {
                const s = root.querySelector(
                    `.accordion-section[data-section="${CSS.escape(target)}"]`
                );
                if (s) s.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }, 120);
        }
        // Иначе остаётся дефолтное состояние из разметки (первая секция open).
    },
};
