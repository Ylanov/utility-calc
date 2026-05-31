// static/js/modules/operator-info.js
//
// Управление юридическими реквизитами оператора персональных данных.
// Эти поля публикуются в /privacy.html (152-ФЗ) и в подвале всех страниц
// через legal-footer.js. API: GET/PUT /api/settings/operator-info.
//
// Lazy-инициализация: модуль грузится при первом раскрытии секции
// «Юридические реквизиты оператора» во вкладке Операции.

import { api } from '../core/api.js';
import { toast, setLoading, showConfirm } from '../core/dom.js';

export const OperatorInfoModule = {
    isInitialized: false,

    // Маппинг id-HTML → ключ в API (поле OperatorInfoSchema).
    FIELDS: {
        opName: 'operator_name',
        opInn: 'operator_inn',
        opOgrn: 'operator_ogrn',
        opLegalAddress: 'operator_legal_address',
        opPostalAddress: 'operator_postal_address',
        opEmail: 'operator_email',
        opPhone: 'operator_phone',
        // Поля 152-ФЗ (см. юр-аудит май 2026).
        opRknRegistryNumber: 'operator_rkn_registry_number',
        opResponsibleName: 'operator_responsible_name',
        opResponsiblePosition: 'operator_responsible_position',
        opResponsibleEmail: 'operator_responsible_email',
        opInfosystemSecurityLevel: 'operator_infosystem_security_level',
    },

    async init() {
        const form = document.getElementById('operatorInfoForm');
        if (!form) return;

        if (!this.isInitialized) {
            form.addEventListener('submit', (e) => this.handleSubmit(e));
            this.isInitialized = true;
        }
        // Каждый раз при раскрытии секции — подгружаем актуальные данные
        // (на случай если изменения сделаны с другого устройства).
        await this.load();
    },

    async load() {
        try {
            const data = await api.get('/settings/operator-info');
            for (const [htmlId, apiKey] of Object.entries(this.FIELDS)) {
                const inp = document.getElementById(htmlId);
                if (inp) inp.value = data[apiKey] || '';
            }
        } catch (e) {
            console.warn('[operator-info] load failed:', e);
            toast('Не удалось загрузить реквизиты: ' + e.message, 'error');
        }
    },

    async handleSubmit(event) {
        event.preventDefault();
        const form = document.getElementById('operatorInfoForm');
        const button = form.querySelector('button[type="submit"]');

        // Собираем payload.
        const payload = {};
        for (const [htmlId, apiKey] of Object.entries(this.FIELDS)) {
            const inp = document.getElementById(htmlId);
            payload[apiKey] = inp ? inp.value.trim() : '';
        }

        // Soft-валидация — предупреждаем если пусто main поля.
        // Сервер примет пустые (всё опционально на уровне схемы), но без них
        // политика 152-ФЗ нерабочая.
        if (!payload.operator_name || !payload.operator_email) {
            const ok = await showConfirm(
                'Не заполнены: «Наименование организации» и/или «Email для запросов по ПД». ' +
                'Без них политика обработки ПД считается неполной. Сохранить как есть?'
            );
            if (!ok) return;
        }

        setLoading(button, true, 'Сохранение…');
        try {
            await api.put('/settings/operator-info', payload);
            toast('Реквизиты оператора сохранены', 'success');
        } catch (e) {
            toast('Ошибка: ' + e.message, 'error');
        } finally {
            setLoading(button, false);
        }
    },
};
