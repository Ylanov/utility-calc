// static/js/modules/client-history.js
import { api } from '../core/api.js';
import { el, toast } from '../core/dom.js';

export const ClientHistory = {
    init() {
        this.cacheDOM();
        this.loadHistory();
    },

    cacheDOM() {
        this.dom = {
            historyBody: document.getElementById('historyBody')
        };
    },

    async loadHistory() {
        if (!this.dom.historyBody) return;
        this.dom.historyBody.innerHTML = '';

        try {
            const history = await api.get('/readings/history');

            if (!history.length) {
                this.dom.historyBody.innerHTML = '<tr><td colspan="6" class="text-center" style="padding: 20px; color: #888;">История пуста</td></tr>';
                return;
            }

            const fragment = document.createDocumentFragment();

            history.forEach(r => {
                const tr = el('tr', {},
                    el('td', { style: { fontWeight: '600' } }, r.period),
                    el('td', { class: 'text-right' }, Number(r.hot).toFixed(3)),
                    el('td', { class: 'text-right' }, Number(r.cold).toFixed(3)),
                    el('td', { class: 'text-right' }, Number(r.electric).toFixed(3)),
                    el('td', { class: 'text-right', style: { fontWeight: 'bold', color: 'var(--success-color)' } }, `${Number(r.total).toFixed(2)} ₽`),
                    el('td', { class: 'text-center' },
                        el('button', {
                            class: 'action-btn secondary-btn',
                            style: { padding: '4px 10px', fontSize: '12px' },
                            title: 'Скачать PDF',
                            onclick: () => this.downloadReceipt(r.id)
                        }, 'Квитанция')
                    )
                );
                fragment.appendChild(tr);
            });

            this.dom.historyBody.appendChild(fragment);
        } catch (e) {
            console.warn('Ошибка загрузки истории:', e);
        }
    },

    async downloadReceipt(id) {
        toast('Генерация квитанции...', 'info');
        try {
            const res = await api.get(`/client/receipts/${id}`);
            if (res.url) {
                // Открываем PDF в новой вкладке (браузер сам скачает/покажет файл)
                window.open(res.url, '_blank');
            }
        } catch (e) {
            toast('Ошибка скачивания: ' + e.message, 'error');
        }
    }
};