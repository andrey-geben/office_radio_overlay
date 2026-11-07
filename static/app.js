let formInitialized = false;
let sipInitialized = false;
let fallbackInitialized = false;
let lastHistogram = null;
let lastSpectrum = null;
let streamConnected = false;

const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[c]));

// ==================== Стрелочные индикаторы ====================
function setMeter(id, value, min, max) {
    const el = $(id);
    if (!el) return;
    const clamp = Math.max(min, Math.min(max, value));
    const ratio = (clamp - min) / (max - min);
    const angle = -90 + ratio * 180;
    el.setAttribute('transform', `rotate(${angle.toFixed(1)} 50 52)`);
}

// ==================== Эквалайзер ====================
function drawEqualizer(spectrum) {
    const canvas = $('eq-canvas');
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const W = canvas.clientWidth;
    const H = canvas.clientHeight || 80;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    const bars = spectrum.length || 16;
    const totalW = W;
    const barW = (totalW / bars) * 0.7;
    const gap = (totalW / bars) * 0.3;
    for (let i = 0; i < bars; i++) {
        const v = Math.max(0, Math.min(1, spectrum[i] || 0));
        const h = v * (H - 4);
        const x = i * (barW + gap) + gap / 2;
        const y = H - h;
        const grad = ctx.createLinearGradient(0, H, 0, 0);
        grad.addColorStop(0, '#3aa83a');
        grad.addColorStop(0.55, '#d4c44a');
        grad.addColorStop(1, '#c83a3a');
        ctx.fillStyle = grad;
        ctx.fillRect(x, y, barW, h);
        // отражение сверху — яркая точка
        if (v > 0.1) {
            ctx.fillStyle = 'rgba(255,230,150,0.6)';
            ctx.fillRect(x, y, barW, 2);
        }
    }
    // сетка
    ctx.strokeStyle = 'rgba(60,40,25,0.4)';
    ctx.lineWidth = 1;
    for (let i = 1; i < 4; i++) {
        const y = (H / 4) * i;
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(W, y);
        ctx.stroke();
    }
}

// ==================== Гистограмма ====================
function drawToneHistogram(hist) {
    const canvas = $('tone-canvas');
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const W = canvas.clientWidth;
    const H = 100;
    canvas.width = W * dpr; canvas.height = H * dpr;
    canvas.style.height = H + 'px';
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    const buckets = hist.buckets || [];
    if (!buckets.length) return;
    const maxCount = Math.max(1, ...buckets.map(b => b.ctcss + b.dcs));
    const bw = W / buckets.length;
    const padY = 6;
    const plotH = H - 2 * padY;

    ctx.strokeStyle = 'rgba(60,40,25,0.3)';
    for (let i = 1; i <= 3; i++) {
        const y = padY + plotH * (i / 4);
        ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    }

    for (let i = 0; i < buckets.length; i++) {
        const b = buckets[i];
        const total = b.ctcss + b.dcs;
        if (total === 0) continue;
        const x = i * bw;
        const barW = Math.max(2, bw - 1);
        const h = (total / maxCount) * plotH;
        const ctcssH = (b.ctcss / total) * h;
        const dcsH = h - ctcssH;
        ctx.fillStyle = '#3aa83a';
        ctx.fillRect(x, H - padY - ctcssH, barW, ctcssH);
        if (b.dcs > 0) {
            ctx.fillStyle = '#3a7ac8';
            ctx.fillRect(x, H - padY - ctcssH - dcsH, barW, dcsH);
        }
    }
    ctx.strokeStyle = 'rgba(200,60,60,0.6)';
    ctx.beginPath();
    ctx.moveTo(W - 0.5, padY);
    ctx.lineTo(W - 0.5, H - padY);
    ctx.stroke();
}

window.addEventListener('resize', () => {
    if (lastHistogram) drawToneHistogram(lastHistogram);
    if (lastSpectrum) drawEqualizer(lastSpectrum);
});

// ==================== Рендер статуса ====================
function renderIndicators(state) {
    $('ind-ctcss').classList.toggle('active', !!state.ctcss_detected);
    $('ind-dcs').classList.toggle('active', !!state.dcs_detected);
    $('ind-onair').classList.toggle('active', !!state.announcement_active || !!state.sip_active);
    $('ind-ctcss-p').textContent = (state.ctcss_power ?? 0).toFixed(3);
    $('ind-dcs-p').textContent = (state.dcs_power ?? 0).toFixed(3);
}

