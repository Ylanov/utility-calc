/**
 * МОДУЛЬ: DOCUMENTS LIST & VIEW
 * Чтение списка, просмотр карточки документа, скачивание файлов из S3, удаление.
 * ОПТИМИЗАЦИЯ: Добавлена клиентская пагинация.
 */

window.Documents = window.Documents || {};

Object.assign(window.Documents, {
    // Временное хранилище остатков склада-отправителя для автозаполнения
    currentSourceBalance: [],

    // Состояние пагинации
    state: {
        skip: 0,
        limit: 50, // Размер страницы
        hasMore: true
    },

    // Инициализация (вызывается при старте)
    init: () => {
        Documents.loadList();
    },

    // Загрузка журнала документов с учетом пагинации
    loadList: async () => {
        const tableBody = document.getElementById('docsTableBody');
        const footer = document.getElementById('docsCount'); // Используем подвал для кнопок

        // Очищаем таблицу только если это первая загрузка или ручное обновление,
        // чтобы избежать "мигания" при переключении страниц можно оставить скелетон,
        // но здесь используем спиннер.
        UI.setLoading('docsTableBody', 'Загрузка журнала операций...', 7);

        try {
            // Формируем URL с параметрами пагинации
            const url = `/api/arsenal/documents?skip=${Documents.state.skip}&limit=${Documents.state.limit}`;

            const response = await apiFetch(url);
            if (!response || !response.ok) throw new Error('API Error');

            const docs = await response.json();

            // Определяем, есть ли еще страницы
            Documents.state.hasMore = docs.length === Documents.state.limit;

            if (docs.length === 0 && Documents.state.skip === 0) {
                tableBody.innerHTML = '<tr><td colspan="7" class="text-center p-8 text-slate-400">Журнал пуст. Нажмите "Создать документ".</td></tr>';
                Documents.renderPagination(footer, 0);
                return;
            }

            const rowsHtml = docs.map(doc => {
                let icon = '<i class="fa-solid fa-file text-slate-300"></i>';
                if (doc.type === 'Первичный ввод') icon = '<i class="fa-solid fa-file-import text-emerald-500"></i>';
                else if (['Отправка', 'Перемещение', 'Прием'].includes(doc.type)) icon = '<i class="fa-solid fa-truck-arrow-right text-blue-500"></i>';
                else if (doc.type === 'Списание') icon = '<i class="fa-solid fa-ban text-rose-500"></i>';

                return `
                    <tr class="doc-row cursor-pointer hover:bg-slate-50 transition border-b border-slate-100 last:border-0 group" data-id="${doc.id}">
                        <td class="text-center text-lg py-3">${icon}</td>
                        <td class="p-4 text-slate-600 font-mono text-xs">${doc.date}</td>
                        <td class="p-4 font-bold text-blue-800 text-sm font-mono group-hover:text-blue-600 transition">${doc.doc_number}</td>
                        <td class="p-4">${Documents.getTypeBadge(doc.type)}</td>
                        <td class="p-4 text-slate-600 text-sm truncate max-w-[150px]" title="${doc.source || ''}">${doc.source || '<span class="text-slate-300">-</span>'}</td>
                        <td class="p-4 text-slate-600 text-sm truncate max-w-[150px]" title="${doc.target || ''}">${doc.target || '<span class="text-slate-300">-</span>'}</td>
                        <td class="p-4 text-center">
                            <button class="delete-btn text-slate-300 hover:text-rose-600 p-2 rounded-lg hover:bg-rose-50 transition" data-id="${doc.id}" title="Удалить">
                                <i class="fa-solid fa-trash"></i>
                            </button>
                        </td>
                    </tr>`;
            }).join('');

            tableBody.innerHTML = rowsHtml;

            // Обновляем контролы пагинации
            Documents.renderPagination(footer, docs.length);

            // Вешаем слушатели кликов
            document.querySelectorAll('.doc-row').forEach(row => {
                row.addEventListener('click', (e) => {
                    if (!e.target.closest('.delete-btn')) Documents.openViewModal(row.dataset.id);
                });
            });

            document.querySelectorAll('.delete-btn').forEach(btn => {
                btn.addEventListener('click', function(e) {
                    e.stopPropagation();
                    Documents.delete(this.dataset.id);
                });
            });

        } catch (error) {
            console.error(error);
            tableBody.innerHTML = '<tr><td colspan="7" class="text-center text-rose-500 p-4 font-bold">Ошибка загрузки журнала.</td></tr>';
        }
    },

    // Рендеринг кнопок пагинации в подвале
    renderPagination: (container, currentCount) => {
        if (!container) return;

        const start = Documents.state.skip + 1;
        const end = Documents.state.skip + currentCount;

        // Очищаем старый текст
        container.innerHTML = '';
        container.className = "mt-3 text-xs text-slate-500 flex justify-between items-center shrink-0 font-medium px-1 w-full";

        // Левая часть: статистика
        const statsDiv = document.createElement('div');
        if (currentCount > 0) {
            statsDiv.innerText = `Показано: ${start}-${end}`;
        } else {
            statsDiv.innerText = 'Нет данных';
        }

        // Правая часть: кнопки
        const controlsDiv = document.createElement('div');
        controlsDiv.className = "flex gap-2";

        const prevBtn = document.createElement('button');
        prevBtn.innerHTML = '<i class="fa-solid fa-chevron-left"></i> Назад';
        prevBtn.className = `px-3 py-1 rounded border transition ${Documents.state.skip === 0 ? 'bg-slate-100 text-slate-300 cursor-not-allowed' : 'bg-white border-slate-300 text-slate-600 hover:bg-slate-50'}`;
        prevBtn.disabled = Documents.state.skip === 0;
        prevBtn.onclick = () => Documents.changePage(-1);

        const nextBtn = document.createElement('button');
        nextBtn.innerHTML = 'Вперед <i class="fa-solid fa-chevron-right"></i>';
        nextBtn.className = `px-3 py-1 rounded border transition ${!Documents.state.hasMore ? 'bg-slate-100 text-slate-300 cursor-not-allowed' : 'bg-white border-slate-300 text-slate-600 hover:bg-slate-50'}`;
        nextBtn.disabled = !Documents.state.hasMore;
        nextBtn.onclick = () => Documents.changePage(1);

        controlsDiv.appendChild(prevBtn);
        controlsDiv.appendChild(nextBtn);

        container.appendChild(statsDiv);
        container.appendChild(controlsDiv);
    },

    // Переключение страницы
    changePage: (direction) => {
        if (direction === 1 && Documents.state.hasMore) {
            Documents.state.skip += Documents.state.limit;
            Documents.loadList();
        } else if (direction === -1 && Documents.state.skip > 0) {
            Documents.state.skip = Math.max(0, Documents.state.skip - Documents.state.limit);
            Documents.loadList();
        }
    },

    // Удаление документа
    delete: async (id) => {
        if (!confirm('Вы уверены? Это действие необратимо удалит документ и откатит движение ТМЦ.')) return;
        try {
            const res = await apiFetch(`/api/arsenal/documents/${id}`, { method: 'DELETE' });
            if (res && res.ok) {
                UI.showToast("Документ успешно удален.", "success");
                Documents.loadList(); // Перезагружаем текущую страницу
                Dashboard.loadKPIs();
            } else {
                UI.showToast('Ошибка удаления.', "error");
            }
        } catch (e) {
             UI.showToast('Ошибка сети при удалении.', "error");
        }
    },

    // Просмотр карточки документа (с поддержкой скачивания файла из S3)
    openViewModal: async (id) => {
        const tBody = document.getElementById('viewDocItems');
        UI.setLoading('viewDocItems', 'Загрузка содержимого...', 4);
        UI.openModal('viewDocModal');

        try {
            const res = await apiFetch(`/api/arsenal/documents/${id}`);
            const doc = await res.json();

            document.getElementById('viewDocNumber').innerText = doc.doc_number;
            document.getElementById('viewDocDate').innerText = new Date(doc.operation_date).toLocaleDateString();
            document.getElementById('viewDocType').innerText = doc.operation_type;

            document.getElementById('viewDocSource').innerText = doc.source ? doc.source.name : '---';
            document.getElementById('viewDocTarget').innerText = doc.target ? doc.target.name : '---';

            // 🔥 ОТОБРАЖАЕМ ССЫЛКУ НА ФАЙЛ S3
            const fileContainer = document.getElementById('viewDocFileContainer');
            if (fileContainer) {
                if (doc.file_url) {
                    fileContainer.innerHTML = `
                        <a href="${doc.file_url}" target="_blank" class="text-xs bg-blue-50 text-blue-700 hover:bg-blue-100 border border-blue-200 px-3 py-1.5 rounded-lg font-bold transition flex items-center gap-2">
                            <i class="fa-solid fa-paperclip"></i> Скачать документ
                        </a>
                    `;
                } else {
                    fileContainer.innerHTML = '<span class="text-[10px] text-slate-400 uppercase font-bold"><i class="fa-solid fa-file-excel opacity-50"></i> Нет скана</span>';
                }
            }

            if (doc.items.length === 0) {
                tBody.innerHTML = '<tr><td colspan="4" class="text-center p-4">Нет позиций.</td></tr>';
                return;
            }

            const itemsHtml = doc.items.map(i => {
                const priceFmt = (i.price || 0).toLocaleString('ru-RU', { style: 'currency', currency: 'RUB' });
                return `
                    <tr class="border-b border-slate-100 last:border-0">
                        <td class="p-3">
                            <div class="font-bold text-slate-800 text-sm">${i.nomenclature.name}</div>
                            <div class="text-xs text-slate-500 font-mono">${i.nomenclature.code || ''}</div>
                        </td>
                        <td class="p-3 font-mono text-blue-700 text-sm font-bold">${i.serial_number || '-'}</td>
                        <td class="p-3 text-slate-500 font-mono text-xs">${i.inventory_number || '-'} <br><span class="text-[10px]">${priceFmt}</span></td>
                        <td class="p-3 text-center font-bold text-slate-700 bg-slate-50">${i.quantity}</td>
                    </tr>`;
            }).join('');

            tBody.innerHTML = itemsHtml;

        } catch (e) {
            tBody.innerHTML = '<tr><td colspan="4" class="text-rose-500 text-center p-4">Ошибка загрузки.</td></tr>';
        }
    },

    getTypeBadge: (type) => {
        const styles = {
            'Первичный ввод': 'bg-emerald-100 text-emerald-800 border-emerald-200',
            'Отправка': 'bg-amber-100 text-amber-800 border-amber-200',
            'Списание': 'bg-rose-100 text-rose-800 border-rose-200',
            'Прием': 'bg-blue-100 text-blue-800 border-blue-200',
            'Перемещение': 'bg-indigo-100 text-indigo-800 border-indigo-200'
        };
        const cls = styles[type] || 'bg-slate-100 text-slate-800 border-slate-200';
        return `<span class="px-2.5 py-1 rounded-full text-xs font-bold border ${cls}">${type}</span>`;
    }
});