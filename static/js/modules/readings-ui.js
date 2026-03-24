// static/js/modules/readings-ui.js
import { api } from '../core/api.js';
import { el, toast, setLoading } from '../core/dom.js';

/**
 * Генерирует цветные бейджи для статусов риска и аномалий
 */
export function createBadges(details, rawFlags) {
    const container = el('div', { style: { display: 'flex', gap: '4px', flexWrap: 'wrap' } });

    if (details && details.length > 0) {
        details.forEach(d => {
            container.appendChild(el('span', {
                title: d.message,
                style: {
                    background: d.color || '#95a5a6', color: 'white', padding: '2px 6px',
                    borderRadius: '4px', fontSize: '10px', fontWeight: 'bold', cursor: 'help'
                }
            }, d.code));
        });
        return container;
    }

    if (rawFlags && rawFlags !== 'PENDING') {
        rawFlags.split(',').forEach(flag => {
            container.appendChild(el('span', {
                style: {
                    background: '#9ca3af', color: 'white', padding: '2px 6px',
                    borderRadius: '4px', fontSize: '10px', fontWeight: 'bold'
                }
            }, flag));
        });
    }
    return container;
}

/**
 * Модалка: История правок жильцом
 */
export function showHistoryModal(reading) {
    const overlay = el('div', { class: 'modal-overlay open', style: { zIndex: 10000 } });
    const closeBtn = el('button', { class: 'close-icon' }, '×');
    closeBtn.onclick = () => document.body.removeChild(overlay);

    const content = el('div', { class: 'modal-form' });

    if (!reading.edit_history || reading.edit_history.length === 0) {
        content.appendChild(el('p', { style: { textAlign: 'center', color: '#6b7280', padding: '20px 0' } }, 'Жилец передал показания с первого раза. Истории правок нет.'));
    } else {
        const timeline = el('div', { style: { display: 'flex', flexDirection: 'column', gap: '10px' } });

        reading.edit_history.forEach((h, index) => {
            const isLast = index === reading.edit_history.length - 1;
            const item = el('div', {
                style: {
                    padding: '12px', background: isLast ? '#eff6ff' : '#f9fafb',
                    borderLeft: isLast ? '3px solid #3b82f6' : '3px solid #d1d5db',
                    borderRadius: '6px', fontSize: '13px'
                }
            },
                el('div', { style: { fontWeight: 'bold', marginBottom: '5px', color: '#374151' } },
                    isLast ? `🗓️ ${h.date} (Предпоследний вариант)` : `🗓️ ${h.date}`
                ),
                el('div', { style: { color: '#4b5563', fontFamily: 'monospace', fontSize: '14px' } },
                    `ГВС: ${h.hot} | ХВС: ${h.cold} | Свет: ${h.elect}`
                )
            );
            timeline.appendChild(item);
        });

        const currentItem = el('div', {
            style: {
                padding: '12px', background: '#ecfdf5', borderLeft: '3px solid #10b981',
                borderRadius: '6px', fontSize: '13px', marginTop: '10px'
            }
        },
            el('div', { style: { fontWeight: 'bold', marginBottom: '5px', color: '#065f46' } }, `✅ Текущие показания (В таблице)`),
            el('div', { style: { color: '#065f46', fontFamily: 'monospace', fontSize: '14px' } },
                `ГВС: ${reading.cur_hot} | ХВС: ${reading.cur_cold} | Свет: ${reading.cur_elect}`
            )
        );
        timeline.appendChild(currentItem);
        content.appendChild(timeline);
    }

    const btnOk = el('button', { class: 'action-btn primary-btn full-width', style: { marginTop: '20px' } }, 'Закрыть историю');
    btnOk.onclick = () => document.body.removeChild(overlay);
    content.appendChild(btnOk);

    // ИЗМЕНЕНИЕ: В заголовок добавлен адрес (reading.dormitory)
    const titleText = reading.dormitory ? `История: ${reading.username} (${reading.dormitory})` : `История: ${reading.username}`;

    const modalWindow = el('div', { class: 'modal-window', style: { width: '450px' } },
        el('div', { class: 'modal-header' }, el('h3', { style: { fontSize: '16px'} }, titleText), closeBtn),
        content
    );

    overlay.appendChild(modalWindow);
    document.body.appendChild(overlay);
}

/**
 * Модалка: Результат массового импорта Excel
 */