function renderMeters(state) {
    setMeter('needle-signal', state.signal_db ?? -90, -60, 0);
    $('signal-db').textContent = (state.signal_db ?? -90).toFixed(1);

    setMeter('needle-cpu', state.cpu_percent ?? 0, 0, 100);
    $('cpu-val').textContent = Math.round(state.cpu_percent ?? 0);

    const bps = state.network_bps || 0;
    const kb = bps / 1024;
    setMeter('needle-net', kb, 0, 64);
    $('net-val').textContent = bps < 1024 ? bps : kb.toFixed(1);
}

function renderStatus(state, settings) {
    $('running').textContent = state.running ? '✅ работает' : '⏹ остановлен';
    $('current-source').textContent = state.current_source || '—';
    $('count').textContent = state.announcements_played;
    $('event').textContent = state.last_event || '—';
    $('error').textContent = state.error || '—';
    $('seconds-since-tone').textContent = state.seconds_since_tone ?? 0;
    $('btn-start').disabled = state.running;
    $('btn-stop').disabled = !state.running;

    let nowText;
    if (state.sip_active) nowText = '📞 SIP-звонок';
    else if (!state.running) nowText = '⏹ остановлен';
    else if (state.announcement_active) nowText = '🔴 объявление';
    else if (state.using_reserve) nowText = '🟡 резервный источник';
    else nowText = '🟢 трансляция';
    $('onair-text').textContent = nowText;

    renderMeters(state);
    renderIndicators(state);

    if (state.spectrum) {
        lastSpectrum = state.spectrum;
        drawEqualizer(state.spectrum);
    }
    if (state.tone_histogram) {
        lastHistogram = state.tone_histogram;
        drawToneHistogram(state.tone_histogram);
    }
    if (state.session_tones) {
        $('th-ctcss-total').textContent = state.session_tones.ctcss;
        $('th-dcs-total').textContent = state.session_tones.dcs;
    }
}

function fillForm(s) {
    for (const k of ['tone_freq', 'detection_threshold', 'dcs_freq', 'dcs_threshold',
                     'ducking_level', 'announcement_level', 'fade_out_ms', 'fade_in_ms',
                     'tone_timeout_seconds', 'fallback_timeout_seconds', 'fallback_retry_main_seconds']) {
        const el = $(k);
        if (el) el.value = s[k];
    }
    $('simulate').checked = !!s.simulate_tone;
    $('tone_timeout_enabled').checked = !!s.tone_timeout_enabled;
    $('fallback_enabled').checked = !!s.fallback_enabled;
}

function updateSourceUI(s) {
    const type = s.source_type || 'stream';
    const r = document.querySelector(`input[name="source_type"][value="${type}"]`);
    if (r) r.checked = true;
    $('source-stream').style.display = type === 'device' ? 'none' : '';
    $('source-device').style.display = type === 'device' ? '' : 'none';
    $('source-url').value = s.radio_url || '';
    const sel = $('source-device-select');
    sel.value = s.input_device != null ? String(s.input_device) : '';
}

// ==================== Объявления ====================
function scheduleText(it) {
    const a = it.start_time, b = it.end_time, lim = it.repeat_limit || 0;
    const parts = [];
    if (a && b) parts.push(`${a}–${b}`);
    else if (a) parts.push(`с ${a}`);
    else if (b) parts.push(`до ${b}`);
    else parts.push('весь день');
    if (lim > 0) parts.push(`≤${lim}`);
    return parts.join(' · ');
}

