// static/js/modules/client-dashboard.js
import { Auth } from '../core/auth.js';
import { ClientProfile } from './client-profile.js';
import { ClientHistory } from './client-history.js';
import { ClientReadings } from './client-readings.js';

// Мгновенная защита: выкидываем неавторизованных
if (!Auth.isAuthenticated()) {
    window.location.replace('login.html');
}

export const ClientDashboard = {
    init() {
        this.setupTabs();

        // Показываем контейнер (снимаем opacity=0 из CSS, если есть)
        const container = document.getElementById('app-container');
        if (container) container.style.opacity = '1';

        // Запускаем независимые модули
        ClientProfile.init();
        ClientHistory.init();
        ClientReadings.init();
    },

    setupTabs() {
        const tabs = document.querySelectorAll('.tab-btn');
        const contents = document.querySelectorAll('.tab-content');

        tabs.forEach(tab => {
            tab.addEventListener('click', () => {
                // Убираем active у всех кнопок и контента
                tabs.forEach(t => t.classList.remove('active'));
                contents.forEach(c => c.classList.remove('active'));

                // Добавляем active на нажатую кнопку и соответствующий блок
                tab.classList.add('active');
                const targetId = tab.dataset.tab;
                const targetContent = document.getElementById(targetId);

                if (targetContent) {
                    targetContent.classList.add('active');
                }
            });
        });
    }
};