export function showImportResultModal(result) {
    const hasErrors = result.errors && result.errors.length > 0;
    const overlay = el('div', { class: 'modal-overlay open', style: { zIndex: 10000 } });

    const headerTitle = hasErrors ? '⚠️ Результат импорта (Есть ошибки)' : '✅ Импорт успешно завершен';
    const headerColor = hasErrors ? '#d97706' : '#059669';

    const closeBtn = el('button', { class: 'close-icon' }, '×');
    closeBtn.onclick = () => document.body.removeChild(overlay);

    const content = el('div', { class: 'modal-form' },
        el('ul', { style: { marginBottom: '15px', paddingLeft: '20px', fontSize: '15px', color: '#374151' } },
            el('li', { style: { marginBottom: '5px' } }, `Добавлено черновиков: `, el('strong', { style: { color: '#059669' } }, String(result.added || 0))),
            el('li', {}, `Обновлено существующих: `, el('strong', { style: { color: '#2563eb' } }, String(result.updated || 0)))
        )
    );

    if (hasErrors) {
        const errorBox = el('div', {
            style: {
                maxHeight: '250px', overflowY: 'auto', background: '#fef2f2', border: '1px solid #fecaca',
                borderRadius: '8px', padding: '12px', fontSize: '13px', color: '#991b1b', fontFamily: 'monospace'
            }
        });
        result.errors.forEach(err => {
            errorBox.appendChild(el('div', { style: { marginBottom: '6px', borderBottom: '1px dashed #fca5a5', paddingBottom: '6px' } }, String(err)));
        });
        content.appendChild(el('h4', { style: { marginBottom: '10px', color: '#dc2626', fontSize: '14px' } }, `Ошибки (${result.errors.length}):`));
        content.appendChild(errorBox);
    }

    const btnOk = el('button', { class: 'action-btn primary-btn full-width', style: { marginTop: '20px' } }, 'Понятно, закрыть');
    btnOk.onclick = () => document.body.removeChild(overlay);
    content.appendChild(btnOk);

    const modalWindow = el('div', { class: 'modal-window', style: { width: '550px' } },
        el('div', { class: 'modal-header' }, el('h3', { style: { color: headerColor } }, headerTitle), closeBtn),
        content
    );

    overlay.appendChild(modalWindow);
    document.body.appendChild(overlay);
}

/**
 * Открытие модалки ручного утверждения показаний
 */
export function openApproveModal(reading, refreshTableCallback) {
    const modal = document.getElementById('approveModal');
    if (!modal) return;

    document.getElementById('modal_reading_id').value = reading.id;

    // ИЗМЕНЕНИЕ: В модалке утверждения показываем точный адрес, чтобы не перепутать
    const displayInfo = reading.dormitory ? `${reading.username} (${reading.dormitory})` : reading.username;
    document.getElementById('m_username').textContent = displayInfo;

    document.getElementById('m_hot_usage').textContent = (Number(reading.cur_hot) - Number(reading.prev_hot)).toFixed(3);
    document.getElementById('m_cold_usage').textContent = (Number(reading.cur_cold) - Number(reading.prev_cold)).toFixed(3);
    document.getElementById('m_elect_usage').textContent = (Number(reading.cur_elect) - Number(reading.prev_elect)).toFixed(3);['m_corr_hot', 'm_corr_cold', 'm_corr_elect', 'm_corr_sewage'].forEach(id => {
        const input = document.getElementById(id);
        if (input) input.value = 0;
    });

    modal.classList.add('open');

    const btnSubmit = document.getElementById('btnModalSubmit');
    // Перезаписываем обработчик, чтобы не плодить слушателей
    btnSubmit.onclick = () => submitApproval(reading.id, refreshTableCallback);

    const btnClose = document.getElementById('btnModalClose');
    btnClose.onclick = () => modal.classList.remove('open');
}

/**
 * Логика отправки данных на утверждение
 */
async function submitApproval(id, refreshTableCallback) {
    const btn = document.getElementById('btnModalSubmit');
    const parseInput = (elId) => {
        const el = document.getElementById(elId);
        return (!el || !el.value) ? 0 : (parseFloat(el.value.replace(',', '.')) || 0);
    };

    const data = {
        hot_correction: parseInput('m_corr_hot'),
        cold_correction: parseInput('m_corr_cold'),
        electricity_correction: parseInput('m_corr_elect'),
        sewage_correction: parseInput('m_corr_sewage')
    };

    setLoading(btn, true, 'Сохранение...');
    try {
        const res = await api.post(`/admin/approve/${id}`, data);
        toast(`Утверждено! Сумма: ${Number(res.new_total).toFixed(2)} ₽`, 'success');
        document.getElementById('approveModal').classList.remove('open');

        if (typeof refreshTableCallback === 'function') refreshTableCallback();
    } catch (e) {
        toast('Ошибка: ' + e.message, 'error');
    } finally {
        setLoading(btn, false);
    }
}