function renderAnnouncements(items, activeId) {
    const tb = $('ann-tbody');
    if (!items.length) {
        tb.innerHTML = '<tr><td colspan="7" class="empty">Загрузок пока нет</td></tr>';
        return;
    }
    tb.innerHTML = '';
    for (const it of items) {
        const tr = document.createElement('tr');
        if (it.id === activeId) tr.classList.add('active-row');
        const lim = it.repeat_limit > 0 ? `/${it.repeat_limit}` : '';
        const sizeKb = Math.round(it.size / 1024);
        const last = it.last_played ? it.last_played.replace('T', ' ').slice(0, 19) : '—';
        tr.innerHTML = `
            <td class="icon">${it.id === activeId ? '🔊' : ''}</td>
            <td>
                <div class="fname">${esc(it.original_name)}</div>
                <div class="fdesc">${esc(it.description || '—')}</div>
                <div class="fsize">${sizeKb} КБ · ${it.duration ?? '?'} с</div>
            </td>
            <td class="small">${scheduleText(it)}</td>
            <td class="counter">${it.plays_today ?? 0}${lim}</td>
            <td class="counter">${it.plays_total ?? 0}</td>
            <td class="small">${last}</td>
            <td class="actions">
                <button class="btn-sm btn-activate" data-id="${it.id}" ${it.id === activeId ? 'disabled' : ''}>
                    ${it.id === activeId ? 'Активно' : 'Активировать'}
                </button>
                <button class="btn-sm btn-edit" data-id="${it.id}">Расписание</button>
                <button class="btn-sm btn-delete" data-id="${it.id}">Удалить</button>
            </td>`;
        tb.appendChild(tr);
    }
    tb.querySelectorAll('.btn-activate').forEach(b => b.onclick = () => activateAnn(b.dataset.id));
    tb.querySelectorAll('.btn-delete').forEach(b => b.onclick = () => deleteAnn(b.dataset.id));
    tb.querySelectorAll('.btn-edit').forEach(b =>
        b.onclick = () => editSchedule(b.dataset.id, items.find(x => x.id === b.dataset.id))
    );
}

async function activateAnn(id) {
    await fetch(`/api/announcements/${id}/activate`, { method: 'POST' });
    refreshAll();
}
async function deleteAnn(id) {
    if (!confirm('Удалить это объявление?')) return;
    await fetch(`/api/announcements/${id}`, { method: 'DELETE' });
    refreshAll();
}
async function editSchedule(id, item) {
    const start = prompt('Действует с (HH:MM, пусто = весь день):', item.start_time || '');
    if (start === null) return;
    const end = prompt('Действует до (HH:MM, пусто = без ограничения):', item.end_time || '');
    if (end === null) return;
    const lim = prompt('Повторов в день (0 = без лимита):', item.repeat_limit || 0);
    if (lim === null) return;
    await fetch(`/api/announcements/${id}/schedule`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            start_time: start || null,
            end_time: end || null,
            repeat_limit: parseInt(lim) || 0
        }),
    });
    refreshAll();
}

// ==================== Fallback ====================
async function loadFallbackSources() {
    try {
        const r = await fetch('/api/fallback/sources');
        const data = await r.json();
        renderFallbackList(data.items || []);
        fallbackInitialized = true;
    } catch (e) { console.error('loadFallbackSources', e); }
}

function renderFallbackList(items) {
    const box = $('fallback-list');
    if (!items.length) {
        box.innerHTML = '<div class="fallback-empty">Резервных каналов нет</div>';
        return;
    }
    box.innerHTML = '';
    items.forEach((it, idx) => {
        const row = document.createElement('div');
        row.className = 'fallback-item';
        row.innerHTML = `
            <div class="fb-name">${esc(it.name || `Резерв ${idx+1}`)}</div>
            <div class="fb-url" title="${esc(it.url)}">${esc(it.url)}</div>
            <button data-idx="${idx}">Удалить</button>`;
        row.querySelector('button').onclick = async () => {
            const newItems = items.filter((_, i) => i !== idx);
            await saveFallbackSources(newItems);
        };
        box.appendChild(row);
    });
}

async function saveFallbackSources(items) {
    try {
        const r = await fetch('/api/fallback/sources', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ items }),
        });
        const data = await r.json();
        if (data.ok) renderFallbackList(data.items || []);
    } catch (e) { console.error(e); }
}

$('btn-add-fallback').onclick = async () => {
    const name = $('new-fallback-name').value.trim();
    const url = $('new-fallback-url').value.trim();
    if (!url) { alert('Введите URL резервного потока'); return; }
    const r = await fetch('/api/fallback/sources');
    const data = await r.json();
    const items = data.items || [];
    items.push({ name: name || `Резерв ${items.length + 1}`, url });
    await saveFallbackSources(items);
    $('new-fallback-name').value = '';
    $('new-fallback-url').value = '';
};

