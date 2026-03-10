// static/js/core/table-controller.js (ФИНАЛЬНАЯ ВЕРСИЯ)
import { api } from './api.js';
import { clear, el } from './dom.js';

export class TableController {
    constructor(config) {
        this.endpoint = config.endpoint;
        this.renderRow = config.renderRow;
        this.getExtraParams = config.getExtraParams || (() => ({}));

        this.dom = {
            tbody: document.getElementById(config.dom.tableBody),
            search: document.getElementById(config.dom.searchInput),
            limit: document.getElementById(config.dom.limitSelect),
            prev: document.getElementById(config.dom.prevBtn),
            next: document.getElementById(config.dom.nextBtn),
            info: document.getElementById(config.dom.pageInfo),
            loader: document.getElementById(config.dom.loadingEl),

            // НОВОЕ: Чекбокс "Выбрать все" в шапке таблицы (если есть)
            selectAll: document.getElementById(config.dom.selectAllCheckbox)
        };

        this.state = {
            page: 1,
            limit: this.dom.limit ? parseInt(this.dom.limit.value) : 50,
            total: 0,
            search: '',
            sortBy: 'id',
            sortDir: 'asc',
            isLoading: false
        };

        // НОВОЕ: Множество для хранения ID выбранных строк (сохраняется при смене страниц)
        this.selectedIds = new Set();

        this.abortController = null;
        this.debounceTimer = null;
    }

    init() {
        this.bindEvents();
        this.load();
    }

    bindEvents() {
        if (this.dom.search) {
            this.dom.search.addEventListener('input', (e) => {
                clearTimeout(this.debounceTimer);
                this.debounceTimer = setTimeout(() => {
                    this.state.search = e.target.value.trim();
                    this.state.page = 1;
                    this.load();
                }, 400);
            });
        }

        if (this.dom.limit) {
            this.dom.limit.addEventListener('change', (e) => {
                this.state.limit = parseInt(e.target.value);
                this.state.page = 1;
                this.load();
            });
        }

        if (this.dom.prev) this.dom.prev.addEventListener('click', () => this.changePage(-1));
        if (this.dom.next) this.dom.next.addEventListener('click', () => this.changePage(1));

        // Сортировка по заголовкам
        const table = this.dom.tbody.closest('table');
        if (table) {
            table.querySelector('thead')?.addEventListener('click', (e) => {
                const th = e.target.closest('th[data-sort]');
                if (!th) return;

                const field = th.dataset.sort;
                if (this.state.sortBy === field) {
                    this.state.sortDir = this.state.sortDir === 'asc' ? 'desc' : 'asc';
                } else {
                    this.state.sortBy = field;
                    this.state.sortDir = 'asc';
                }

                this.updateSortIcons(table);
                this.load();
            });
        }

        // НОВОЕ: Делегирование событий для чекбоксов строк
        this.dom.tbody.addEventListener('change', (e) => {
            if (e.target.classList.contains('row-checkbox')) {
                const id = e.target.value;
                if (e.target.checked) {
                    this.selectedIds.add(id);
                } else {
                    this.selectedIds.delete(id);
                }
                this.updateSelectAllCheckboxState();
            }
        });

        // НОВОЕ: Обработка главного чекбокса "Выбрать все" на текущей странице
        if (this.dom.selectAll) {
            this.dom.selectAll.addEventListener('change', (e) => {
                const isChecked = e.target.checked;
                const checkboxes = this.dom.tbody.querySelectorAll('.row-checkbox');
                checkboxes.forEach(cb => {
                    cb.checked = isChecked;
                    if (isChecked) {
                        this.selectedIds.add(cb.value);
                    } else {
                        this.selectedIds.delete(cb.value);
                    }
                });
            });
        }
    }

    changePage(delta) {
        const newPage = this.state.page + delta;
        if (newPage < 1) return;

        this.state.page = newPage;
        this.load();
    }

