/* Voltaneum · interface de supervision (lecture seule).
   Données : routes JSON du serveur (dashboard/server.py) + flux temps réel /api/stream. */
(function () {
  'use strict';

  // ================================================================ outils
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  // Noms des sondes : valeurs par défaut identiques à config.py, remplacées par celles du serveur
  // (réglables dans .env) dès le premier instantané — voir applyLabels().
  const LABELS = {
    pt01: 'PT01 · liquide diélectrique', pt02: 'PT02 · liquide diélectrique (régulé)',
    pt03: 'PT03 · liquide de refroidissement', pt04: 'PT04 · liquide de refroidissement',
    ht01: 'HT01 · débit', freq_pompe: 'Fréquence pompe', temp_outdoor: 'Air extérieur',
    thermal_power_kw: 'Puissance dissipée (estimée)', humidity_outdoor: 'Humidité extérieure',
  };
  const UNITS = { pt01: '°C', pt02: '°C', pt03: '°C', pt04: '°C', ht01: 'm³/h', freq_pompe: 'Hz', temp_outdoor: '°C', thermal_power_kw: 'kW', humidity_outdoor: '%' };
  const DIGITS = { pt01: 1, pt02: 1, pt03: 1, pt04: 1, ht01: 2, freq_pompe: 1, temp_outdoor: 1, thermal_power_kw: 1, humidity_outdoor: 0 };
  const COLORS = { pt01: '--s2', pt02: '--s1', pt03: '--s3', pt04: '--s4', temp_outdoor: '--s5', freq_pompe: '--s5', ht01: '--s7', thermal_power_kw: '--s2', humidity_outdoor: '--s7' };
  const MODEL_LABELS = { random_forest: 'Forêt aléatoire', gradient_boosting: 'Gradient boosting', baseline_persistence: 'Persistance (référence)' };
  const MODEL_COLORS = { random_forest: '--pred', gradient_boosting: '--pred-2' };

  const fmtTime = new Intl.DateTimeFormat('fr-FR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const fmtDateTime = new Intl.DateTimeFormat('fr-FR', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
  const fmtShort = new Intl.DateTimeFormat('fr-FR', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });

  function fmtHorizon(sec) {
    if (sec == null) return '—';
    if (sec < 3600) return Math.round(sec / 60) + ' min';
    const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
    return h + ' h' + (m ? ' ' + String(m).padStart(2, '0') : '');
  }
  const num = (v, d = 1) => (v == null || !isFinite(v)) ? '—' : Number(v).toFixed(d).replace('.', ',').replace(/^-(0,?0*)$/, '$1');
  const signed = (v, d = 1) => (v > 0 ? '+' : v < 0 ? '−' : '±') + num(Math.abs(v), d);
  function bytes(n) {
    if (n == null) return '—';
    const u = ['o', 'Ko', 'Mo', 'Go', 'To'];
    let i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return num(n, n < 10 && i ? 1 : 0) + ' ' + u[i];
  }
  function ago(ms) {
    const s = Math.max(0, Math.round(ms / 1000));
    if (s < 60) return s + ' s';
    const m = Math.floor(s / 60);
    if (m < 60) return m + ' min' + (s % 60 && m < 10 ? ' ' + (s % 60) + ' s' : '');
    const h = Math.floor(m / 60);
    if (h < 48) return h + ' h' + (m % 60 ? ' ' + String(m % 60).padStart(2, '0') : '');
    return Math.floor(h / 24) + ' j';
  }
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function chip(level) {
    const map = { CRITIQUE: 'crit', ATTENTION: 'warn', INFO: '' };
    const c = el('span', 'chip ' + (map[level] ?? 'warn'), (level || '?').toLowerCase());
    return c;
  }
  function levelClass(level) { return level === 'CRITIQUE' ? 'crit' : level === 'ATTENTION' ? 'warn' : ''; }

  async function api(path) {
    const res = await fetch(path, { headers: { Accept: 'application/json' }, cache: 'no-store' });
    let body = null;
    try { body = await res.json(); } catch (_) { /* corps non JSON */ }
    if (!res.ok) throw new Error((body && body.error) || ('erreur ' + res.status));
    return body;
  }

  // Valeur numérique qui glisse vers sa nouvelle valeur au lieu de sauter.
  const tweens = new WeakMap();
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  function setNumber(node, value, digits) {
    if (!node) return;
    if (value == null || !isFinite(value)) { tweens.delete(node); node.textContent = '—'; return; }
    const prev = tweens.get(node);
    const from = prev ? prev.current : null;
    if (from == null || reduceMotion.matches || document.hidden) {
      node.textContent = num(value, digits); tweens.set(node, { current: value }); return;
    }
    const start = performance.now(), dur = 600, state = { current: from };
    tweens.set(node, state);
    const step = now => {
      if (tweens.get(node) !== state) return;
      const k = Math.min(1, (now - start) / dur), e = 1 - Math.pow(1 - k, 3);
      state.current = from + (value - from) * e;
      node.textContent = num(state.current, digits);
      if (k < 1) requestAnimationFrame(step); else state.current = value;
    };
    requestAnimationFrame(step);
  }

  // ================================================================ thème
  const THEMES = ['auto', 'light', 'dark'];
  const THEME_LABEL = { auto: 'automatique', light: 'clair', dark: 'sombre' };
  let theme = 'auto';
  try { theme = localStorage.getItem('voltaneum.theme') || 'auto'; } catch (_) { /* stockage indisponible */ }
  function applyTheme() {
    if (theme === 'auto') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', theme);
    $('#theme-btn').title = 'Thème : ' + THEME_LABEL[theme];
    redrawCharts();
  }
  $('#theme-btn').addEventListener('click', () => {
    theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
    try { localStorage.setItem('voltaneum.theme', theme); } catch (_) { /* ignoré */ }
    applyTheme();
  });
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => redrawCharts());
  const allCharts = [];
  function redrawCharts() { requestAnimationFrame(() => allCharts.forEach(c => c.draw())); }
  function chart(id, opts) { const c = new window.TimeChart(document.getElementById(id), opts); allCharts.push(c); return c; }

  // ================================================================ état global
  const state = {
    snapshot: null, clockOffset: 0, thresholds: null, linkOk: false,
    lastAlertId: null, unseenAlerts: 0, view: null,
  };

  // ================================================================ navigation
  const VIEWS = ['supervision', 'historique', 'alertes', 'pid', 'modeles', 'reglages'];
  const loaders = {};
  function route() {
    const name = VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'supervision';
    if (state.view === name) return;
    state.view = name;
    $$('.view').forEach(v => { v.hidden = v.dataset.view !== name; });
    $$('.tabs a').forEach(a => { if (a.dataset.tab === name) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); });
    if (name === 'alertes') { state.unseenAlerts = 0; updateBadge(); }
    document.title = 'Voltaneum · ' + $('.tabs a[data-tab="' + name + '"]').firstChild.textContent.trim();
    (loaders[name] || (() => {}))();
    requestAnimationFrame(() => allCharts.forEach(c => c.resize()));
  }
  window.addEventListener('hashchange', route);

  function segmented(group, onPick) {
    group.addEventListener('click', e => {
      const b = e.target.closest('button'); if (!b) return;
      $$('button', group).forEach(o => o.setAttribute('aria-pressed', String(o === b)));
      onPick(b);
    });
    return () => $('button[aria-pressed="true"]', group);
  }

  // ================================================================ horloge, fraîcheur
  function tick() {
    $('#clock').textContent = fmtTime.format(new Date());
    updateFreshness();
  }
  function setPill(id, cls, text, live) {
    const p = $(id);
    p.className = 'pill ' + (cls || '') + (live ? ' live' : '');
    $('.txt', p).textContent = text;
  }
  function updateFreshness() {
    const s = state.snapshot, th = state.thresholds;
    const banner = $('#banner');
    if (!s) return;
    if (s.empty) {
      setPill('#pill-data', 'warn', 'Base vide');
      banner.hidden = false; banner.className = 'banner';
      banner.textContent = 'La base ne contient encore aucune mesure. Démarrez Voltaneum : ./voltaneum';
      return;
    }
    const age = Date.now() - state.clockOffset - s.ts;
    const interval = th.snapshot_interval_seconds * 1000, stale = th.stale_after_seconds * 1000;
    const cls = age <= interval * 3 ? 'ok' : age <= stale ? 'warn' : 'crit';
    setPill('#pill-data', cls, 'Mesure il y a ' + ago(age));
    $('#syno-age').textContent = 'dernière mesure : ' + fmtTime.format(new Date(s.ts));
    const isStale = age > stale;
    $('[data-view="supervision"]').classList.toggle('stale', isStale);
    if (isStale) {
      banner.hidden = false; banner.className = 'banner crit';
      banner.replaceChildren(el('strong', null, 'Mesures périmées. '),
        document.createTextNode('Aucune nouvelle mesure depuis ' + ago(age) + ' (dernière : ' + fmtDateTime.format(new Date(s.ts)) +
          '). Les valeurs affichées ne sont plus actuelles. Vérifier : ./voltaneum status'));
    } else if (!state.dbError) {
      banner.hidden = true;
    }
  }

  // ================================================================ noms des sondes
  // Les noms viennent du serveur (config.SENSOR_LABELS, réglables dans .env) : synoptique, tuile
  // PT02, courbes et légendes de l'historique affichent tous le même nom.
  let labelsApplied = '';
  function applyLabels(labels) {
    if (!labels) return;
    const signature = JSON.stringify(labels);
    if (signature === labelsApplied) return;
    labelsApplied = signature;
    for (const [key, l] of Object.entries(labels)) {
      LABELS[key] = l.long;
      const tag = $('.tag[data-key="' + key + '"] .lbl-pt'); if (tag) tag.textContent = l.short;
      const chipText = $('.chip-label[data-key="' + key + '"]'); if (chipText) chipText.textContent = l.long;
    }
    if (labels.pt02) $('#lbl-pt02').textContent = labels.pt02.long;
    for (const c of allCharts) for (const serie of c.o.series) if (labels[serie.key]) serie.label = labels[serie.key].long;
  }

  // ================================================================ supervision : instantané
  function renderSnapshot(s) {
    state.snapshot = s;
    state.thresholds = s.thresholds;
    state.clockOffset = Date.now() - s.server_time;
    state.dbError = null;
    updateFreshness();
    if (s.empty) return;
    const v = s.values, th = s.thresholds, cool = s.cooling;

    // Tuiles
    const tile = key => $('.tile[data-key="' + key + '"]');
    const pumpNoFlow = v.freq_pompe != null && v.ht01 != null && v.freq_pompe > th.pump_running_min_hz && v.ht01 < th.flow_min_m3h;

    const t02 = tile('pt02');
    setNumber($('.v', t02), v.pt02, 1);
    t02.className = 'tile' + (v.pt02 == null ? ' absent' : v.pt02 > th.critical_temp ? ' crit' : v.pt02 >= th.critical_temp - 5 ? ' warn' : '');
    const trend = $('.trend', t02);
    if (s.pt02_trend_10min != null) {
      trend.textContent = (s.pt02_trend_10min > 0.05 ? '↗ ' : s.pt02_trend_10min < -0.05 ? '↘ ' : '→ ') + signed(s.pt02_trend_10min) + ' °C / 10 min';
      trend.className = 'trend num ' + (s.pt02_trend_10min > 0.3 ? 'up' : s.pt02_trend_10min < -0.3 ? 'down' : '');
    } else trend.textContent = '';
    $('.hint', t02).textContent = v.pt02 == null ? 'mesure absente' : 'cible ' + num(th.target_temp) + ' °C';

    const tp = tile('pred');
    const pred = s.predictions && s.predictions.models[s.predictions.best_model];
    if (pred && pred.value != null) {
      setNumber($('.v', tp), pred.value, 1);
      $('.hz', tp).textContent = '+' + fmtHorizon(pred.horizon_seconds);
      const dis = s.predictions.disagreement;
      const hint = $('.hint', tp);
      hint.replaceChildren(document.createTextNode((MODEL_LABELS[s.predictions.best_model] || s.predictions.best_model) + ' '));
      if (dis != null) {
        const c = el('span', 'chip ' + (dis > s.predictions.disagreement_threshold ? 'warn' : 'ok'),
          dis > s.predictions.disagreement_threshold ? 'désaccord ' + num(dis) + ' °C' : 'modèles d\'accord');
        hint.append(c);
      }
      tp.className = 'tile tile-pred' + (pred.value > th.critical_temp ? ' crit' : '');
    } else {
      setNumber($('.v', tp), null);
      $('.hz', tp).textContent = '';
      $('.hint', tp).textContent = 'aucune prédiction récente (aucun modèle installé ?)';
      tp.className = 'tile tile-pred absent';
    }

    const tf = tile('freq_pompe');
    setNumber($('.v', tf), v.freq_pompe, 1);
    const fmax = (th.valid_ranges.freq_pompe || [0, 50])[1];
    const fm = $('.meter > span', tf);
    fm.style.width = v.freq_pompe == null ? '0' : Math.min(100, v.freq_pompe / fmax * 100) + '%';
    // Fréquence de la POMPE : elle n'est pas comparable à la fréquence minimale conseillée au
    // ventilateur du dry cooler (autre moteur, non mesuré).
    fm.className = '';
    tf.className = 'tile' + (v.freq_pompe == null ? ' absent' : '');

    const th01 = tile('ht01');
    setNumber($('.v', th01), v.ht01, 2);
    th01.className = 'tile' + (v.ht01 == null ? ' absent' : pumpNoFlow ? ' crit' : '');
    $('.hint', th01).textContent = v.ht01 == null ? 'mesure absente' : pumpNoFlow ? 'pompe en marche sans débit' : 'minimum ' + num(th.flow_min_m3h, 1) + ' m³/h en marche';

    const te = tile('temp_outdoor');
    setNumber($('.v', te), v.temp_outdoor, 1);
    te.className = 'tile' + (v.temp_outdoor == null ? ' absent' : '');
    const src = s.temp_outdoor_source;
    const teHint = $('.hint', te);
    teHint.replaceChildren();
    if (v.temp_outdoor == null) teHint.textContent = 'ni capteur ni météo';
    else if (src === 'weather') { teHint.append(el('span', 'chip warn', 'météo'), document.createTextNode(' repli API, pas de capteur')); }
    else if (src === 'sensor') { teHint.append(el('span', 'chip ok', 'capteur'), document.createTextNode(' mesure locale')); }

    // Puissance dissipée estimée (débit × écart de température)
    const tpw = tile('thermal_power_kw');
    setNumber($('.v', tpw), v.thermal_power_kw, 1);
    tpw.className = 'tile' + (v.thermal_power_kw == null ? ' absent' : '');
    const tpConf = th.thermal_power || {};
    const tpHint = $('.hint', tpw); tpHint.replaceChildren();
    if (v.thermal_power_kw == null) tpHint.textContent = 'débit ou températures absents';
    else {
      tpHint.append(tpConf.confirmed ? el('span', 'chip ok', 'confirmé') : el('span', 'chip warn', 'estimation'),
        document.createTextNode(' ' + (tpConf.hot || 'pt03').toUpperCase() + ' − ' + (tpConf.cold || 'pt04').toUpperCase() + ' × débit'));
    }
    setPill('#pill-ext', src === 'sensor' ? 'ok' : src === 'weather' ? 'warn' : '', src === 'sensor' ? 'Air ext. : capteur' : src === 'weather' ? 'Air ext. : météo' : 'Air ext. : absent');
    $('#pill-ext').hidden = false;

    applyLabels(th.labels);

    // Synoptique
    const syno = $('#syno');
    for (const key of Object.keys(LABELS)) {
      const g = $('.tag[data-key="' + key + '"]', syno); if (!g) continue;
      setNumber($('.v', g), v[key], DIGITS[key]);
      let cls = 'tag';
      if (v[key] == null) cls += ' absent';
      else if (key === 'pt02' && v.pt02 > th.critical_temp) cls += ' crit';
      else if (key === 'pt01' && v.pt01 > (th.dielectric_critical_temp ?? th.critical_temp)) cls += ' crit';
      else if (key === 'ht01' && pumpNoFlow) cls += ' crit';
      g.setAttribute('class', cls);
    }
    $('#syno-ext-lbl').textContent = src === 'weather' ? 'Air extérieur (météo)' : src === 'sensor' ? 'Air extérieur (capteur)' : 'Air extérieur';
    // L'état du ventilateur du dry cooler n'est pas mesuré (la fréquence lue est celle de la
    // pompe) : l'hélice reste immobile plutôt que de suggérer un état inventé.
    syno.classList.add('fan-off');
    syno.classList.toggle('no-flow', !(v.ht01 != null && v.ht01 >= th.flow_min_m3h));

    // Conditions de refroidissement
    $$('#bands .band').forEach(b => b.classList.toggle('on', !!cool && b.dataset.band === cool.outdoor_band));
    const dd = k => $('#cooling dd[data-k="' + k + '"]');
    if (cool) {
      dd('target_temp').textContent = num(cool.target_temp) + ' °C';
      dd('fan_on_above_temp').textContent = num(cool.fan_on_above_temp) + ' °C';
      dd('fan_off_below_temp').textContent = num(cool.fan_off_below_temp) + ' °C';
      dd('fan_min_freq_hz').textContent = cool.fan_min_freq_hz == null ? 'non conseillée' : num(cool.fan_min_freq_hz, 0) + ' Hz';
      dd('critical_temp').textContent = num(cool.critical_temp) + ' °C';
    } else {
      $$('#cooling dd').forEach(d => { d.textContent = '—'; });
    }

    renderPid(s);
    renderMl(s);
    onSnapshotForPid(s);
    supChart.updateThresholds(s);
    supChart.appendSnapshot(s);
  }

  // ================================================================ supervision : régulation PID
  const MODE_INFO = {
    pid: { label: 'PID', cls: 'ok' },
    feedforward: { label: 'PID + anticipation', cls: 'pred' },
    safety_bypass: { label: 'Sécurité : 100 %', cls: 'crit' },
  };
  const pct = v => num(v, 1).replace(/,0$/, '') + ' %';
  function freqPercent(s) {
    const f = s.values && s.values.freq_pompe;
    const fmax = (s.thresholds.valid_ranges.freq_pompe || [0, 50])[1];
    return f == null ? null : Math.max(0, Math.min(100, f / fmax * 100));
  }
  function renderPid(s) {
    const c = s.control, empty = $('#pid-empty'), body = $('#pid-body');
    if (!c) {
      empty.hidden = false; body.hidden = true;
      empty.className = 'callout warn';
      empty.textContent = 'Aucun calcul PID enregistré pour l\'instant : la régulation calcule une consigne chaque minute ' +
        'dès que PT02 est mesurée (vérifier ./voltaneum status).';
      $('#pid-age').textContent = '';
      return;
    }
    empty.hidden = true; body.hidden = false;
    const stale = c.age_seconds > c.interval_seconds * 3;
    body.classList.toggle('stale-block', stale);
    $('#pid-age').textContent = stale ? 'dernier calcul il y a ' + ago(c.age_seconds * 1000) + ' · Voltaneum arrêté ?' : 'calculé à ' + fmtTime.format(new Date(c.ts));

    const sp = $('#pid-sp');
    setNumber(sp, c.setpoint, 1);
    sp.parentElement.classList.toggle('crit', c.mode === 'safety_bypass');
    const mi = MODE_INFO[c.mode] || { label: c.mode, cls: '' };
    const mode = $('#pid-mode'); mode.textContent = mi.label; mode.className = 'chip ' + mi.cls;
    const g = $('#pid-gauge');
    g.style.width = Math.max(0, Math.min(100, c.setpoint)) + '%';
    g.className = c.mode === 'safety_bypass' ? 'crit' : c.mode === 'feedforward' ? 'ff' : '';
    const fp = freqPercent(s), mark = $('#pid-freq-mark');
    mark.hidden = fp == null;
    if (fp != null) { mark.style.left = fp + '%'; mark.title = 'Fréquence réelle : ' + num(s.values.freq_pompe, 1) + ' Hz'; }

    // Explication en clair
    const why = $('#pid-why'); why.replaceChildren();
    const add = (text, bold) => why.append(bold ? el('b', null, text) : document.createTextNode(text));
    if (c.mode === 'safety_bypass') {
      add('PT02 est à '); add(num(c.t_oil) + ' °C', true);
      add(', au-dessus du seuil critique de ' + num(s.thresholds.critical_temp) + ' °C : refroidissement maximal demandé, sans calcul PID.');
    } else {
      const e = c.error;
      add('PT02 (liquide diélectrique) est à '); add(num(c.t_oil) + ' °C', true);
      add(Math.abs(e) < 0.05 ? ', exactement à la cible : ' : ', soit ' + num(Math.abs(e)) + ' °C ' + (e > 0 ? 'au-dessus' : 'en dessous') + ' de la cible de ' + num(c.t_target) + ' °C. ');
      add('Le PID suggère '); add(pct(c.setpoint), true); add(' de refroidissement');
      const reasons = [];
      if (c.feedforward_prediction > 0) reasons.push('+' + pct(c.feedforward_prediction) + ' d\'anticipation, car le modèle prévoit ' + num(c.t_predicted) + ' °C (plus de ' + num(c.feedforward_thresholds.predicted_overshoot) + ' °C au-dessus de la cible)');
      if (c.feedforward_outdoor > 0) reasons.push('+' + pct(c.feedforward_outdoor) + ' car l\'air extérieur dépasse ' + num(c.feedforward_thresholds.outdoor_hot, 0) + ' °C');
      add(reasons.length ? ', dont ' + reasons.join(' et ') + '.' : '.');
      if (c.raw_output != null && c.raw_output > 100.05) add(' Le calcul brut donne ' + pct(c.raw_output) + ', ramené au maximum de 100 %.');
      if (c.raw_output != null && c.raw_output < -0.05) add(' Le calcul brut est négatif (' + pct(c.raw_output) + ') : le PID ne sait que refroidir, la consigne reste à 0 %.');
      if (c.i_term != null && c.i_term >= 99.9) add(' Le terme intégral est à son maximum : PT02 est restée longtemps au-dessus de la cible.');
    }

    // Décomposition terme par terme
    const terms = $('#pid-terms'); terms.replaceChildren();
    if (c.mode === 'safety_bypass' || c.p_term == null) {
      terms.hidden = true;
    } else {
      terms.hidden = false;
      const rows = [
        ['Proportionnel', 'Kp × écart', c.p_term, ''],
        ['Intégral', 'Ki × écart cumulé', c.i_term, ''],
        ['Dérivé', 'Kd × variation', c.d_term, ''],
        ['Anticipation ML', 'prédiction', c.feedforward_prediction, 'ff'],
        ['Anticipation météo', 'air chaud', c.feedforward_outdoor, 'ff'],
      ];
      const scale = Math.max(100, ...rows.map(r => Math.abs(r[2] || 0)), Math.abs(c.raw_output || 0));
      const addRow = (name, hint, value, kind, total) => {
        const row = el('div', 'term' + (total ? ' total' : '') + (!total && !value ? ' off' : ''));
        const n = el('span', 'name', name); if (hint) n.append(el('small', null, hint));
        const bar = el('span', 'bar'); const fill = el('span', (value < 0 ? 'neg ' : '') + kind);
        const w = Math.abs(value || 0) / scale * 50;
        fill.style.left = (value < 0 ? 50 - w : 50) + '%'; fill.style.width = w + '%';
        bar.append(fill);
        const v = Math.abs(value || 0) < 0.05 ? 0 : value;
        row.append(n, bar, el('span', 'val', (v > 0 ? '+' : v < 0 ? '−' : '') + pct(Math.abs(v))));
        terms.append(row);
      };
      for (const r of rows) addRow(...r);
      addRow('Somme', c.raw_output > 100.05 || c.raw_output < -0.05 ? 'bornée à ' + pct(c.setpoint) : 'consigne', c.raw_output, '', true);
    }

    // Entrées du calcul
    const dl = $('#pid-inputs'); dl.replaceChildren();
    const kv = (k, v) => dl.append(el('dt', null, k), el('dd', null, v));
    kv('Écart PT02 − cible', c.error == null ? '—' : signed(c.error, 2) + ' °C');
    kv('Prédiction utilisée', c.t_predicted == null ? 'aucune (PID seul)' : num(c.t_predicted, 2) + ' °C');
    kv('Air extérieur', c.t_outdoor == null ? 'inconnu' : num(c.t_outdoor) + ' °C' + (c.t_outdoor_source === 'weather' ? ' (météo)' : c.t_outdoor_source === 'sensor' ? ' (capteur)' : ''));
    kv('Fréquence réelle pompe', s.values.freq_pompe == null ? '—' : num(s.values.freq_pompe, 1) + ' Hz (' + pct(fp) + ')');
    kv('Gains Kp / Ki / Kd', [c.gains.kp, c.gains.ki, c.gains.kd].map(v => num(v, v % 1 ? 2 : 0)).join(' / '));
    if (!c.published) kv('Publication MQTT', 'désactivée');
  }

  // ================================================================ supervision : prédiction ML
  function renderMl(s) {
    const p = s.predictions, empty = $('#ml-empty'), body = $('#ml-body');
    if (!p) {
      empty.hidden = false; body.hidden = true; empty.className = 'callout warn';
      empty.textContent = 'Aucune prédiction dans les 5 dernières minutes : aucun modèle validé n\'est installé (onglet Modèles), ou des mesures manquent.';
      $('#ml-horizon').textContent = '';
      return;
    }
    empty.hidden = true; body.hidden = false;
    const models = Object.entries(p.models);
    const any = models[0][1];
    $('#ml-horizon').textContent = 'PT02 dans ' + fmtHorizon(any.horizon_seconds);
    const now = s.values.pt02;
    const live = {};
    for (const r of (state.models && state.models.live) || []) if (r.horizon_seconds === any.horizon_seconds) live[r.model] = r;
    const box = $('#ml-models'); box.replaceChildren();
    for (const [name, m] of models.sort((a, b) => (b[0] === p.best_model) - (a[0] === p.best_model))) {
      const card = el('div', 'ml-model' + (name === p.best_model ? ' best' : ''));
      const n = el('span', 'name', MODEL_LABELS[name] || name);
      if (name === p.best_model) n.append(el('span', 'chip pred', 'retenu'));
      const v = el('span', 'v'); v.append(document.createTextNode(num(m.value, 2)), el('u', null, '°C'));
      const delta = now != null && m.value != null ? m.value - now : null;
      const sub = el('span', 'sub', (delta == null ? '' : (Math.abs(delta) < 0.05 ? 'stable' : signed(delta, 2) + ' °C par rapport à maintenant') + ' · ') +
        'à ' + fmtTime.format(new Date(m.target_ts)).slice(0, 5) + (live[name] ? ' · erreur récente ' + num(live[name].rmse, 2) + ' °C' : ''));
      card.append(n, v, sub); box.append(card);
    }
    const dl = $('#ml-info'); dl.replaceChildren();
    const kv = (k, node) => dl.append(el('dt', null, k), typeof node === 'string' ? el('dd', null, node) : (() => { const d = el('dd'); d.append(node); return d; })());
    if (p.disagreement != null) {
      const bad = p.disagreement > p.disagreement_threshold;
      kv('Écart entre les modèles', el('span', 'chip ' + (bad ? 'warn' : 'ok'), num(p.disagreement, 2) + ' °C' + (bad ? ' · désaccord' : ' · accord')));
    }
    const c = s.control;
    if (c && s.thresholds) {
      const trigger = c.t_target + c.feedforward_thresholds.predicted_overshoot;
      kv('Anticipation du PID', c.feedforward_prediction > 0 ? 'déclenchée (+' + pct(c.feedforward_prediction) + ')' : 'non (seuil ' + num(trigger) + ' °C)');
    }
    kv('Dernière prédiction', fmtTime.format(new Date(any.made_at)));
  }

  // ================================================================ supervision : courbes
  const supGroup = [];
  const supTemp = chart('chart-sup', {
    label: 'Tendance de PT02', unit: '°C', digits: 1, minSpan: 4, now: true, group: supGroup,
    series: [
      { key: 'pt02', label: 'PT02', color: '--s1', width: 2, area: true, band: true, endpoint: true },
      { key: 'pred_hist', label: 'Prédit', color: '--pred', width: 1.4, dash: [2, 3] },
      { key: 'forecast', label: 'Prévision', color: '--pred', width: 2, dash: [6, 4], endpoint: true },
    ],
  });
  const supFreq = chart('chart-sup-freq', {
    label: 'Fréquence pompe', unit: 'Hz', digits: 1, minSpan: 10, zeroBased: true, now: true, group: supGroup,
    series: [{ key: 'freq_pompe', label: 'Fréquence', color: '--s5', width: 1.5, area: true }],
  });
  const supPid = chart('chart-sup-pid', {
    label: 'Consigne PID', unit: '%', digits: 1, minSpan: 10, zeroBased: true, now: true, group: supGroup,
    yInclude: [0, 100],
    series: [
      { key: 'freq_pct', label: 'Fréquence réelle', color: '--s5', width: 1.2, dash: [3, 3], unit: '%' },
      { key: 'setpoint', label: 'Consigne PID', color: '--s2', width: 1.8, area: true, endpoint: true, unit: '%' },
    ],
    emptyText: 'Aucune consigne PID sur cette période',
  });
  const supChart = {
    minutes: 360, data: null, loading: false,
    async load() {
      const box = $('#chart-sup');
      box.classList.add('loading');
      try {
        const points = Math.min(1500, Math.max(300, Math.round(box.clientWidth * 1.2)));
        const d = await api('/api/series?minutes=' + this.minutes + '&points=' + points);
        this.data = d;
        this.render();
      } catch (err) {
        showDbError(err);
      } finally { box.classList.remove('loading'); }
    },
    series() {
      const d = this.data, s = state.snapshot;
      const best = d.predictions && (d.predictions.best_model || Object.keys(d.predictions.models)[0]);
      const ph = best && d.predictions.models[best];
      const out = {
        pt02: { t: d.t, v: d.series.pt02 },
        freq_pompe: { t: d.t, v: d.series.freq_pompe },
        pred_hist: ph ? { t: ph.t, v: ph.v } : { t: [], v: [] },
        forecast: { t: [], v: [] },
      };
      const p = s && !s.empty && s.predictions && s.predictions.models[s.predictions.best_model];
      if (p && p.value != null && s.values.pt02 != null) out.forecast = { t: [s.ts, p.target_ts], v: [s.values.pt02, p.value] };
      return out;
    },
    xRange() {
      const now = Date.now() - state.clockOffset;
      const s = state.snapshot;
      const p = s && !s.empty && s.predictions && s.predictions.models[s.predictions.best_model];
      const ahead = p ? Math.max(0, p.target_ts - now) + 60e3 : 60e3;
      return [now - this.minutes * 60e3, now + Math.min(ahead, this.minutes * 60e3 * 0.15)];
    },
    render() {
      if (!this.data) return;
      const sr = this.series(), [x0, x1] = this.xRange();
      const gap = this.data.gap_ms;
      supTemp.setData({ xMin: x0, xMax: x1, gapMs: gap, series: sr,
        band: this.data.bucket_seconds > state.thresholds.snapshot_interval_seconds ? { t: this.data.t, lo: this.data.pt02_min, hi: this.data.pt02_max } : null });
      supFreq.setData({ xMin: x0, xMax: x1, gapMs: gap, series: { freq_pompe: sr.freq_pompe } });
      const ctl = this.data.control || { t: [], setpoint: [] };
      const fmax = (state.thresholds.valid_ranges.freq_pompe || [0, 50])[1];
      supPid.setData({ xMin: x0, xMax: x1, gapMs: Math.max(gap, ctl.gap_ms || 0), series: {
        setpoint: { t: ctl.t, v: ctl.setpoint },
        freq_pct: { t: this.data.t, v: this.data.series.freq_pompe.map(v => v == null ? null : Math.min(100, v / fmax * 100)) },
      } });
    },
    updateThresholds(s) {
      const th = s.thresholds, c = s.cooling;
      const list = [{ value: th.critical_temp, color: '--crit', label: 'critique ' + num(th.critical_temp, 0) + ' °C', dash: [], width: 1.5, offscale: true }];
      if (c) {
        list.push({ value: c.fan_on_above_temp, color: '--ok', label: 'marche ' + num(c.fan_on_above_temp, 1) });
        list.push({ value: c.fan_off_below_temp, color: '--ok', label: 'arrêt ' + num(c.fan_off_below_temp, 1), below: true });
      }
      supTemp.o.thresholds = list;
      supTemp.o.yInclude = c ? [c.fan_off_below_temp, c.fan_on_above_temp] : [th.target_temp];
      supFreq.o.yInclude = [0, (th.valid_ranges.freq_pompe || [0, 50])[1]];
      if (c && c.fan_min_freq_hz != null) supFreq.o.thresholds = [{ value: c.fan_min_freq_hz, color: '--ok', label: 'min. conseillé ' + num(c.fan_min_freq_hz, 0) + ' Hz' }];
    },
    appendSnapshot(s) {
      const d = this.data;
      if (!d) return;
      // Courtes périodes : la nouvelle mesure est ajoutée directement à la courbe (glissement
      // fluide). Longues périodes (moyennes par tranche) : rechargement périodique.
      if (d.bucket_seconds <= state.thresholds.snapshot_interval_seconds * 2) {
        if (!d.t.length || s.ts > d.t[d.t.length - 1]) {
          d.t.push(s.ts);
          for (const k of Object.keys(d.series)) d.series[k].push(s.values[k] ?? null);
          d.pt02_min.push(s.values.pt02 ?? null); d.pt02_max.push(s.values.pt02 ?? null);
          const ctl = d.control;
          if (ctl && s.control && (!ctl.t.length || s.control.ts > ctl.t[ctl.t.length - 1])) {
            ctl.t.push(s.control.ts); ctl.setpoint.push(s.control.setpoint); ctl.t_predicted.push(s.control.t_predicted);
          }
          const cut = d.t.findIndex(t => t >= Date.now() - state.clockOffset - this.minutes * 60e3);
          if (cut > 0) {
            d.t.splice(0, cut);
            for (const k of Object.keys(d.series)) d.series[k].splice(0, cut);
            d.pt02_min.splice(0, cut); d.pt02_max.splice(0, cut);
          }
        }
        this.render();
      } else if (Date.now() - (this.loadedAt || 0) > 5 * 60e3) {
        this.loadedAt = Date.now();
        this.load();
      } else this.render();
    },
  };
  const supRange = segmented($('#sup-range'), b => { supChart.minutes = +b.dataset.min; supChart.loadedAt = Date.now(); supChart.load(); });
  supChart.minutes = +supRange().dataset.min;
  legend('#legend-sup', [
    ['--s1', 'PT02 mesurée', ''], ['--pred', 'Prévision du modèle', 'dash'], ['--pred', 'Prédictions passées, à leur échéance', 'dotted'],
    ['--ok', 'Seuils marche / arrêt ventilateur', 'dotted'], ['--crit', 'Seuil critique', ''], ['--s5', 'Fréquence pompe (Hz)', ''], ['--s2', 'Consigne PID (%)', ''], ['--s5', 'Fréquence en % de la plage', 'dash'],
  ]);
  function legend(sel, items) {
    const box = $(sel); box.replaceChildren();
    for (const [color, label, style] of items) {
      const span = el('span'); const i = el('i', style); i.style.color = `var(${color})`;
      span.append(i, document.createTextNode(label)); box.append(span);
    }
  }

  // ================================================================ supervision : alertes et système
  async function loadRecentAlerts() {
    try {
      const d = await api('/api/alerts?limit=6');
      const ul = $('#alerts-recent'); ul.replaceChildren();
      if (!d.items.length) { ul.append(el('li', 'empty', 'Aucune alerte enregistrée.')); }
      for (const a of d.items) {
        const li = el('li');
        if (state.lastAlertId != null && a.id > state.lastAlertId) li.classList.add('fresh');
        const sev = chip(a.level); sev.classList.add('sev');
        li.append(sev, el('span', 'what', alertText(a)), el('span', 'when num', (a.ts ? fmtShort.format(new Date(a.ts)) : '—') + (a.email_sent ? ' · e-mail envoyé' : '')));
        ul.append(li);
      }
      const top = d.items[0] ? d.items[0].id : 0;
      if (state.lastAlertId != null && top > state.lastAlertId) {
        for (const a of d.items.filter(x => x.id > state.lastAlertId).reverse()) toast(a);
        if (state.view !== 'alertes') { state.unseenAlerts += d.items.filter(x => x.id > state.lastAlertId).length; updateBadge(); }
        if (state.view === 'alertes') loadAlerts();
      }
      state.lastAlertId = top;
    } catch (err) { /* l'erreur de base est déjà affichée par ailleurs */ }
  }
  function alertText(a) {
    const msg = (a.message || '').split('\n')[0];
    return msg || a.rule;
  }
  function updateBadge() {
    const b = $('#alert-badge');
    b.hidden = !state.unseenAlerts;
    b.textContent = state.unseenAlerts > 99 ? '99+' : String(state.unseenAlerts);
  }
  function toast(a) {
    const t = el('div', 'toast ' + levelClass(a.level));
    t.append(el('b', null, 'Alerte ' + (a.level || '').toLowerCase()), document.createTextNode(alertText(a)));
    t.addEventListener('click', () => { location.hash = '#alertes'; close(); });
    $('#toasts').append(t);
    const close = () => { t.classList.add('out'); setTimeout(() => t.remove(), 260); };
    setTimeout(close, a.level === 'CRITIQUE' ? 20000 : 8000);
  }

  async function loadHealth() {
    try {
      const [sys, models] = await Promise.all([api('/api/system'), api('/api/models')]);
      state.models = models;
      loadRetrain();
      if (state.snapshot && !state.snapshot.empty) renderMl(state.snapshot);
      const h = $('#health');
      const k = key => $('[data-k="' + key + '"]', h);
      const ratio = sys.db_bytes / sys.storage_limit_bytes;
      k('db').textContent = bytes(sys.db_bytes) + ' · ' + sys.rows.toLocaleString('fr-FR') + ' lignes';
      const m = k('db-meter'); m.style.width = Math.max(1, Math.min(100, ratio * 100)) + '%';
      m.className = ratio >= sys.storage_warning_ratio ? 'crit' : ratio >= sys.storage_warning_ratio * 0.8 ? 'warn' : '';
      k('disk').textContent = sys.disk ? bytes(sys.disk.free) + ' sur ' + bytes(sys.disk.total) : '—';
      const comp = sys.completeness_last_hour;
      k('completeness').replaceChildren(document.createTextNode(sys.rows_last_hour + ' '), el('span', 'chip ' + (comp >= .95 ? 'ok' : comp >= .7 ? 'warn' : 'crit'), Math.round(comp * 100) + ' %'));
      k('backup').textContent = sys.last_backup ? fmtShort.format(new Date(sys.last_backup.ts)) : 'aucune';
      const tr = models.training;
      const md = k('model');
      md.replaceChildren();
      if (!tr || !tr.status) md.append(el('span', 'chip warn', 'non entraîné'));
      else if (tr.status !== 'ok') md.append(el('span', 'chip warn', tr.status));
      else {
        md.append(document.createTextNode((MODEL_LABELS[tr.best_model] || tr.best_model) + ' '));
        md.append(el('span', 'chip ' + (tr.beats_persistence ? 'ok' : 'warn'), tr.beats_persistence ? 'bat la persistance' : 'ne bat pas la persistance'));
      }
    } catch (err) { /* affiché ailleurs */ }
  }

  function showDbError(err) {
    state.dbError = err;
    const banner = $('#banner');
    banner.hidden = false; banner.className = 'banner crit';
    banner.replaceChildren(el('strong', null, 'Données indisponibles. '), document.createTextNode(err.message));
  }

  loaders.supervision = () => { supChart.load(); loadRecentAlerts(); loadHealth(); };

  // ================================================================ temps réel (SSE)
  let source = null, retryTimer = null;
  function connect() {
    if (source) source.close();
    source = new EventSource('/api/stream');
    source.addEventListener('open', () => { state.linkOk = true; setPill('#pill-link', 'ok', 'Temps réel', true); });
    source.addEventListener('snapshot', e => { try { renderSnapshot(JSON.parse(e.data)); } catch (err) { console.error(err); } });
    source.addEventListener('alert', () => { loadRecentAlerts(); });
    source.addEventListener('unavailable', e => { try { showDbError(new Error(JSON.parse(e.data).error)); } catch (_) { /* ignoré */ } });
    source.addEventListener('error', () => {
      state.linkOk = false;
      setPill('#pill-link', 'warn', 'Reconnexion…');
      if (source.readyState === EventSource.CLOSED) { clearTimeout(retryTimer); retryTimer = setTimeout(connect, 5000); }
    });
  }

  // ================================================================ historique
  const histGroup = [];
  const TEMP_KEYS = ['pt01', 'pt02', 'pt03', 'pt04', 'temp_outdoor'];
  const histTemp = chart('chart-hist-temp', {
    label: 'Températures', unit: '°C', digits: 1, minSpan: 4, group: histGroup,
    series: TEMP_KEYS.map(k => ({ key: k, label: LABELS[k], color: COLORS[k], width: k === 'pt02' ? 2 : 1.4, band: k === 'pt02', dash: k === 'temp_outdoor' ? [4, 3] : null })),
  });
  const histFlow = chart('chart-hist-flow', {
    label: 'Fréquence et débit', unit: 'Hz', rightUnit: 'm³/h', digits: 1, rightDigits: 2, minSpan: 10, rightMinSpan: 1,
    zeroBased: true, rightZeroBased: true, group: histGroup,
    series: [
      { key: 'freq_pompe', label: LABELS.freq_pompe, color: COLORS.freq_pompe, width: 1.5, area: true },
      { key: 'ht01', label: LABELS.ht01, color: COLORS.ht01, width: 1.5, axis: 'right' },
    ],
  });
  const histPower = chart('chart-hist-power', {
    label: 'Puissance dissipée estimée', unit: 'kW', digits: 1, minSpan: 2, group: histGroup,
    series: [
      { key: 'thermal_power_kw', label: LABELS.thermal_power_kw, color: COLORS.thermal_power_kw, width: 1.6, area: true },
    ],
    emptyText: 'Pas encore de puissance enregistrée sur cette période',
  });
  const HIST_KEYS = [...TEMP_KEYS, 'freq_pompe', 'ht01', 'thermal_power_kw'];
  const chartOf = k => TEMP_KEYS.includes(k) ? histTemp : k === 'thermal_power_kw' ? histPower : histFlow;
  const histChips = $('#hist-series');
  for (const k of HIST_KEYS) {
    const lab = el('label'); lab.style.color = `var(${COLORS[k]})`;
    const input = el('input'); input.type = 'checkbox'; input.checked = true; input.id = 'hist-' + k; input.dataset.key = k;
    const txt = el('span', 'chip-label', LABELS[k]); txt.style.color = 'var(--fg)'; txt.dataset.key = k;
    lab.append(input, el('i'), txt);
    histChips.append(lab);
  }
  histChips.addEventListener('change', e => {
    const k = e.target.dataset.key; if (!k) return;
    chartOf(k).setHidden(k, !e.target.checked);
  });
  const toLocalInput = ms => { const d = new Date(ms - new Date(ms).getTimezoneOffset() * 60e3); return d.toISOString().slice(0, 16); };
  const fromLocalInput = s => new Date(s).getTime();
  let histMinutes = 1440;
  function setHistPreset(min) {
    const now = Date.now();
    $('#hist-from').value = toLocalInput(now - min * 60e3);
    $('#hist-to').value = toLocalInput(now);
  }
  segmented($('#hist-presets'), b => { histMinutes = +b.dataset.min; setHistPreset(histMinutes); loadHistory(); });
  $('#hist-form').addEventListener('submit', e => {
    e.preventDefault();
    $$('#hist-presets button').forEach(b => b.setAttribute('aria-pressed', 'false'));
    loadHistory();
  });
  // Exports CSV : un fichier par élément, sur la période choisie.
  const EXPORT_LIST = [
    ['mesures', 'Mesures', 'toutes les sondes, débit, fréquence, air extérieur, puissance'],
    ['consignes', 'Consignes PID', 'chaque cycle, ses termes et le message MQTT exact'],
    ['alertes', 'Alertes', 'niveau, règle, message, e-mail envoyé'],
    ['predictions', 'Prédictions', 'valeur prédite, mesure à l\'échéance, erreur'],
    ['entrainements', 'Ré-entraînements', 'verdict de chaque horizon, décisions'],
  ];
  function renderExports(start, end) {
    const box = $('#exports'); box.replaceChildren();
    for (const [name, label, hint] of EXPORT_LIST) {
      const a = el('a', 'export-btn'); a.href = '/api/export/' + name + '.csv?start=' + Math.round(start) + '&end=' + Math.round(end);
      a.setAttribute('download', '');
      a.append(el('b', null, '↓ ' + label), el('span', null, hint));
      box.append(a);
    }
    $('#exports-period').textContent = 'du ' + fmtDateTime.format(new Date(start)) + ' au ' + fmtDateTime.format(new Date(end));
  }

  async function loadHistory() {
    const start = fromLocalInput($('#hist-from').value), end = fromLocalInput($('#hist-to').value);
    if (!(end > start)) { $('#hist-foot').textContent = 'La date de fin doit suivre la date de début.'; return; }
    renderExports(start, end);
    $('#hist-csv').href = '/api/export/mesures.csv?start=' + Math.round(start) + '&end=' + Math.round(end);
    const box = $('#chart-hist-temp'); box.classList.add('loading');
    try {
      const points = Math.min(2500, Math.max(400, Math.round(box.clientWidth * 1.5)));
      const d = await api('/api/series?start=' + start + '&end=' + end + '&points=' + points);
      const series = {};
      for (const k of Object.keys(d.series)) series[k] = { t: d.t, v: d.series[k] };
      const banded = d.bucket_seconds > state.thresholds?.snapshot_interval_seconds;
      histTemp.o.thresholds = state.thresholds ? [{ value: state.thresholds.critical_temp, color: '--crit', label: 'critique ' + num(state.thresholds.critical_temp, 0) + ' °C', dash: [], offscale: true }, { value: state.thresholds.target_temp, color: '--ok', label: 'cible ' + num(state.thresholds.target_temp, 0) + ' °C' }] : [];
      histTemp.setData({ xMin: start, xMax: end, gapMs: d.gap_ms, series, band: banded ? { t: d.t, lo: d.pt02_min, hi: d.pt02_max } : null });
      histFlow.setData({ xMin: start, xMax: end, gapMs: d.gap_ms, series });
      histPower.setData({ xMin: start, xMax: end, gapMs: d.gap_ms, series });
      $('#hist-foot').textContent = d.count.toLocaleString('fr-FR') + ' mesures · ' +
        (banded ? 'moyennes par tranches de ' + ago(d.bucket_seconds * 1000) + ' (enveloppe min / max de PT02 en transparence)' : 'toutes les mesures affichées');
      renderStats(d, banded);
    } catch (err) {
      $('#hist-foot').textContent = 'Impossible de charger la période : ' + err.message;
    } finally { box.classList.remove('loading'); }
  }
  function renderStats(d, banded) {
    const body = $('#hist-stats tbody'); body.replaceChildren();
    for (const k of HIST_KEYS) {
      const vals = (d.series[k] || []).filter(v => v != null);
      const tr = el('tr');
      const name = el('td'); const sw = el('i', 'sw'); sw.style.background = `var(${COLORS[k]})`; name.append(sw, document.createTextNode(LABELS[k]));
      tr.append(name);
      if (!vals.length) {
        const td = el('td', 'r', 'aucune valeur'); td.colSpan = 4; tr.append(td);
      } else {
        let lo = Math.min(...vals), hi = Math.max(...vals);
        if (k === 'pt02') {
          const mins = d.pt02_min.filter(v => v != null), maxs = d.pt02_max.filter(v => v != null);
          if (mins.length) lo = Math.min(...mins); if (maxs.length) hi = Math.max(...maxs);
        }
        const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
        const u = ' ' + UNITS[k], dg = DIGITS[k];
        tr.append(el('td', 'r', num(lo, dg) + u), el('td', 'r', num(mean, dg) + u), el('td', 'r', num(hi, dg) + u));
        const amp = el('td', 'r', num(hi - lo, dg) + u);
        if (vals.length > 1 && hi - lo < 1e-9) amp.append(document.createTextNode(' '), el('span', 'chip warn', 'figée'));
        tr.append(amp);
      }
      body.append(tr);
    }
    $('#hist-stats-note').textContent = banded ? 'min / max calculés sur les moyennes par tranche (PT02 : valeurs réelles)' : 'sur toutes les mesures';
  }
  let histInit = false;
  loaders.historique = () => { if (!histInit) { histInit = true; setHistPreset(histMinutes); } loadHistory(); };

  // ================================================================ alertes
  let alertLevel = '', alertItems = [];
  segmented($('#alert-filter'), b => { alertLevel = b.dataset.level; loadAlerts(); });
  $('#alert-search').addEventListener('input', () => renderAlerts());
  async function loadAlerts() {
    try {
      const d = await api('/api/alerts?limit=500' + (alertLevel ? '&level=' + encodeURIComponent(alertLevel) : ''));
      alertItems = d.items;
      const counts = $('#alert-counts'); counts.replaceChildren();
      const c = d.counts_24h || {};
      for (const [lvl, label, cls] of [['CRITIQUE', 'Critiques · 24 h', 'crit'], ['ATTENTION', 'Attention · 24 h', 'warn'], ['INFO', 'Info · 24 h', '']]) {
        const box = el('div', 'count ' + ((c[lvl] || 0) ? cls : ''));
        box.append(el('span', null, label), el('b', null, String(c[lvl] || 0)));
        counts.append(box);
      }
      const total = el('div', 'count');
      total.append(el('span', null, 'Total · 24 h'), el('b', null, String(Object.values(c).reduce((a, b) => a + b, 0))));
      counts.append(total);
      renderAlerts();
    } catch (err) {
      $('#alerts-table tbody').replaceChildren(rowMsg(5, 'Impossible de charger les alertes : ' + err.message));
    }
  }
  function rowMsg(cols, text) { const tr = el('tr'); const td = el('td', 'empty', text); td.colSpan = cols; tr.append(td); return tr; }
  function renderAlerts() {
    const q = $('#alert-search').value.trim().toLowerCase();
    const items = q ? alertItems.filter(a => (a.rule + ' ' + (a.message || '')).toLowerCase().includes(q)) : alertItems;
    const body = $('#alerts-table tbody'); body.replaceChildren();
    if (!items.length) { body.append(rowMsg(5, q || alertLevel ? 'Aucune alerte ne correspond.' : 'Aucune alerte enregistrée.')); return; }
    for (const a of items) {
      const tr = el('tr', levelClass(a.level));
      const lv = el('td'); lv.append(chip(a.level));
      tr.append(el('td', 'n', a.ts ? fmtDateTime.format(new Date(a.ts)) : '—'), lv, el('td', 'n', a.rule), el('td', 'msg', a.message || ''),
        el('td', null, a.email_sent ? 'envoyé' : 'non'));
      body.append(tr);
    }
  }
  loaders.alertes = loadAlerts;

  // ================================================================ onglet PID
  // Coloration du JSON construite en nœuds DOM (jamais d'innerHTML).
  const JSON_TOKEN = /("(?:\\u[a-fA-F0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)/g;
  function renderJson(pre, value) {
    pre.replaceChildren();
    if (value == null) return;
    // Message enregistré : affiché caractère pour caractère tel qu'envoyé au broker (pas de reformatage).
    const raw = typeof value === 'string';
    pre.classList.toggle('raw', raw);
    const text = raw ? value : JSON.stringify(value, null, 2);
    let last = 0;
    for (const m of text.matchAll(JSON_TOKEN)) {
      if (m.index > last) pre.append(document.createTextNode(text.slice(last, m.index)));
      const tok = m[0];
      const cls = tok.startsWith('"') ? (m[2] ? 'k' : 's') : /true|false|null/.test(tok) ? 'b' : 'n';
      pre.append(el('span', cls, tok));
      last = m.index + tok.length;
    }
    pre.append(document.createTextNode(text.slice(last)));
  }
  async function copyText(text, button) {
    const done = ok => { const old = button.textContent; button.textContent = ok ? 'Copié' : 'Sélectionnez puis copiez'; setTimeout(() => { button.textContent = old; }, 1500); };
    try { await navigator.clipboard.writeText(text); done(true); } catch (_) { done(false); }
  }
  const SOURCE_CHIP = { env: '.env', code: 'code' };
  const fmtVal = (v, unit) => typeof v === 'number' ? num(v, Number.isInteger(v) ? 0 : (Math.abs(v) < 1 ? 2 : 1)) + (unit ? ' ' + unit : '') : String(v);

  let pidSettings = null, pidTopic = 'setpoint', pidMinutes = 60, pidItems = [], pidLastControlTs = null;
  const pidHistChart = chart('chart-pid-history', {
    label: 'Consignes PID', unit: '%', digits: 1, minSpan: 10, zeroBased: true, yInclude: [0, 100],
    series: [
      { key: 'raw', label: 'Calcul brut', color: '--faint', width: 1.2, dash: [3, 3], unit: '%' },
      { key: 'i_term', label: 'Terme intégral', color: '--s4', width: 1.2, unit: '%' },
      { key: 'setpoint', label: 'Consigne envoyée', color: '--s2', width: 2, area: true, unit: '%' },
    ],
    thresholds: [{ value: 100, color: '--line-strong', label: 'max 100 %', dash: [] }],
    emptyText: 'Aucun cycle PID sur cette période',
  });

  function renderPidSettings(d) {
    const gains = $('#pid-gains'); gains.replaceChildren();
    for (const g of d.gains) {
      const box = el('div', 'gain');
      box.append(el('span', 'lbl', g.label), el('b', null, fmtVal(g.value)), el('small', null, g.unit), el('small', null, g.help), el('code', null, g.variable));
      gains.append(box);
    }
    const fill = (sel, items) => {
      const dl = $(sel); dl.replaceChildren();
      for (const it of items) {
        const dt = el('dt', null, it.label); if (it.help) dt.append(el('small', null, it.help));
        const dd = el('dd'); dd.append(document.createTextNode(fmtVal(it.value, it.unit)), el('span', 'chip', SOURCE_CHIP[it.source] || it.source));
        dd.title = it.variable;
        dl.append(dt, dd);
      }
    };
    fill('#pid-loop', d.loop);
    fill('#pid-ff', d.feedforward);
    $('#pid-aw').textContent = d.anti_windup;
    const seg = $('#mqtt-topic'); seg.replaceChildren();
    for (const t of d.mqtt) {
      const b = el('button', null, t.key === 'setpoint' ? 'Consigne' : t.key === 'conditions' ? 'Conditions' : 'Prédiction');
      b.type = 'button'; b.dataset.key = t.key; b.setAttribute('aria-pressed', String(t.key === pidTopic));
      seg.append(b);
    }
  }
  segmented($('#mqtt-topic'), b => { pidTopic = b.dataset.key; renderMqtt(); });

  function lastMessageFor(key) {
    const s = state.snapshot, c = s && !s.empty ? s.control : null;
    if (key === 'setpoint') {
      if (!c) return { label: 'Aucun cycle PID enregistré', msg: null };
      if (c.mqtt_setpoint) return { label: 'Dernier message envoyé · ' + fmtTime.format(new Date(c.ts)), msg: c.mqtt_setpoint, ok: true };
      if (c.mqtt_sent === 0) return { label: 'Dernier calcul NON transmis (broker injoignable) · contenu qui aurait été envoyé', msg: { percent: c.setpoint, mode: c.mode, ts: c.ts / 1000 }, ok: false };
      return { label: 'Publication désactivée (PUBLISH_PID_SETPOINT=false) · contenu calculé', msg: { percent: c.setpoint, mode: c.mode, ts: c.ts / 1000 } };
    }
    if (key === 'conditions') {
      if (!c) return { label: 'Aucun cycle enregistré', msg: null };
      if (c.mqtt_conditions) return { label: 'Dernier message envoyé · ' + fmtTime.format(new Date(c.ts)), msg: c.mqtt_conditions, ok: true };
      return { label: 'Dernier message non transmis', msg: null, ok: false };
    }
    const p = s && !s.empty ? s.predictions : null;
    if (!p) return { label: 'Aucune prédiction récente', msg: null };
    const preds = {}; let horizon = null, made = null;
    for (const [k, m] of Object.entries(p.models)) { preds[k] = m.value; horizon = m.horizon_seconds; made = m.made_at; }
    const vals = Object.values(preds);
    return { label: 'Reconstitué depuis la base (ce topic n\'est pas journalisé tel quel)', msg: {
      predictions: preds, combined_prediction: preds[p.best_model], best_model: p.best_model,
      disagreement: p.disagreement ?? (vals.length > 1 ? Math.abs(vals[0] - vals[1]) : 0),
      disagreement_flag: (p.disagreement || 0) > p.disagreement_threshold, horizon_seconds: horizon, ts: made / 1000 } };
  }
  function renderMqtt() {
    if (!pidSettings) return;
    const t = pidSettings.mqtt.find(x => x.key === pidTopic);
    $('#mqtt-topic-name').textContent = t.topic;
    $('#mqtt-meta').textContent = t.title + ' · ' + (t.enabled ? 'publié' : 'NON publié (désactivé)') + ' toutes les ' + num(t.every_seconds, 0) + ' s · ' + t.encoding +
      ' · QoS ' + t.qos + ' · ' + (t.retain ? 'retenu' : 'non retenu (retain=false)') + ' · broker ' + (pidSettings.broker.tls ? 'TLS' : 'sans TLS') + ', port ' + pidSettings.broker.port;
    const last = lastMessageFor(pidTopic);
    const label = $('#mqtt-msg-label'); label.replaceChildren(document.createTextNode(last.label + ' '));
    if (last.ok === true) label.append(el('span', 'chip ok', 'transmis'));
    if (last.ok === false) label.append(el('span', 'chip crit', 'échec'));
    const pre = $('#mqtt-json');
    if (last.msg == null) { pre.replaceChildren(); pre.textContent = '—'; } else renderJson(pre, last.msg);
    const body = $('#mqtt-fields tbody'); body.replaceChildren();
    for (const f of t.fields) {
      const tr = el('tr'); tr.append(el('td', null, f.name), el('td', null, f.type), el('td', null, f.unit || '—'), el('td', null, f.desc)); body.append(tr);
    }
  }
  $('#mqtt-copy').addEventListener('click', e => copyText($('#mqtt-topic-name').textContent, e.currentTarget));
  $('#mqtt-copy-json').addEventListener('click', e => copyText($('#mqtt-json').textContent, e.currentTarget));

  function renderPidLive() {
    const s = state.snapshot, c = s && !s.empty ? s.control : null;
    setNumber($('#pl-sp'), c ? c.setpoint : null, 1);
    const mi = c ? (MODE_INFO[c.mode] || { label: c.mode, cls: '' }) : { label: 'aucun calcul', cls: 'warn' };
    const m = $('#pl-mode'); m.textContent = mi.label; m.className = 'chip ' + mi.cls;
    $('#pl-when').textContent = c ? fmtTime.format(new Date(c.ts)) + ' (il y a ' + ago(c.age_seconds * 1000) + ')' : '—';
    const mq = $('#pl-mqtt'); mq.replaceChildren();
    if (!c) mq.textContent = '—';
    else mq.append(c.mqtt_sent === 1 ? el('span', 'chip ok', 'transmis au broker') : c.mqtt_sent === 0 ? el('span', 'chip crit', 'échec : broker injoignable') : el('span', 'chip', 'publication désactivée'));
  }

  segmented($('#pid-range'), b => { pidMinutes = +b.dataset.min; loadPidHistory(); });
  $('#pid-failed-only').addEventListener('change', () => renderPidTable());
  async function loadPidHistory() {
    const now = Date.now() - state.clockOffset, start = now - pidMinutes * 60e3, end = now + 60e3;
    $('#pid-csv').href = '/api/export/consignes.csv?start=' + Math.round(start) + '&end=' + Math.round(end);
    const box = $('#chart-pid-history'); box.classList.add('loading');
    try {
      const d = await api('/api/control?start=' + Math.round(start) + '&end=' + Math.round(end) + '&limit=5000');
      pidItems = d.items;
      const sum = d.summary, counts = $('#pid-summary'); counts.replaceChildren();
      const card = (label, value, cls) => { const b = el('div', 'count ' + (cls || '')); b.append(el('span', null, label), el('b', null, value)); counts.append(b); };
      if (sum && sum.cycles) {
        card('Cycles calculés', String(sum.cycles));
        card('Consigne moyenne', pct(sum.mean));
        card('Temps à 100 %', Math.round(sum.share_max * 100) + ' %', sum.share_max > .5 ? 'warn' : '');
        card('Temps à 0 %', Math.round(sum.share_zero * 100) + ' %');
        card('En anticipation', Math.round(sum.share_feedforward * 100) + ' %');
        if (sum.sent != null) card('Envois MQTT réussis / échecs', sum.sent + ' / ' + sum.failed, sum.failed ? 'crit' : '');
      }
      const asc = pidItems.slice().reverse();
      const t = asc.map(r => r.ts);
      pidHistChart.setData({ xMin: start, xMax: now, gapMs: 3 * ((state.snapshot && state.snapshot.control && state.snapshot.control.interval_seconds) || 60) * 1000, series: {
        setpoint: { t, v: asc.map(r => r.setpoint) },
        raw: { t, v: asc.map(r => r.raw_output) },
        i_term: { t, v: asc.map(r => r.i_term) },
      } });
      renderPidTable(d.truncated);
    } catch (err) {
      $('#pid-table tbody').replaceChildren(rowMsg(12, 'Impossible de charger les consignes : ' + err.message));
    } finally { box.classList.remove('loading'); }
  }
  function renderPidTable(truncated) {
    const failedOnly = $('#pid-failed-only').checked;
    const items = failedOnly ? pidItems.filter(r => r.mqtt_sent === 0) : pidItems;
    const body = $('#pid-table tbody'); body.replaceChildren();
    if (!items.length) { body.append(rowMsg(12, failedOnly ? 'Aucun échec d\'envoi sur cette période.' : 'Aucun cycle PID sur cette période.')); return; }
    const opt = v => v == null ? '—' : num(v, 1);
    for (const r of items.slice(0, 1000)) {
      const tr = el('tr', r.mqtt_sent === 0 ? 'failed' : '');
      const mi = MODE_INFO[r.mode] || { label: r.mode, cls: '' };
      const mode = el('td'); mode.append(el('span', 'chip ' + mi.cls, mi.label));
      const mq = el('td'); mq.append(r.mqtt_sent === 1 ? el('span', 'chip ok', 'envoyé') : r.mqtt_sent === 0 ? el('span', 'chip crit', 'échec') : el('span', 'chip', 'désactivé'));
      tr.append(el('td', 'n', fmtShort.format(new Date(r.ts))), el('td', 'r', pct(r.setpoint)), mode,
        el('td', 'r', opt(r.t_oil)), el('td', 'r', r.error == null ? '—' : signed(r.error, 2)),
        el('td', 'r', opt(r.p_term)), el('td', 'r', opt(r.i_term)), el('td', 'r', opt(r.d_term)),
        el('td', 'r', opt((r.feedforward_prediction || 0) + (r.feedforward_outdoor || 0))), el('td', 'r', opt(r.raw_output)),
        el('td', 'r', opt(r.t_predicted)), mq);
      tr.addEventListener('click', () => {
        const next = tr.nextElementSibling;
        if (next && next.classList.contains('detail')) { next.remove(); tr.classList.remove('open'); return; }
        tr.classList.add('open');
        const d = el('tr', 'detail'); const td = el('td'); td.colSpan = 12;
        for (const [label, msg, topic] of [['Consigne', r.mqtt_setpoint, pidSettings && pidSettings.mqtt[0].topic], ['Conditions', r.mqtt_conditions, pidSettings && pidSettings.mqtt[1].topic]]) {
          td.append(el('div', 'lbl', label + ' → ' + (topic || '')));
          const pre = el('pre', 'json');
          if (msg) renderJson(pre, msg); else pre.textContent = r.mqtt_sent === 0 ? 'non transmis (broker injoignable)' : 'non publié';
          td.append(pre);
        }
        d.append(td); tr.after(d);
      });
      body.append(tr);
    }
    if (truncated) body.append(rowMsg(12, 'Affichage limité aux cycles les plus récents : exportez en CSV pour toute la période.'));
  }
  async function loadPidView() {
    renderPidLive();
    if (!pidSettings) {
      try { pidSettings = await api('/api/pid'); renderPidSettings(pidSettings); } catch (err) { $('#pid-gains').replaceChildren(el('p', 'callout warn', 'Impossible de charger les réglages : ' + err.message)); }
    }
    renderMqtt();
    loadPidHistory();
  }
  loaders.pid = loadPidView;
  function onSnapshotForPid(s) {
    if (state.view !== 'pid' || s.empty) return;
    renderPidLive(); renderMqtt();
    if (s.control && s.control.ts !== pidLastControlTs) { pidLastControlTs = s.control.ts; if (pidMinutes <= 1440) loadPidHistory(); }
  }

  // ================================================================ modèles
  const modelChart = chart('chart-models', {
    label: 'Prédiction comparée à la mesure', unit: '°C', digits: 2, minSpan: 3,
    series: [
      { key: 'pt02', label: 'PT02 mesurée', color: '--s1', width: 2 },
      { key: 'random_forest', label: MODEL_LABELS.random_forest, color: MODEL_COLORS.random_forest, width: 1.4, dash: [5, 3] },
      { key: 'gradient_boosting', label: MODEL_LABELS.gradient_boosting, color: MODEL_COLORS.gradient_boosting, width: 1.4, dash: [2, 3] },
    ],
  });
  legend('#legend-models', [['--s1', 'PT02 mesurée', ''], ['--pred', MODEL_LABELS.random_forest, 'dash'], ['--pred-2', MODEL_LABELS.gradient_boosting, 'dotted']]);
  let modelMinutes = 1440;
  segmented($('#model-range'), b => { modelMinutes = +b.dataset.min; loadModelChart(); });
  function setModelExports() {
    const now = Date.now();
    $('#predictions-csv').href = '/api/export/predictions.csv?start=' + (now - modelMinutes * 60e3) + '&end=' + now;
    $('#retrain-csv').href = '/api/export/entrainements.csv?days=400';
  }

  async function loadModelChart() {
    setModelExports();
    const box = $('#chart-models'); box.classList.add('loading');
    try {
      const d = await api('/api/series?minutes=' + modelMinutes + '&points=' + Math.max(300, Math.round(box.clientWidth)));
      const series = { pt02: { t: d.t, v: d.series.pt02 } };
      for (const [m, s] of Object.entries(d.predictions.models || {})) series[m] = { t: s.t, v: s.v };
      modelChart.o.emptyText = 'Aucune mesure ni prédiction sur cette période';
      modelChart.setData({ xMin: d.start, xMax: d.end, gapMs: d.gap_ms, series });
    } catch (err) { /* affiché par le tableau */ } finally { box.classList.remove('loading'); }
  }
  // ---------------------------------------------------------------- ré-entraînement automatique
  const pctGain = g => g == null ? '—' : (g >= 0 ? '−' : '+') + Math.round(Math.abs(g) * 100) + ' %';
  function cmdLine(command) {
    const line = el('div', 'cmd'); const code = el('code', null, command);
    const b = el('button', 'btn small', 'Copier'); b.type = 'button';
    b.addEventListener('click', () => copyText(command, b));
    line.append(code, b); return line;
  }
  async function loadRetrain() {
    let d;
    try { d = await api('/api/retrain'); } catch (err) { return; }
    const box = $('#retrain-proposal'); box.replaceChildren();
    const pr = d.proposal, run = d.last_run;
    $('#model-badge').hidden = !(pr && pr.status === 'pending');
    $('#retrain-when').textContent = run ? 'dernière nuit : ' + fmtDateTime.format(new Date(run.started_at)) + ' · ' + (run.rows || 0).toLocaleString('fr-FR') + ' mesures' : '';
    if (!run && !pr) {
      box.append(el('p', 'callout info', 'Aucun ré-entraînement n\'a encore tourné. Il a lieu chaque nuit (RETRAIN_AT, 3 h 15 par défaut) ; pour le lancer tout de suite : ./voltaneum retrain'));
    }
    if (pr && pr.status === 'pending') {
      const p = pr.proposed, cur = pr.current;
      const card = el('div', 'proposal');
      card.append(el('h3', null, 'Nouveau modèle proposé : prédiction à ' + fmtHorizon(p.horizon_seconds)), el('p', 'small-text', pr.reason));
      const cmp = el('div', 'cmp');
      const side = (cls, title, m) => {
        const b = el('div', cls); b.append(el('span', 'lbl', title));
        if (!m) { b.append(el('b', null, '—'), el('small', null, 'aucun modèle fiable')); return b; }
        b.append(el('b', null, fmtHorizon(m.horizon_seconds)),
          el('small', null, (MODEL_LABELS[m.best_model] || m.best_model) + ' · erreur ' + num(m.rmse, 2) + ' °C · ' + pctGain(m.gain) + ' d\'erreur vs persistance'));
        return b;
      };
      cmp.append(side('', 'Installé (données récentes)', cur), side('new', 'Proposé', p));
      card.append(cmp);
      for (const w of pr.warnings || []) card.append(el('p', 'callout warn', w));
      card.append(el('span', 'lbl', 'Pour l\'installer, sur le contrôleur'), cmdLine('./voltaneum model accept'),
        el('span', 'lbl', 'Pour la refuser'), cmdLine('./voltaneum model reject --note "raison"'));
      box.append(card);
    } else if (pr) {
      const label = { accepted: 'acceptée', rejected: 'refusée', withdrawn: 'retirée (plus fiable la nuit suivante)' }[pr.status] || pr.status;
      box.append(el('p', 'callout info', 'Dernière proposition (' + fmtHorizon(pr.proposed.horizon_seconds) + ') ' + label +
        (pr.operator ? ' par ' + pr.operator : '') + (pr.note ? ' : « ' + pr.note + ' »' : '') + '.'));
    }
    if (run) box.append(el('p', 'small-text', 'Décision de la dernière nuit : ' + run.reason + '.'));
    const c = d.criteria;
    $('#retrain-criteria').textContent = 'Un horizon est jugé fiable si son erreur est au moins ' + Math.round(c.min_gain * 100) +
      ' % plus faible que la persistance, reste sous ' + num(c.max_rmse, 2) + ' °C, et si la période de test couvre au moins ' +
      c.min_test_span_horizons + ' fois l\'horizon. Données : ' + c.max_days + ' derniers jours. Horizons essayés : ' + c.horizons.map(fmtHorizon).join(', ') + '.';
    const body = $('#retrain-table tbody'); body.replaceChildren();
    if (!run) body.append(rowMsg(6, 'Pas encore de résultats.'));
    for (const cand of (run ? run.candidates : [])) {
      const tr = el('tr');
      const v = el('td'); v.append(el('span', 'chip ' + (cand.eligible ? 'ok' : ''), cand.eligible ? 'fiable' : 'refusé'));
      if (pr && pr.status === 'pending' && pr.run_id === run.run_id && pr.proposed.horizon_seconds === cand.horizon_seconds) v.append(document.createTextNode(' '), el('span', 'chip pred', 'proposé'));
      tr.append(el('td', 'n', fmtHorizon(cand.horizon_seconds)), v,
        el('td', 'r', cand.rmse == null ? '—' : num(cand.rmse, 2) + ' °C'), el('td', 'r', cand.persistence_rmse == null ? '—' : num(cand.persistence_rmse, 2) + ' °C'),
        el('td', 'r', pctGain(cand.gain)), el('td', 'msg', cand.reason));
      body.append(tr);
    }
    const ev = $('#retrain-events'); ev.replaceChildren();
    const EV = { run: 'nuit', accepted: 'accepté', rejected: 'refusé', rollback: 'retour arrière' };
    for (const e of d.events) {
      const li = el('li');
      const when = e.started_at || e.at;
      const text = e.event === 'run' ? (e.decision === 'proposed' ? 'proposition à ' + fmtHorizon(e.proposed_horizon_seconds) : 'pas de proposition') + ' : ' + e.reason
        : (e.horizon_seconds ? fmtHorizon(e.horizon_seconds) : '') + (e.operator ? ' par ' + e.operator : '') + (e.note ? ' : « ' + e.note + ' »' : '');
      li.append(el('span', 'when', when ? fmtShort.format(new Date(when)) : '—'), el('span', 'chip ' + (e.event === 'accepted' ? 'ok' : e.event === 'rejected' ? 'warn' : ''), EV[e.event] || e.event), el('span', null, text));
      ev.append(li);
    }
    if (!d.events.length) ev.append(el('li', null, 'Aucun événement.'));
  }

  async function loadModels() {
    loadModelChart();
    loadRetrain();
    try {
      const d = await api('/api/models');
      const tr = d.training, box = $('#training'); box.replaceChildren();
      if (!tr.status) {
        box.append(el('p', 'callout warn', 'Aucun modèle installé. Le ré-entraînement nocturne en proposera un dès que les données le permettront (./voltaneum model).'));
        $('#train-when').textContent = '';
      } else {
        $('#train-when').textContent = tr.trained_at ? fmtDateTime.format(new Date(tr.trained_at * 1000)) : '';
        if (tr.status !== 'ok') box.append(el('p', 'callout warn', tr.message || ('Statut : ' + tr.status)));
        else box.append(el('p', 'callout ' + (tr.beats_persistence ? 'ok' : 'warn'), tr.beats_persistence
          ? 'Le meilleur modèle fait mieux que la persistance (« la température ne bouge pas ») : la prédiction apporte une information.'
          : 'Aucun modèle ne fait mieux que la persistance : ses prédictions ne sont pas plus fiables que la valeur actuelle.'));
        const metrics = el('div', 'metrics');
        for (const key of ['random_forest', 'gradient_boosting', 'baseline_persistence']) {
          if (!tr[key]) continue;
          const mbox = el('div', 'metric' + (key === tr.best_model ? ' best' : ''));
          mbox.append(el('span', null, MODEL_LABELS[key]), el('b', 'num', num(tr[key].rmse, 3) + ' °C'), el('small', null, 'RMSE · MAE ' + num(tr[key].mae, 3) + ' °C'));
          metrics.append(mbox);
        }
        box.append(metrics);
        const dl = el('dl', 'kv');
        const add = (k, v) => { dl.append(el('dt', null, k), el('dd', null, v)); };
        add('Meilleur modèle', MODEL_LABELS[tr.best_model] || tr.best_model || '—');
        add('Horizon', tr.horizon_seconds ? fmtHorizon(tr.horizon_seconds) : '—');
        add('Échantillons d\'entraînement', tr.n_samples != null ? tr.n_samples.toLocaleString('fr-FR') : '—');
        if (tr.degenerate_features && tr.degenerate_features.length) add('Variables constantes (écartées des entrées)', tr.degenerate_features.join(', '));
        box.append(dl);
      }
      const body = $('#live-models tbody'); body.replaceChildren();
      $('#live-note').textContent = d.window + ' dernières prédictions vérifiées';
      if (!d.live.length) body.append(rowMsg(6, 'Aucune prédiction encore comparée à la mesure réelle.'));
      for (const r of d.live) {
        const row = el('tr');
        const bias = el('td', 'r', signed(r.bias, 2) + ' °C');
        row.append(el('td', 'n', r.horizon_seconds >= 60 ? fmtHorizon(r.horizon_seconds) : r.horizon_seconds + ' s'),
          el('td', null, MODEL_LABELS[r.model] || r.model), el('td', 'r', num(r.rmse, 3) + ' °C'), el('td', 'r', num(r.mae, 3) + ' °C'), bias, el('td', 'r', String(r.n)));
        body.append(row);
      }
    } catch (err) {
      $('#training').replaceChildren(el('p', 'callout warn', 'Impossible de charger les modèles : ' + err.message));
    }
  }
  loaders.modeles = loadModels;

  // ================================================================ réglages
  async function loadSettings() {
    try {
      const d = await api('/api/settings');
      const root = $('#settings'); root.replaceChildren();
      const section = (title, rows) => {
        const p = el('section', 'panel');
        const ph = el('header', 'ph'); ph.append(el('h2', null, title)); p.append(ph);
        const pb = el('div', 'pb'); const dl = el('dl', 'kv');
        for (const [k, v] of Object.entries(rows)) dl.append(el('dt', null, k), el('dd', null, String(v)));
        pb.append(dl); p.append(pb); root.append(p);
      };
      section('Procédé', d.process);
      const bands = el('section', 'panel');
      const ph = el('header', 'ph'); ph.append(el('h2', null, 'Bandes d\'air extérieur'), el('small', null, 'valeurs indicatives')); bands.append(ph);
      const wrap = el('div', 'table-wrap'); const table = el('table', 'table');
      const head = el('tr'); for (const h of ['Bande', 'Air extérieur', 'Marche', 'Arrêt', 'Fréq. min.']) head.append(el('th', h === 'Bande' ? null : 'r', h));
      const thead = el('thead'); thead.append(head); table.append(thead);
      const tbody = el('tbody'); let prev = null;
      for (const b of d.bands) {
        const tr = el('tr');
        const range = prev == null ? '< ' + num(b.upper, 0) + ' °C' : b.upper == null ? '≥ ' + num(prev, 0) + ' °C' : num(prev, 0) + ' – ' + num(b.upper, 0) + ' °C';
        tr.append(el('td', null, b.name), el('td', 'r', range), el('td', 'r', num(b.fan_on) + ' °C'), el('td', 'r', num(b.fan_off) + ' °C'), el('td', 'r', num(b.min_freq_hz, 0) + ' Hz'));
        tbody.append(tr); prev = b.upper;
      }
      table.append(tbody); wrap.append(table); bands.append(wrap); root.append(bands);
      section('Collecte', d.collection);
      section('Alertes', d.alerts);
      section('Conservation des données', d.retention);
      const ranges = {};
      for (const [k, [lo, hi]] of Object.entries(d.valid_ranges)) ranges[LABELS[k] || k] = num(lo, 0) + ' … ' + num(hi, 0) + ' ' + (UNITS[k] || '');
      section('Plages de mesure acceptées', ranges);
      if (d.sensors) section('Sondes (noms modifiables dans .env : PT01_LABEL…)', d.sensors);
      if (d.flow_direction) section('Sens de circulation', d.flow_direction);
      if (d.thermal) section('Puissance dissipée estimée', d.thermal);
    } catch (err) {
      $('#settings').replaceChildren(el('p', 'callout warn', 'Impossible de charger les réglages : ' + err.message));
    }
  }
  loaders.reglages = loadSettings;

  // ================================================================ démarrage
  async function start() {
    applyTheme();
    tick(); setInterval(tick, 1000);
    try { renderSnapshot(await api('/api/snapshot')); } catch (err) { showDbError(err); }
    if (!state.thresholds) state.thresholds = { snapshot_interval_seconds: 15, stale_after_seconds: 120, valid_ranges: {} };
    route();
    connect();
    setInterval(() => { if (state.view === 'supervision' && !document.hidden) loadHealth(); }, 60e3);
    setInterval(() => { if (!document.hidden) supChart.render(); }, 15e3); // fait glisser l'axe du temps
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && state.view === 'supervision') { supChart.load(); loadRecentAlerts(); }
    });
  }
  start();
})();