// ==================== Прочие загрузчики ====================
async function loadInputDevices() {
    try {
        const { items } = await (await fetch('/api/devices')).json();
        const sel = $('source-device-select');
        const cur = sel.value;
        sel.innerHTML = '<option value="">По умолчанию</option>';
        for (const d of items) {
            const o = document.createElement('option');
            o.value = String(d.index);
            o.textContent = `#${d.index} ${d.name} (${d.channels}ch)${d.is_default ? ' ★' : ''}`;
            sel.appendChild(o);
        }
        if (cur) sel.value = cur;
    } catch (e) { console.error(e); }
}

async function loadHwOutputs() {
    try {
        const { items } = await (await fetch('/api/outputs')).json();
        const sel = $('hw-output-select');
        const cur = sel.value;
        sel.innerHTML = '<option value="">Отключён</option>';
        for (const d of items) {
            const o = document.createElement('option');
            o.value = String(d.index);
            o.textContent = `#${d.index} ${d.name} (${d.channels}ch)${d.is_default ? ' ★' : ''}`;
            sel.appendChild(o);
        }
        if (cur) sel.value = cur;
    } catch (e) { console.error(e); }
}

async function loadSinkDevices() {
    try {
        if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
        const devices = await navigator.mediaDevices.enumerateDevices();
        const sel = $('sink-select');
        const cur = sel.value;
        sel.innerHTML = '<option value="">По умолчанию</option>';
        for (const d of devices) {
            if (d.kind === 'audiooutput') {
                const o = document.createElement('option');
                o.value = d.deviceId;
                o.textContent = d.label || `Устройство ${d.deviceId.slice(0, 6)}`;
                sel.appendChild(o);
            }
        }
        if (cur) sel.value = cur;
    } catch (e) { console.error(e); }
}

// ==================== Главный refresh ====================
async function refreshAll() {
    try {
        const [sr, ar] = await Promise.all([fetch('/api/status'), fetch('/api/announcements')]);
        if (sr.status === 401) { location.href = '/login'; return; }
        const { state, settings } = await sr.json();
        const { items } = await ar.json();

        renderStatus(state, settings);

        if (!formInitialized) {
            fillForm(settings);
            updateSourceUI(settings);
            formInitialized = true;
        }
        if (!fallbackInitialized) {
            loadFallbackSources();
        }
        renderAnnouncements(items, settings.active_announcement_id);

        if (settings.hw_output_device != null && $('hw-output-select').value === '') {
            $('hw-output-select').value = String(settings.hw_output_device);
        }
        refreshSipStatus();
    } catch (e) { console.error('refreshAll', e); }
}

// ==================== Обработчики ====================
document.querySelectorAll('input[name="source_type"]').forEach(r => {
    r.addEventListener('change', () => {
        const isDev = r.value === 'device' && r.checked;
        $('source-stream').style.display = isDev ? 'none' : '';
        $('source-device').style.display = isDev ? '' : 'none';
    });
});
$('btn-refresh-devices').onclick = loadInputDevices;

$('btn-apply-source').onclick = async () => {
    const st = $('source-status');
    const type = document.querySelector('input[name="source_type"]:checked').value;
    const url = $('source-url').value.trim();
    const dv = $('source-device-select').value;
    if (type === 'stream' && !url) { st.textContent = '❌ Укажите URL'; return; }
    st.textContent = '⏳ Применение...';
    try {
        const r = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                source_type: type,
                radio_url: url,
                input_device: dv === '' ? null : parseInt(dv)
            }),
        });
        const data = await r.json();
        st.textContent = data.restarting ? '✅ Применено, перезапуск...' : '✅ Применено';
        formInitialized = false;
        refreshAll();
    } catch (e) { st.textContent = '❌ ' + e.message; }
    setTimeout(() => st.textContent = '', 4000);
};

$('btn-refresh-hw-outputs').onclick = loadHwOutputs;
$('btn-apply-hw-output').onclick = async () => {
    const status = $('output-status');
    const val = $('hw-output-select').value;
    status.textContent = '⏳ Применение...';
    try {
        const r = await fetch('/api/settings/output', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ hw_output_device: val === '' ? null : parseInt(val) }),
        });
        const data = await r.json();
        status.textContent = data.ok ? '✅ Применено' : '❌ ошибка';
    } catch (e) { status.textContent = '❌ ' + e.message; }
    setTimeout(() => status.textContent = '', 4000);
};

