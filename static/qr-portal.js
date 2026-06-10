// static/qr-portal.js — анонимный QR-портал подачи показаний по квартире.
// Токен — во фрагменте URL (#...), на сервер в логи/Referer не уходит.
// Все запросы — /api/q/<token>/*. Без ФИО/адреса.

(function () {
  'use strict';

  var app = document.getElementById('app');
  var token = decodeURIComponent((location.hash || '').replace(/^#/, '')).trim();

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function api(path, opts) {
    return fetch('/api/q/' + encodeURIComponent(token) + path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (body) {
        if (!r.ok) { var e = new Error(body.detail || ('Ошибка ' + r.status)); e.status = r.status; throw e; }
        return body;
      });
    });
  }

  function banner(cls, html) { return '<div class="banner ' + cls + '">' + html + '</div>'; }

  // Общий футер: скачать квитанцию (если готова) + связаться с админом.
  function footer(state) {
    var s = state || {};
    var parts = [];
    if (s.receipt_available) {
      parts.push('<a class="ghost" style="display:block; text-align:center; text-decoration:none; line-height:1.3; padding:14px; border-radius:12px;" href="/api/q/' +
        encodeURIComponent(token) + '/receipt">📄 Скачать квитанцию' +
        (s.receipt_period ? ' за ' + esc(s.receipt_period) : '') + '</a>');
    }
    parts.push('<button class="ghost" data-act="contact-toggle">✉️ Связаться с администратором</button>');
    parts.push('<div data-contact-box style="display:none; margin-top:10px;">' +
      '<textarea data-contact-msg rows="4" style="width:100%; box-sizing:border-box; border:2px solid #cbd5e1; border-radius:10px; padding:10px; font-size:15px;" placeholder="Опишите вопрос администратору…"></textarea>' +
      '<button class="primary" data-act="contact-send" style="margin-top:8px;">Отправить</button>' +
      '<div data-contact-out></div></div>');
    return '<div class="card">' + parts.join('') + '</div>';
  }

  var footerWired = false;
  function wireFooterOnce() {
    if (footerWired) return; footerWired = true;
    app.addEventListener('click', function (e) {
      if (e.target.closest('[data-act="contact-toggle"]')) {
        var box = app.querySelector('[data-contact-box]');
        if (box) box.style.display = (box.style.display === 'none' ? 'block' : 'none');
        return;
      }
      var send = e.target.closest('[data-act="contact-send"]');
      if (!send) return;
      var ta = app.querySelector('[data-contact-msg]');
      var out = app.querySelector('[data-contact-out]');
      var msg = ((ta && ta.value) || '').trim();
      if (msg.length < 5) { if (out) out.innerHTML = banner('b-err', 'Напишите вопрос — хотя бы пару слов.'); return; }
      send.disabled = true; send.textContent = 'Отправляем…';
      api('/contact', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message: msg }) })
        .then(function () {
          if (out) out.innerHTML = banner('b-ok', 'Обращение отправлено — администратор увидит его в системе.');
          if (ta) { ta.value = ''; ta.disabled = true; }
          send.style.display = 'none';
        })
        .catch(function (e2) {
          send.disabled = false; send.textContent = 'Отправить';
          if (out) out.innerHTML = banner('b-err', esc(e2.message));
        });
    });
  }

  // Один счётчик: чёрные (5) + красные (3) поля. Защита от пропущенной точки.
  function meterBlock(key, label, icon, val) {
    var black = '', red = '';
    if (val) { var p = String(val).split('.'); black = p[0] || ''; red = p[1] || ''; }
    return '' +
      '<div class="meter">' +
      '  <div class="meter-label">' + icon + ' ' + esc(label) + '</div>' +
      '  <div class="digits">' +
      '    <span class="blk black"><input inputmode="numeric" maxlength="5" data-k="' + key + '-b" value="' + esc(black) + '" placeholder="00000"></span>' +
      '    <span class="dot">,</span>' +
      '    <span class="rd red"><input inputmode="numeric" maxlength="3" data-k="' + key + '-r" value="' + esc(red) + '" placeholder="000"></span>' +
      '  </div>' +
      '  <div class="cap">чёрные цифры&nbsp;&nbsp;,&nbsp;&nbsp;красные (3)</div>' +
      '</div>';
  }

  // Строгая проверка 5+3: РОВНО 5 чёрных и 3 красные цифры (8 всего).
  // empty — оба поля пусты; error — заполнено частично/не 5+3.
  function readMeter(key) {
    var b = (app.querySelector('[data-k="' + key + '-b"]') || {}).value || '';
    var r = (app.querySelector('[data-k="' + key + '-r"]') || {}).value || '';
    b = b.replace(/\D/g, ''); r = r.replace(/\D/g, '');
    if (!b && !r) return { empty: true };
    if (b.length !== 5 || r.length !== 3) return { error: true };
    return { value: b + '.' + r };
  }

  // Блок «Ваши последние показания» + пометка про норматив за пропуски (#2).
  function lastActualBlock(state) {
    var out = '';
    var la = state.last_actual;
    if (la) {
      out += '<div style="background:#f0f9ff; border-left:3px solid var(--pri); border-radius:8px; padding:10px 12px; margin:10px 0; font-size:13px; line-height:1.6;">' +
        '<b>Ваши последние показания</b>' + (la.period ? ' (за ' + esc(la.period) + ')' : '') + ':<br>' +
        '🔥 ГВС ' + esc(la.hot_water) + ' &nbsp; 💧 ХВС ' + esc(la.cold_water) + ' &nbsp; ⚡ ' + esc(la.electricity) +
        '<div style="color:var(--muted); margin-top:4px;">Новые показания не могут быть меньше этих.</div>' +
        '</div>';
    }
    var ns = state.norm_since || [];
    if (ns.length) {
      var lines = ns.map(function (n) {
        var amt = (n.amount != null) ? (' — по нормативу ' + n.amount.toLocaleString('ru-RU') + ' ₽') : '';
        return esc(n.period) + amt;
      }).join('<br>');
      out += banner('b-warn', '⚠️ За эти периоды вы не подавали показания — начислено по нормативу:<br>' + lines);
    }
    return out;
  }

  function showForm(state) {
    var cur = state.current || {};
    var editing = state.editable;
    app.className = '';
    app.innerHTML = '' +
      '<div class="card">' +
      '  <h1>Подача показаний</h1>' +
      '  <p class="sub">Период: ' + esc(state.period || '—') + '</p>' +
      (editing ? banner('b-warn', 'Показания за этот период уже переданы. Можно исправить — измените и отправьте снова.') : '') +
      lastActualBlock(state) +
      '  <div class="hint">Вводите как на счётчике: крупные (чёрные) цифры до запятой, мелкие (красные, 3 шт.) — после. Красные пишите, даже если их «не считают».</div>' +
      meterBlock('hot', 'Горячая вода (ГВС)', '🔥', cur.hot_water) +
      meterBlock('cold', 'Холодная вода (ХВС)', '💧', cur.cold_water) +
      meterBlock('el', 'Электричество', '⚡', cur.electricity) +
      '  <button class="primary" id="send">' + (editing ? 'Исправить показания' : 'Передать показания') + '</button>' +
      '  <div id="msg"></div>' +
      '</div>' + footer(state);

    document.getElementById('send').addEventListener('click', function () {
      var btn = this, msg = document.getElementById('msg');
      var hot = readMeter('hot'), cold = readMeter('cold'), el = readMeter('el');
      var errs = [];
      var FMT = 'введите ВСЕ цифры: 5 чёрных и 3 красные';
      if (hot.empty || hot.error) errs.push('Горячая вода — ' + FMT + '.');
      if (cold.empty || cold.error) errs.push('Холодная вода — ' + FMT + '.');
      if (el.empty || el.error) errs.push('Электричество — ' + FMT + '.');
      if (errs.length) { msg.innerHTML = banner('b-err', errs.join('<br>')); return; }
      var payload = { hot_water: hot.value, cold_water: cold.value, electricity: el.value };
      btn.disabled = true; btn.textContent = 'Отправляем…';
      api('/submit', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      }).then(function () {
        showThanks(state);
      }).catch(function (e) {
        btn.disabled = false; btn.textContent = editing ? 'Исправить показания' : 'Передать показания';
        msg.innerHTML = banner('b-err', esc(e.message));
      });
    });
  }

  function showThanks(state) {
    var period = (state && state.period) || 'период';
    app.className = '';
    app.innerHTML = '' +
      '<div class="card center">' +
      '  <div style="font-size:54px;">✅</div>' +
      '  <h1>Спасибо!</h1>' +
      '  <p class="sub">Показания за ' + esc(period) + ' приняты. Их проверит бухгалтерия.</p>' +
      banner('b-ok', 'Если ошиблись — можно исправить, пока бухгалтерия не приняла показания.') +
      '  <button class="ghost" id="fix">Исправить показания</button>' +
      '</div>' + footer(state);
    document.getElementById('fix').addEventListener('click', load);
  }

  function render(state) {
    if (state.no_residents) {
      app.className = ''; app.innerHTML = '<div class="card">' + banner('b-warn',
        'По этому коду пока нет зарегистрированных жильцов. Обратитесь к администратору.') + '</div>';
      return;
    }
    if (!state.has_period) {
      app.className = ''; app.innerHTML = '<div class="card">' + banner('b-warn',
        'Расчётный период сейчас закрыт. Загляните позже.') + '</div>';
      return;
    }
    if (!state.metered) {
      app.className = ''; app.innerHTML = '<div class="card"><h1>Ваша квартира</h1>' + banner('b-ok',
        'По этой квартире показания счётчиков не подаются — сумма фиксированная.') + '</div>' + footer(state);
      return;
    }
    if (state.approved) {
      app.className = ''; app.innerHTML = '<div class="card center">' +
        '<div style="font-size:54px;">📋</div><h1>Показания приняты</h1>' +
        banner('b-ok', 'Показания за ' + esc(state.period) + ' уже проверены бухгалтерией. Изменить нельзя.') +
        '</div>' + footer(state);
      return;
    }
    if (!state.window_open) {
      var w = state.window || {};
      app.className = ''; app.innerHTML = '<div class="card"><h1>Подача показаний</h1>' + banner('b-warn',
        'Приём показаний сейчас закрыт. Окно подачи: с ' + esc(w.start) + ' по ' + esc(w.end) +
        ' число месяца (сегодня ' + esc(w.today) + ').') + '</div>' + footer(state);
      return;
    }
    showForm(state);
  }

  function load() {
    app.className = 'center'; app.innerHTML = '<span class="spin" style="margin-top:80px;"></span>';
    api('/state').then(render).catch(function (e) {
      app.className = '';
      app.innerHTML = '<div class="card">' + banner('b-err',
        e.status === 404 ? 'Код не найден или больше не действует.' : esc(e.message)) + '</div>';
    });
  }

  if (!token) {
    app.className = '';
    app.innerHTML = '<div class="card">' + banner('b-err', 'Неверная ссылка: нет кода квартиры.') + '</div>';
  } else {
    wireFooterOnce();
    load();
  }
})();
