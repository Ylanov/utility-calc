// static/js/modules/tools.js
//
// Контроллер accordion-секций на вкладке "Операции".
// Вся логика ручного ввода и тарифов живёт в ManualModule/TariffsModule;
// этот модуль отвечает только за раскрытие/сворачивание секций
// и синхронизацию с URL (?section=manual).

// Группы верхних табов. Должны совпадать с data-ops-tab атрибутами секций
// в tab_tools.html. При добавлении новой секции — допишите её ops-tab сюда
// и в HTML, чтобы UI знал куда её положить.
const OPS_TABS = ['readings', 'tariffs', 'finance', 'analytics', 'system'];
const OPS_TAB_LS_KEY = 'ops:tab';
const OPS_TAB_DEFAULT = 'readings';

export const ToolsModule = {
    isInitialized: false,
    currentOpsTab: OPS_TAB_DEFAULT,

    init() {
        const root = document.getElementById('toolsAccordion');
        if (!root) return;

        if (!this.isInitialized) {
            this._bindEvents(root);
            this._bindOpsTabs();
            // Глобальный мост: другие модули (dashboard и т.п.) могут
            // открыть конкретную секцию через CustomEvent — это переключит
            // нужный ops-tab автоматически.
            window.addEventListener('tools:open-section', (e) => {
                const name = e?.detail?.section;
                if (name) this.openByName(name);
            });
            this.isInitialized = true;
        }

        // Восстанавливаем активный ops-tab из localStorage.
        const savedTab = this._readSavedTab();
        this.setOpsTab(savedTab, { persist: false });

        // При повторном входе на вкладку — если в hash/search есть section=xxx,
        // откроем соответствующую секцию (и переключим её ops-tab).
        this._applyInitialState(root);
    },

    _readSavedTab() {
        try {
            const v = localStorage.getItem(OPS_TAB_LS_KEY);
            return OPS_TABS.includes(v) ? v : OPS_TAB_DEFAULT;
        } catch { return OPS_TAB_DEFAULT; }
    },

    _bindOpsTabs() {
        const tabsNav = document.getElementById('opsTabs');
        if (!tabsNav) return;
        tabsNav.addEventListener('click', (e) => {
            const btn = e.target.closest('.ops-tab');
            if (!btn) return;
            const name = btn.dataset.opsTab;
            if (!name) return;
            this.setOpsTab(name);
        });
    },

    // Переключает активный верхний таб «Операций» и скрывает аккордеоны
    // других групп. По умолчанию запоминает выбор в localStorage.
    setOpsTab(name, { persist = true } = {}) {
        if (!OPS_TABS.includes(name)) name = OPS_TAB_DEFAULT;
        this.currentOpsTab = name;

        // Подсветка кнопки
        document.querySelectorAll('.ops-tab').forEach(btn => {
            const active = btn.dataset.opsTab === name;
            btn.classList.toggle('active', active);
            btn.setAttribute('aria-selected', active ? 'true' : 'false');
        });

        // Показ/скрытие секций. Используем класс (не inline display) чтобы
        // не затирать собственный style="display:none" у scheduled — он
        // отображается только если есть запланированные тарифы.
        document.querySelectorAll('.accordion-section[data-ops-tab]').forEach(s => {
            s.classList.toggle('is-hidden-by-tab', s.dataset.opsTab !== name);
        });

        if (persist) {
            try { localStorage.setItem(OPS_TAB_LS_KEY, name); } catch {}
        }
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
    // Если секция лежит в другом ops-tab — сначала переключает таб,
    // иначе раскрытие произойдёт «вслепую» (секция скрыта).
    openByName(name) {
        const section = document.querySelector(
            `.accordion-section[data-section="${CSS.escape(name)}"]`
        );
        if (!section) return;
        const opsTab = section.dataset.opsTab;
        if (opsTab && opsTab !== this.currentOpsTab) {
            this.setOpsTab(opsTab);
        }
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
            // Если секция в другом ops-tab — переключаем туда.
            const targetSection = root.querySelector(
                `.accordion-section[data-section="${CSS.escape(target)}"]`
            );
            const opsTab = targetSection?.dataset.opsTab;
            if (opsTab && opsTab !== this.currentOpsTab) {
                this.setOpsTab(opsTab);
            }
            // Закрываем всё кроме выбранной (только в пределах текущего ops-tab).
            root.querySelectorAll('.accordion-section').forEach(s => {
                if (s.dataset.section === target) {
                    this.open(s);
                } else {
                    this.close(s);
                }
            });
            setTimeout(() => {
                if (targetSection) targetSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }, 120);
        }
        // Иначе остаётся дефолтное состояние из разметки (первая секция open).
    },
};