$('btn-refresh-sinks').onclick = loadSinkDevices;
$('sink-select').onchange = async (e) => {
    const id = e.target.value;
    const player = $('player');
    if (!player.setSinkId) {
        $('output-status').textContent = '❌ браузер не поддерживает выбор';
        return;
    }
    try { await player.setSinkId(id || ''); }
    catch (err) { $('output-status').textContent = '❌ ' + err.message; }
};

$('btn-stream-connect').onclick = async () => {
    const player = $('player');
    const btn = $('btn-stream-connect');
    if (streamConnected) {
        player.pause();
        player.removeAttribute('src');
        player.load();
        streamConnected = false;
        btn.textContent = '▶ Подключить';
    } else {
        player.src = '/stream';
        try {
            await player.play();
            streamConnected = true;
            btn.textContent = '⏸ Отключить';
        } catch (e) {
            $('output-status').textContent = '❌ ' + e.message;
        }
    }
};

$('btn-start').onclick = async () => { await fetch('/api/start', { method: 'POST' }); refreshAll(); };
$('btn-stop').onclick = async () => { await fetch('/api/stop', { method: 'POST' }); refreshAll(); };

$('settings-form').onsubmit = async e => {
    e.preventDefault();
    const data = {};
    for (const el of e.target.elements) {
        if (!el.name) continue;
        if (el.type === 'checkbox') data[el.name] = el.checked;
        else if (el.type === 'number') data[el.name] = parseFloat(el.value);
        else data[el.name] = el.value;
    }
    // fallback_enabled и его параметры не входят в settings-form — добавим вручную
    data.fallback_enabled = $('fallback_enabled').checked;
    data.fallback_timeout_seconds = parseFloat($('fallback_timeout_seconds').value) || 10;
    data.fallback_retry_main_seconds = parseFloat($('fallback_retry_main_seconds').value) || 60;

    await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
    });
    formInitialized = false;
    refreshAll();
};

$('simulate').onchange = async e => {
    await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ simulate_tone: e.target.checked }),
    });
    refreshAll();
};

$('upload-form').onsubmit = async e => {
    e.preventDefault();
    const status = $('upload-status');
    const fd = new FormData(e.target);
    status.textContent = '⏳ Загрузка...';
    try {
        const r = await fetch('/api/announcements', { method: 'POST', body: fd });
        const data = await r.json();
        status.textContent = data.ok ? '✅ Загружено' : '❌ ' + (data.error || 'ошибка');
        if (data.ok) { e.target.reset(); refreshAll(); }
    } catch (err) { status.textContent = '❌ ' + err.message; }
    setTimeout(() => status.textContent = '', 4000);
};

// ==================== Скан субтонов ====================
$('btn-scan-tone').onclick = async () => {
    const status = $('scan-status');
    const resultsBox = $('scan-results');
    status.textContent = '⏳ Сканирование...';
    resultsBox.innerHTML = '';
    try {
        const r = await fetch('/api/scan_tone', { method: 'POST' });
        const data = await r.json();
        if (!data.ok) { status.textContent = '❌ ' + (data.error || 'ошибка'); return; }
        status.textContent = `✅ Проанализировано ${data.buffer_seconds} с`;
        renderScanResults(data.candidates);
    } catch (err) { status.textContent = '❌ ' + err.message; }
};

