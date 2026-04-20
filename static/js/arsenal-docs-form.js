/**
 * МОДУЛЬ: DOCUMENTS FORM (Создание документа)
 * ОПТИМИЗАЦИЯ: Серверный Autocomplete с поддержкой Debounce.
 * Умный поиск по остаткам склада (для перемещений) или справочнику (для ввода).
 */

// Локальный escape — это не ESM-модуль, поэтому нельзя импортировать из dom.js.
// Используется чтобы предотвратить XSS через названия номенклатуры,
// серийники, инвентарные номера, которые подставляются в innerHTML autocomplete.
function _escAd(value) {
    if (value === null || value === undefined) return '';
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

window.Documents = window.Documents || {};

Object.assign(window.Documents, {

    // Таймер для задержки ввода (чтобы не спамить сервер запросами)
    searchTimeout: null,

    openCreateModal: () => {
        const form = document.getElementById('newDocForm');
        form.reset();
        document.querySelector('#docItemsTable tbody').innerHTML = '';

        document.getElementById('newDocType').value = 'Первичный ввод';
        document.getElementById('newDocNumber').value = "АВТО";
        document.getElementById('newDocDate').valueAsDate = new Date();

        Documents.updateFormState();
        Documents.addRow(); // Добавляем первую пустую строку
        UI.openModal('newDocModal');

        // Очищаем старые слушатели источника
        const sourceSelect = document.getElementById('newDocSource');
        if (sourceSelect) {
            const newSourceSelect = sourceSelect.cloneNode(true);
            sourceSelect.parentNode.replaceChild(newSourceSelect, sourceSelect);

            // Если поменяли склад отправителя — очищаем таблицу спецификации,
            // так как выбранные товары с прошлого склада уже не актуальны
            newSourceSelect.addEventListener('change', () => {
                document.querySelector('#docItemsTable tbody').innerHTML = '';
                Documents.addRow();
            });
        }

        // Закрытие выпадающих списков при клике вне
        document.addEventListener('click', (e) => {
            if (!e.target.closest('.nom-autocomplete-wrapper')) {
                document.querySelectorAll('.nom-results').forEach(el => el.classList.add('hidden'));
            }
        });
    },

    updateFormState: () => {
        const type = document.getElementById('newDocType').value;
        const sourceContainer = document.getElementById('sourceSelectContainer');
        const targetContainer = document.getElementById('targetSelectContainer');

        if (sourceContainer && targetContainer) {
            sourceContainer.style.display = 'block';
            targetContainer.style.display = 'block';

            if (type === 'Первичный ввод') {
                sourceContainer.style.display = 'none';
                document.getElementById('newDocSource').value = "";
            } else if (type === 'Списание') {
                targetContainer.style.display = 'none';
                document.getElementById('newDocTarget').value = "";
            }
        }

        // При смене типа операции очищаем таблицу, чтобы не отправить "приходные" данные как "расходные"
        document.querySelector('#docItemsTable tbody').innerHTML = '';
        Documents.addRow();
    },

    addRow: () => {
        const tableBody = document.querySelector('#docItemsTable tbody');
        const tr = document.createElement('tr');
        tr.className = 'border-b border-slate-100 last:border-0 hover:bg-slate-50 transition doc-item-row align-top';

        tr.innerHTML = `
            <td class="p-2 min-w-[250px] relative nom-autocomplete-wrapper">
                <div class="relative">
                    <i class="fa-solid fa-search absolute left-2.5 top-2.5 text-slate-400 text-xs"></i>
                    <input type="text" class="nom-display w-full border border-slate-300 pl-8 pr-2 py-2 rounded text-xs bg-white focus:border-blue-500 outline-none font-medium" 
                           placeholder="Поиск по названию, серии, инв.№..." autocomplete="off">
                </div>
                <input type="hidden" class="nom-id-input">
                <!-- Контейнер для результатов поиска -->
                <div class="nom-results hidden absolute top-full left-0 w-[400px] bg-white border border-slate-300 shadow-xl max-h-60 overflow-y-auto z-50 rounded-b-lg"></div>
            </td>
            <td class="p-2 min-w-[120px]">
                <input type="text" class="serial-input w-full border border-slate-300 p-2 rounded text-xs outline-none focus:border-blue-500 font-mono" placeholder="№ / Партия">
            </td>
            <td class="p-2 min-w-[120px]">
                <input type="text" class="inv-input w-full border border-slate-300 p-2 rounded text-xs outline-none font-mono text-slate-500" placeholder="Инв. №">
            </td>
            <td class="p-2 min-w-[100px]">
                <input type="number" step="0.01" class="price-input w-full border border-slate-300 p-2 rounded text-xs outline-none font-mono" placeholder="0.00">
            </td>
            <td class="p-2 min-w-[80px]">
                <input type="number" class="qty-input w-full border border-slate-300 p-2 rounded text-xs text-center outline-none font-bold text-blue-700" value="1" min="1">
            </td>
            <td class="p-2 text-center w-10">
                <button type="button" class="text-slate-400 hover:text-rose-600 transition pt-1.5" onclick="this.closest('tr').remove()"><i class="fa-solid fa-times"></i></button>
            </td>
        `;

        tableBody.appendChild(tr);

        // Инициализация серверного поиска для этой строки
        Documents.bindServerSearch(tr);
    },

    bindServerSearch: (row) => {
        const input = row.querySelector('.nom-display');
        const resultsDiv = row.querySelector('.nom-results');

        input.addEventListener('input', (e) => {
            const query = e.target.value.trim();
            const docType = document.getElementById('newDocType').value;
            const sourceId = document.getElementById('newDocSource')?.value;

            // Если это расходная операция, но склад не выбран — блокируем поиск
            if (docType !== 'Первичный ввод' && !sourceId) {
                UI.showToast("Сначала выберите склад-отправитель!", "error");
                input.value = '';
                return;
            }

            if (query.length < 2) {
                resultsDiv.classList.add('hidden');
                return;
            }

            // Показываем спиннер загрузки
            resultsDiv.innerHTML = '<div class="p-3 text-xs text-slate-500 flex items-center gap-2"><i class="fa-solid fa-spinner fa-spin text-blue-500"></i> Ищем на сервере...</div>';
            resultsDiv.classList.remove('hidden');

            // Очищаем предыдущий таймер
            if (Documents.searchTimeout) clearTimeout(Documents.searchTimeout);

            // Ждем 300мс после окончания ввода (Debounce)
            Documents.searchTimeout = setTimeout(async () => {
                try {
                    let url = '';
                    let isBalanceSearch = false;

                    if (docType === 'Первичный ввод') {
                        // Ищем по всему справочнику номенклатуры
                        url = `/api/arsenal/nomenclature?limit=20&q=${encodeURIComponent(query)}`;
                    } else {
                        // Ищем ТОЛЬКО по остаткам выбранного склада
                        url = `/api/arsenal/balance/${sourceId}?limit=20&q=${encodeURIComponent(query)}`;
                        isBalanceSearch = true;
                    }

                    const res = await apiFetch(url);
                    if (!res || !res.ok) throw new Error("API Error");
                    const data = await res.json();

                    if (data.length === 0) {
                        resultsDiv.innerHTML = '<div class="p-3 text-xs text-rose-500 font-medium">Ничего не найдено</div>';
                        return;
                    }

                    // ИСПРАВЛЕНО XSS: данные из БД (nomenclature, serial_number,
                    // inventory_number, name, code) пробрасывались в innerHTML без
                    // экранирования. Если в номенклатуру попадёт строка с тегами —
                    // получим выполнение JS у любого пользователя в autocomplete.
                    // Все ${...} от item.* теперь идут через _escAd().
                    resultsDiv.innerHTML = data.map(item => {
                        const safeData = encodeURIComponent(JSON.stringify(item));

                        if (isBalanceSearch) {
                            const price = (item.price || 0).toLocaleString('ru-RU', { style: 'currency', currency: 'RUB' });
                            return `
                                <div class="p-2 hover:bg-blue-50 cursor-pointer border-b border-slate-100 last:border-0 transition"
                                     onclick="Documents.selectItem(this, '${safeData}', true)">
                                    <div class="font-bold text-slate-800 text-xs">${_escAd(item.nomenclature)}</div>
                                    <div class="flex justify-between items-center mt-1">
                                        <span class="text-[10px] text-slate-500 font-mono bg-slate-100 px-1.5 py-0.5 rounded">Серия: ${_escAd(item.serial_number)}</span>
                                        <span class="text-[10px] font-bold ${item.quantity > 0 ? 'text-emerald-600' : 'text-rose-500'}">Доступно: ${Number(item.quantity)} шт.</span>
                                    </div>
                                    <div class="text-[10px] text-slate-400 font-mono mt-0.5">Инв: ${_escAd(item.inventory_number || 'Б/Н')} | ${_escAd(price)}</div>
                                </div>
                            `;
                        } else {
                            return `
                                <div class="p-2 hover:bg-blue-50 cursor-pointer border-b border-slate-100 last:border-0 transition"
                                     onclick="Documents.selectItem(this, '${safeData}', false)">
                                    <div class="font-bold text-slate-800 text-xs">${_escAd(item.name)}</div>
                                    <div class="text-[10px] text-slate-400 font-mono mt-0.5">ГРАУ: ${_escAd(item.code || '-')}</div>
                                </div>
                            `;
                        }
                    }).join('');

                } catch (e) {
                    resultsDiv.innerHTML = '<div class="p-3 text-xs text-rose-500">Ошибка поиска на сервере</div>';
                }
            }, 300); // Задержка 300мс
        });
    },

    // Функция выбора элемента из списка результатов поиска
    selectItem: (element, encodedData, isBalanceItem) => {
        const item = JSON.parse(decodeURIComponent(encodedData));
        const row = element.closest('tr');

        const inputDisplay = row.querySelector('.nom-display');
        const hiddenId = row.querySelector('.nom-id-input');
        const resultsDiv = row.querySelector('.nom-results');

        const serialInput = row.querySelector('.serial-input');
        const invInput = row.querySelector('.inv-input');
        const priceInput = row.querySelector('.price-input');
        const qtyInput = row.querySelector('.qty-input');

        if (isBalanceItem) {
            // Заполнение из остатков склада
            inputDisplay.value = item.nomenclature;
            hiddenId.value = item.nomenclature_id;

            serialInput.value = item.serial_number.replace('Партия ', '');
            invInput.value = item.inventory_number !== 'Б/Н' ? item.inventory_number : '';
            priceInput.value = item.price || 0;

            // Настройка количества
            Documents.configureRowState(row, item.is_numbered);
            if (!item.is_numbered) {
                qtyInput.max = item.quantity;
                qtyInput.title = `Максимум: ${item.quantity}`;
            }

            // Если это не партионный учет, блокируем инвентарник и серийник от изменения
            if(item.is_numbered) {
                serialInput.readOnly = true;
                serialInput.classList.add('bg-slate-50', 'text-slate-500');
            }

        } else {
            // Заполнение из Справочника Номенклатуры (Первичный ввод)
            inputDisplay.value = item.name;
            hiddenId.value = item.id;
            Documents.configureRowState(row, item.is_numbered);

            // Фокус переносим на серийный номер для быстрого ввода
            setTimeout(() => serialInput.focus(), 50);
        }

        resultsDiv.classList.add('hidden');
        row.classList.add('bg-emerald-50');
        setTimeout(() => row.classList.remove('bg-emerald-50'), 1000);
    },

    // Настройка состояния полей (Readonly, placeholder) в зависимости от типа учета
    configureRowState: (row, isNumbered) => {
        const qty = row.querySelector('.qty-input');
        const ser = row.querySelector('.serial-input');

        if (isNumbered) {
            qty.value = 1;
            qty.readOnly = true;
            qty.classList.add('bg-slate-100', 'text-slate-500', 'cursor-not-allowed');
            ser.placeholder = "Заводской номер";
        } else {
            qty.readOnly = false;
            qty.classList.remove('bg-slate-100', 'text-slate-500', 'cursor-not-allowed');
            ser.placeholder = "Номер партии (опц.)";
        }
    },

    // Сохранение документа (Осталось без изменений, работает с новыми полями)
    create: async () => {
        const btn = document.getElementById('btnSaveDoc');
        const docType = document.getElementById('newDocType').value;
        const sourceId = document.getElementById('newDocSource')?.value;
        const targetId = document.getElementById('newDocTarget')?.value;
        const dateVal = document.getElementById('newDocDate').value;

        if (!dateVal) return UI.showToast('Укажите дату.', "error");
        if (docType === 'Первичный ввод' && !targetId) return UI.showToast('Укажите получателя.', "error");
        if (docType === 'Списание' && !sourceId) return UI.showToast('Укажите источник.', "error");
        if (['Перемещение', 'Отправка', 'Прием'].includes(docType) && (!sourceId || !targetId)) return UI.showToast('Укажите и отправителя, и получателя.', "error");

        const items = [];
        let valid = true;
        let errorMessage = '';

        document.querySelectorAll('#docItemsTable tbody tr').forEach(row => {
            const nomId = row.querySelector('.nom-id-input').value;
            if (!nomId) return; // Пропускаем пустые строки

            const serial = row.querySelector('.serial-input').value.trim();
            const invNum = row.querySelector('.inv-input').value.trim();
            const price = row.querySelector('.price-input').value;
            const qty = parseInt(row.querySelector('.qty-input').value) || 0;

            const isNum = row.querySelector('.qty-input').readOnly;

            if (isNum && !serial) {
                valid = false;
                errorMessage = 'Для номерных изделий обязательно укажите заводской номер (Серию).';
            }
            if (qty <= 0) {
                valid = false;
                errorMessage = 'Количество должно быть больше 0.';
            }

            items.push({
                nomenclature_id: parseInt(nomId),
                serial_number: serial || (isNum ? null : '1'),
                quantity: qty,
                inventory_number: invNum || null,
                price: price ? parseFloat(price) : null
            });
        });

        if (!valid) return UI.showToast(errorMessage, "error");
        if (items.length === 0) return UI.showToast('Спецификация пуста. Добавьте хотя бы одно изделие.', "error");

        btn.disabled = true;
        const origText = btn.innerHTML;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Сохранение...';

        try {
            const docData = {
                doc_number: null,
                operation_date: dateVal,
                operation_type: docType,
                source_id: sourceId ? parseInt(sourceId) : null,
                target_id: targetId ? parseInt(targetId) : null,
                items: items
            };

            const formData = new FormData();
            formData.append('data', JSON.stringify(docData));

            const fileInput = document.getElementById('newDocFile');
            if (fileInput && fileInput.files.length > 0) {
                formData.append('file', fileInput.files[0]);
            }

            const response = await fetch('/api/arsenal/documents', {
                method: 'POST',
                body: formData
            });

            if (response.ok) {
                UI.closeModal('newDocModal');
                UI.showToast("Документ успешно проведен!", "success");
                if (fileInput) fileInput.value = '';
                Documents.loadList();
                Dashboard.loadKPIs();
            } else {
                const err = await response.json();
                UI.showToast('Ошибка: ' + (err.detail || 'Неизвестная ошибка.'), "error");
            }
        } catch (e) {
            UI.showToast('Сетевая ошибка при сохранении.', "error");
        } finally {
            btn.disabled = false;
            btn.innerHTML = origText;
        }
    }
});