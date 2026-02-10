// static/js/modules/tariffs.js
import { api } from '../core/api.js';
import { setLoading, toast } from '../core/dom.js';

export const TariffsModule = {
    // Флаг для защиты от повторной инициализации событий
    isInitialized: false,

    init() {
        // 1. Навешиваем события ТОЛЬКО один раз
        if (!this.isInitialized) {
            this.setupEventListeners();
            this.isInitialized = true;
        }

        // 2. Загружаем данные каждый раз
        this.load();
    },

    setupEventListeners() {
        console.log('TariffsModule: Event listeners setup.');

        const form = document.getElementById('tariffsForm');
        if (form) {
            form.addEventListener('submit', (e) => this.submit(e));
        }
    },

    async load() {
        try {
            const t = await api.get('/tariffs');

            // Заполняем поля по ID
            // Используем маппинг "ключ из БД" -> "ID инпута"
            const mapping = {
                maintenance_repair: 't_main',
                social_rent: 't_rent',
                waste_disposal: 't_waste',
                heating: 't_heat',
                water_heating: 't_w_heat',
                water_supply: 't_w_sup',
                sewage: 't_sew',
                electricity_per_sqm: 't_el_sqm',
                electricity_rate: 't_el_rate'
            };

            for (const [key, elementId] of Object.entries(mapping)) {
                const input = document.getElementById(elementId);
                if (input && t[key] !== undefined) {
                    input.value = t[key];
                }
            }
        } catch (error) {
            console.error('Ошибка загрузки тарифов:', error);
            toast('Не удалось загрузить тарифы: ' + error.message, 'error');
        }
    },

    async submit(e) {
        e.preventDefault();
        const btn = e.target.querySelector('button[type="submit"]');

        const data = {
            maintenance_repair: parseFloat(document.getElementById('t_main').value),
            social_rent: parseFloat(document.getElementById('t_rent').value),
            heating: parseFloat(document.getElementById('t_heat').value),
            water_heating: parseFloat(document.getElementById('t_w_heat').value),
            water_supply: parseFloat(document.getElementById('t_w_sup').value),
            sewage: parseFloat(document.getElementById('t_sew').value),
            waste_disposal: parseFloat(document.getElementById('t_waste').value),
            electricity_per_sqm: parseFloat(document.getElementById('t_el_sqm').value),
            electricity_rate: parseFloat(document.getElementById('t_el_rate').value)
        };

        setLoading(btn, true, 'Сохранение...');

        try {
            await api.post('/tariffs', data);
            toast("Тарифы успешно обновлены!", "success");
        } catch (error) {
            toast("Ошибка сохранения: " + error.message, "error");
        } finally {
            setLoading(btn, false);
        }
    }
};