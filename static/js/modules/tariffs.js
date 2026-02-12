// static/js/modules/tariffs.js
import { api } from '../core/api.js';
import { setLoading, toast } from '../core/dom.js';

export const TariffsModule = {
    isInitialized: false,

    // Карта соответствия: ключ_в_БД -> id_в_HTML
    MAPPING: {
        maintenance_repair: 't_main',
        social_rent: 't_rent',
        waste_disposal: 't_waste',
        heating: 't_heat',
        water_heating: 't_w_heat',
        water_supply: 't_w_sup',
        sewage: 't_sew',
        electricity_rate: 't_el_rate',
        electricity_per_sqm: 't_el_sqm'
    },

    init() {
        // События вешаем только один раз
        if (!this.isInitialized) {
            this.bindEvents();
            this.isInitialized = true;
        }

        // Данные загружаем каждый раз при открытии вкладки
        this.load();
    },

    bindEvents() {
        const form = document.getElementById('tariffsForm');
        if (form) {
            form.addEventListener('submit', (e) => this.handleSubmit(e));
        }
    },

    async load() {
        try {
            const tariffs = await api.get('/tariffs');

            // Проходим по нашей карте и заполняем инпуты
            for (const [dbKey, htmlId] of Object.entries(this.MAPPING)) {
                const input = document.getElementById(htmlId);
                if (input && tariffs[dbKey] !== undefined) {
                    input.value = Number(tariffs[dbKey]).toFixed(2);
                }
            }
        } catch (error) {
            toast('Не удалось загрузить тарифы: ' + error.message, 'error');
        }
    },

    async handleSubmit(e) {
        e.preventDefault();

        const form = e.target;
        const btnSubmit = form.querySelector('button[type="submit"]');

        // Собираем данные из формы обратно в объект по карте
        const data = {};
        for (const [dbKey, htmlId] of Object.entries(this.MAPPING)) {
            const input = document.getElementById(htmlId);
            if (input) {
                // Обязательно парсим в число (float)
                data[dbKey] = parseFloat(input.value) || 0;
            }
        }

        setLoading(btnSubmit, true, 'Сохранение...');

        try {
            await api.post('/tariffs', data);
            toast('Тарифы успешно обновлены!', 'success');
        } catch (error) {
            toast('Ошибка сохранения: ' + error.message, 'error');
        } finally {
            setLoading(btnSubmit, false);
        }
    }
};