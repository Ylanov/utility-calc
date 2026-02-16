// static/js/core/table-controller.js (ФИНАЛЬНАЯ ВЕРСИЯ)
import { api } from './api.js';
import { clear, el } from './dom.js';

export class TableController {
    constructor(config) {
        this.endpoint = config.endpoint;
        this.renderRow = config.renderRow;
        // НОВОЕ: Функция для получения доп. параметров
        this.getExtraParams = config.getExtraParams || (() => ({}));

        this.dom = {
            tbody: document.getElementById(config.dom.tableBody),
            search: document.getElementById(config.dom.searchInput),
            limit: document.getElementById(config.dom.limitSelect),
            prev: document.getElementById(config.dom.prevBtn),
            next: document.getElementById(config.dom.nextBtn),
            info: document.getElementById(config.dom.pageInfo),
            loader: document.getElementById(config.dom.loadingEl)
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

            // НОВОЕ: Добавляем кастомные параметры
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
            this.dom.tbody.innerHTML = `<tr><td colspan="100" class="text-center text-red-600 py-4">Ошибка: ${e.message}</td></tr>`;
        } finally {
            if (!this.abortController.signal.aborted) {
                this.setLoading(false);
            }
        }
    }

    render(items) {
        clear(this.dom.tbody.id);

        if (!items || items.length === 0) {
            this.dom.tbody.innerHTML = '<tr><td colspan="100" class="text-center py-4 text-gray-500">Нет данных</td></tr>';
            return;
        }

        const fragment = document.createDocumentFragment();
        items.forEach(item => fragment.appendChild(this.renderRow(item)));
        this.dom.tbody.appendChild(fragment);
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

    setLoading(isLoading) {
        this.state.isLoading = isLoading;
        this.dom.tbody.style.opacity = isLoading ? '0.5' : '1';
    }

    refresh() {
        this.load();
    }
}