function renderScanResults(candidates) {
    const box = $('scan-results');
    if (!candidates || !candidates.length) {
        box.innerHTML = '<div class="empty">Нет кандидатов</div>';
        return;
    }
    let html = '<table class="scan-table"><thead><tr>' +
        '<th>Частота</th><th>Мощность</th><th>Оценка</th><th></th></tr></thead><tbody>';
    for (const c of candidates) {
        const power = c.power;
        let rating, cls;
        if (power >= 0.05) { rating = '🟢 уверенно'; cls = 'good'; }
        else if (power >= 0.01) { rating = '🟡 вероятно'; cls = 'mid'; }
        else { rating = '⚪ слабо'; cls = 'weak'; }
        html += `<tr class="${cls}">
            <td><b>${c.freq.toFixed(1)} Гц</b></td>
            <td class="mono">${power.toFixed(6)}</td>
            <td>${rating}</td>
            <td><button class="btn-sm btn-apply-tone" data-freq="${c.freq}">Применить</button></td>
        </tr>`;
    }
    html += '</tbody></table>';
    box.innerHTML = html;
    box.querySelectorAll('.btn-apply-tone').forEach(b => {
        b.onclick = async () => {
            const freq = parseFloat(b.dataset.freq);
            const r = await fetch('/api/apply_tone', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ freq }),
            });
            if (r.ok) {
                $('tone_freq').value = freq;
                $('scan-status').textContent = `✅ Применено: CTCSS ${freq} Гц`;
                formInitialized = false;
                refreshAll();
            }
        };
    });
}

// ==================== SIP ====================
async function refreshSipStatus() {
    try {
        const r = await fetch('/api/sip/status');
        if (r.status === 401) return;
        const data = await r.json();
        $('sip-status').textContent = data.running ? '✅ подключён' : '⏹ отключён';
        $('sip-engine').textContent = data.engine_sip_active ? '📞 активен' : '—';
        $('btn-sip-start').disabled = data.running;
        $('btn-sip-stop').disabled = !data.running;

        if (!sipInitialized) {
            $('sip_domain').value = data.sip_domain || '';
            $('sip_username').value = data.sip_username || '';
            $('sip_port').value = data.sip_port || 5060;
            $('sip_transport').value = data.sip_transport || 'udp';
            $('auto_start_sip').checked = !!data.auto_start_sip;
            $('auto_start_engine').checked = !!data.auto_start_engine;
            $('sip_password').placeholder = data.sip_password_set
                ? '•••••••• (сохранён)' : 'Введите пароль';
            sipInitialized = true;
        }
    } catch (e) { console.error(e); }
}

function _sipPayload() {
    return {
        sip_domain: $('sip_domain').value.trim(),
        sip_username: $('sip_username').value.trim(),
        sip_password: $('sip_password').value,
        sip_port: parseInt($('sip_port').value) || 5060,
        sip_transport: $('sip_transport').value,
    };
}

$('btn-sip-save').onclick = async () => {
    const msg = $('sip-message');
    msg.textContent = '⏳ Сохранение...';
    try {
        const r = await fetch('/api/sip/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(_sipPayload()),
        });
        const data = await r.json();
        msg.textContent = data.ok ? '✅ Сохранено' : '❌ ' + (data.error || 'ошибка');
        if (data.ok) {
            $('sip_password').value = '';
            sipInitialized = false;
            refreshSipStatus();
        }
    } catch (e) { msg.textContent = '❌ ' + e.message; }
    setTimeout(() => msg.textContent = '', 4000);
};

$('btn-sip-start').onclick = async () => {
    const msg = $('sip-message');
    msg.textContent = '⏳ Подключение...';
    try {
        const r = await fetch('/api/sip/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(_sipPayload()),
        });
        const data = await r.json();
        msg.textContent = data.ok ? '✅ SIP подключён' : '❌ ' + (data.error || 'ошибка');
        $('sip_password').value = '';
        sipInitialized = false;
        refreshSipStatus();
    } catch (e) { msg.textContent = '❌ ' + e.message; }
    setTimeout(() => msg.textContent = '', 5000);
};

$('btn-sip-stop').onclick = async () => {
    await fetch('/api/sip/stop', { method: 'POST' });
    refreshSipStatus();
};

$('auto_start_sip').onchange = async e => {
    await fetch('/api/autostart', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            auto_start_sip: e.target.checked,
            auto_start_engine: $('auto_start_engine').checked,
        }),
    });
};

$('auto_start_engine').onchange = async e => {
    await fetch('/api/autostart', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            auto_start_engine: e.target.checked,
            auto_start_sip: $('auto_start_sip').checked,
        }),
    });
};

$('fallback_enabled').onchange = async e => {
    await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ fallback_enabled: e.target.checked }),
    });
    refreshAll();
};

// ==================== Старт ====================
loadInputDevices();
loadHwOutputs();
loadSinkDevices();
loadFallbackSources();
refreshSipStatus();
setInterval(refreshAll, 1000);
refreshAll();