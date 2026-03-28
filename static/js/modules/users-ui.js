// static/js/modules/users-ui.js
import { api } from '../core/api.js';
import { el, toast } from '../core/dom.js';

// Обработчик выбора общежития -> Загружает список комнат
export async function handleDormChange(dormName, roomSelectEl, infoBoxEl, roomsCache) {
    if (infoBoxEl) infoBoxEl.style.display = 'none'; // Прячем инфо

    if (!dormName) {
        if (roomSelectEl) {
            roomSelectEl.innerHTML = '<option value="">Сначала выберите общежитие</option>';
            roomSelectEl.disabled = true;
        }
        return;
    }

    if (roomSelectEl) {
        roomSelectEl.innerHTML = '<option value="">Загрузка комнат...</option>';
        roomSelectEl.disabled = true;
    }

    try {
        // Кэшируем комнаты, чтобы не делать лишних запросов
        if (!roomsCache[dormName]) {
            const res = await api.get(`/rooms?dormitory=${encodeURIComponent(dormName)}&limit=1000`);
            roomsCache[dormName] = res.items;
        }

        const rooms = roomsCache[dormName];

        if (!roomSelectEl) return;

        if (rooms.length === 0) {
            roomSelectEl.innerHTML = '<option value="">В этом общежитии нет комнат</option>';
            return;
        }

        roomSelectEl.innerHTML = '<option value="">-- Выберите комнату --</option>' +
            rooms.map(r => `<option value="${r.id}">${r.room_number}</option>`).join('');
        roomSelectEl.disabled = false;

    } catch (e) {
        toast('Ошибка загрузки комнат', 'error');
    }
}

// Обработчик выбора комнаты -> Показывает площадь, вместимость и счетчики
export function handleRoomChange(roomIdStr, dormName, domContext, roomsCache) {
    const infoBox = domContext.newRoomInfo || domContext.roomInfo;

    if (!roomIdStr || !dormName || !roomsCache[dormName]) {
        if (infoBox) infoBox.style.display = 'none';
        return;
    }

    const roomId = parseInt(roomIdStr);
    const room = roomsCache[dormName].find(r => r.id === roomId);

    if (room && infoBox) {
        infoBox.style.display = 'block';

        if (domContext.infoArea) domContext.infoArea.textContent = Number(room.apartment_area).toFixed(1);
        if (domContext.infoCap) domContext.infoCap.textContent = room.total_room_residents;
        if (domContext.infoHw) domContext.infoHw.textContent = room.hw_meter_serial || '-';
        if (domContext.infoCw) domContext.infoCw.textContent = room.cw_meter_serial || '-';
        if (domContext.infoEl) domContext.infoEl.textContent = room.el_meter_serial || '-';
    }
}

// Модальное окно с результатами массового импорта Excel
export function showImportResultModal(result) {
    const hasErrors = result.errors && result.errors.length > 0;

    const overlay = el('div', { class: 'modal-overlay open', style: { zIndex: 10000 } });
    const headerTitle = hasErrors ? '⚠️ Результат импорта (Есть ошибки)' : '✅ Импорт успешно завершен';
    const headerColor = hasErrors ? '#d97706' : '#059669';

    const closeBtn = el('button', { class: 'close-icon' }, '×');
    closeBtn.onclick = () => document.body.removeChild(overlay);

    const content = el('div', { class: 'modal-form' },
        el('ul', { style: { marginBottom: '15px', paddingLeft: '20px', fontSize: '15px', color: '#374151' } },
            el('li', { style: { marginBottom: '5px' } }, `Добавлено новых жильцов: `, el('strong', { style: { color: '#059669'} }, String(result.added_users))),
            el('li', {}, `Обновлено существующих: `, el('strong', { style: { color: '#2563eb'} }, String(result.updated_users))),
            el('li', { style: { marginTop: '5px' } }, `Добавлено комнат: `, el('strong', { style: { color: '#059669'} }, String(result.added_rooms))),
            el('li', {}, `Обновлено комнат: `, el('strong', { style: { color: '#2563eb'} }, String(result.updated_rooms)))
        )
    );

    if (hasErrors) {
        const errorBox = el('div', {
            style: {
                maxHeight: '250px', overflowY: 'auto', background: '#fef2f2',
                border: '1px solid #fecaca', borderRadius: '8px', padding: '12px',
                fontSize: '13px', color: '#991b1b', fontFamily: 'monospace'
            }
        });

        result.errors.forEach(err => {
            errorBox.appendChild(el('div', {
                style: { marginBottom: '6px', borderBottom: '1px dashed #fca5a5', paddingBottom: '6px' }
            }, String(err)));
        });

        content.appendChild(el('h4', { style: { marginBottom: '10px', color: '#dc2626', fontSize: '14px' } }, `Ошибки (${result.errors.length}):`));
        content.appendChild(errorBox);
    }

    const btnOk = el('button', { class: 'action-btn primary-btn full-width', style: { marginTop: '20px' } }, 'Понятно, закрыть');
    btnOk.onclick = () => document.body.removeChild(overlay);
    content.appendChild(btnOk);

    const modalWindow = el('div', { class: 'modal-window', style: { width: '550px' } },
        el('div', { class: 'modal-header' },
            el('h3', { style: { color: headerColor } }, headerTitle),
            closeBtn
        ),
        content
    );

    overlay.appendChild(modalWindow);
    document.body.appendChild(overlay);
}