    async load() {
        if (this.abortController) {
            this.abortController.abort();
        }
        this.abortController = new AbortController();

        this.setLoading(true);

        try {
            const params = new URLSearchParams({
                page: this.state.page,
                limit: this.state.limit,
                search: this.state.search,
                sort_by: this.state.sortBy,
                sort_dir: this.state.sortDir
            });

            const extraParams = this.getExtraParams();
            for (const [key, value] of Object.entries(extraParams)) {
                params.append(key, value);
            }

            const data = await api.get(`${this.endpoint}?${params.toString()}`, {
                signal: this.abortController.signal
            });

            this.state.total = data.total;
            this.render(data.items);
            this.updatePagination();

        } catch (e) {
            if (e.name === 'AbortError') return;

            // БЕЗОПАСНЫЙ ВЫВОД ОШИБКИ
            this.dom.tbody.innerHTML = '';
            this.dom.tbody.appendChild(
                el('tr', {},
                    el('td', { colspan: '100', class: 'text-center text-red-600 py-4' }, `Ошибка: ${e.message}`)
                )
            );
        } finally {
            if (!this.abortController.signal.aborted) {
                this.setLoading(false);
            }
        }
    }

    render(items) {
        clear(this.dom.tbody.id);

        if (!items || items.length === 0) {
            this.dom.tbody.appendChild(
                el('tr', {}, el('td', { colspan: '100', class: 'text-center py-4 text-gray-500' }, 'Нет данных'))
            );
            this.updateSelectAllCheckboxState();
            return;
        }

        const fragment = document.createDocumentFragment();
        items.forEach(item => {
            const row = this.renderRow(item);

            // НОВОЕ: Восстанавливаем галочки чекбоксов при перелистывании страниц
            const checkbox = row.querySelector('.row-checkbox');
            if (checkbox && this.selectedIds.has(checkbox.value)) {
                checkbox.checked = true;
            }

            fragment.appendChild(row);
        });

        this.dom.tbody.appendChild(fragment);
        this.updateSelectAllCheckboxState();
    }

    updatePagination() {
        const totalPages = Math.ceil(this.state.total / this.state.limit) || 1;
        if (this.dom.info) {
            this.dom.info.textContent = `Стр. ${this.state.page} из ${totalPages} (Всего: ${this.state.total})`;
        }
        if (this.dom.prev) this.dom.prev.disabled = this.state.page <= 1;
        if (this.dom.next) this.dom.next.disabled = this.state.page >= totalPages;
    }

    updateSortIcons(table) {
        table.querySelectorAll('th[data-sort]').forEach(th => th.classList.remove('sort-asc', 'sort-desc'));
        const activeTh = table.querySelector(`th[data-sort="${this.state.sortBy}"]`);
        if (activeTh) activeTh.classList.add(`sort-${this.state.sortDir}`);
    }

    updateSelectAllCheckboxState() {
        if (!this.dom.selectAll) return;

        const checkboxes = this.dom.tbody.querySelectorAll('.row-checkbox');
        if (checkboxes.length === 0) {
            this.dom.selectAll.checked = false;
            this.dom.selectAll.indeterminate = false;
            return;
        }

        let checkedCount = 0;
        checkboxes.forEach(cb => { if (cb.checked) checkedCount++; });

        this.dom.selectAll.checked = checkedCount === checkboxes.length;
        this.dom.selectAll.indeterminate = checkedCount > 0 && checkedCount < checkboxes.length;
    }

    setLoading(isLoading) {
        this.state.isLoading = isLoading;
        this.dom.tbody.style.opacity = isLoading ? '0.5' : '1';
    }

    refresh() {
        this.load();
    }

    // НОВОЕ: Методы API для получения выбранных ID из других модулей (users.js, readings.js)
    getSelectedIds() {
        return Array.from(this.selectedIds);
    }

    clearSelection() {
        this.selectedIds.clear();
        this.updateSelectAllCheckboxState();
        const checkboxes = this.dom.tbody.querySelectorAll('.row-checkbox');
        checkboxes.forEach(cb => cb.checked = false);
    }
}