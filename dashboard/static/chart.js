/* Voltaneum · TimeChart — courbes temporelles sur <canvas>, sans dépendance.
   - plusieurs séries, chacune avec ses propres horodatages, axe gauche ou droit ;
   - lignes coupées sur les trous de collecte, enveloppe min/max optionnelle ;
   - seuils horizontaux, ligne « maintenant », zone future grisée ;
   - transition douce des échelles quand la période change ;
   - réticule + infobulle au survol, synchronisable entre plusieurs graphiques. */
(function () {
  'use strict';

  const MIN = 60e3, HOUR = 3600e3, DAY = 86400e3;
  const TIME_STEPS = [MIN, 2 * MIN, 5 * MIN, 10 * MIN, 15 * MIN, 30 * MIN, HOUR, 2 * HOUR, 3 * HOUR,
    6 * HOUR, 12 * HOUR, DAY, 2 * DAY, 7 * DAY, 14 * DAY, 30 * DAY];
  const fmtHM = new Intl.DateTimeFormat('fr-FR', { hour: '2-digit', minute: '2-digit' });
  const fmtDay = new Intl.DateTimeFormat('fr-FR', { day: 'numeric', month: 'short' });
  const fmtFull = new Intl.DateTimeFormat('fr-FR', { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#888';
  }
  function niceStep(span, count) {
    const raw = span / Math.max(1, count);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const n = raw / mag;
    return (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * mag;
  }
  function lowerBound(arr, x) {
    let lo = 0, hi = arr.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (arr[mid] < x) lo = mid + 1; else hi = mid; }
    return lo;
  }
  function nearestIndex(arr, x) {
    if (!arr.length) return -1;
    const i = lowerBound(arr, x);
    if (i <= 0) return 0;
    if (i >= arr.length) return arr.length - 1;
    return (x - arr[i - 1] <= arr[i] - x) ? i - 1 : i;
  }
  const ease = t => 1 - Math.pow(1 - t, 3);

  class TimeChart {
    constructor(container, opts) {
      this.el = container;
      this.o = Object.assign({
        series: [], thresholds: [], unit: '', rightUnit: '', digits: 1, rightDigits: 1,
        minSpan: 2, rightMinSpan: 2, yInclude: [], rightInclude: [], zeroBased: false, rightZeroBased: false,
        now: false, tooltip: true, emptyText: 'Aucune mesure sur cette période', group: null,
      }, opts || {});
      this.hidden = new Set(this.o.series.filter(s => s.hidden).map(s => s.key));
      this.data = { series: {}, gapMs: Infinity, xMin: 0, xMax: 1 };
      this.dom = null; this.target = null; this.anim = null;
      this.hoverT = null; this.pointerInside = false;

      this.canvas = document.createElement('canvas');
      this.canvas.setAttribute('role', 'img');
      this.canvas.setAttribute('aria-label', this.o.label || 'Graphique');
      this.ctx = this.canvas.getContext('2d');
      this.emptyEl = document.createElement('div');
      this.emptyEl.className = 'chart-empty';
      this.emptyEl.hidden = true;
      container.append(this.canvas, this.emptyEl);

      this.tooltip = document.getElementById('tooltip');
      this.resizeObs = new ResizeObserver(() => this.resize());
      this.resizeObs.observe(container);
      this.canvas.addEventListener('pointermove', e => this.onPointer(e));
      this.canvas.addEventListener('pointerdown', e => this.onPointer(e));
      this.canvas.addEventListener('pointerleave', () => this.onLeave());
      if (this.o.group) this.o.group.push(this);
      this.resize();
    }

    // ---------------------------------------------------------------- données
    setData(d) {
      this.data = Object.assign({ series: {}, gapMs: Infinity }, d);
      this.retarget();
    }
    setHidden(key, hidden) {
      if (hidden) this.hidden.add(key); else this.hidden.delete(key);
      this.retarget();
    }
    hasData() {
      return this.o.series.some(s => !this.hidden.has(s.key) && (this.data.series[s.key]?.v || []).some(v => v != null));
    }

    computeDomain() {
      const d = this.data;
      const dom = { x0: d.xMin, x1: d.xMax };
      for (const side of ['left', 'right']) {
        let lo = Infinity, hi = -Infinity;
        for (const s of this.o.series) {
          if ((s.axis || 'left') !== side || this.hidden.has(s.key)) continue;
          const sd = d.series[s.key];
          if (!sd) continue;
          const i0 = lowerBound(sd.t, dom.x0), i1 = lowerBound(sd.t, dom.x1 + 1);
          for (let i = i0; i < i1; i++) {
            const v = sd.v[i];
            if (v == null) continue;
            if (v < lo) lo = v; if (v > hi) hi = v;
          }
          if (s.band && d.band) {
            for (const v of d.band.lo) if (v != null && v < lo) lo = v;
            for (const v of d.band.hi) if (v != null && v > hi) hi = v;
          }
        }
        const include = side === 'left' ? this.o.yInclude : this.o.rightInclude;
        for (const v of include) { if (v < lo) lo = v; if (v > hi) hi = v; }
        const zero = side === 'left' ? this.o.zeroBased : this.o.rightZeroBased;
        if (zero && lo > 0) lo = 0;
        if (!isFinite(lo)) { lo = 0; hi = 1; }
        const minSpan = side === 'left' ? this.o.minSpan : this.o.rightMinSpan;
        if (hi - lo < minSpan) { const c = (hi + lo) / 2; lo = c - minSpan / 2; hi = c + minSpan / 2; if (zero && lo < 0) { hi -= lo; lo = 0; } }
        const pad = (hi - lo) * 0.08;
        dom[side] = [zero && lo === 0 ? 0 : lo - pad, hi + pad];
      }
      return dom;
    }

    retarget() {
      const target = this.computeDomain();
      this.emptyEl.textContent = this.o.emptyText;
      this.emptyEl.hidden = this.hasData();
      if (!this.dom || reduceMotion.matches) { this.dom = target; this.draw(); return; }
      const from = JSON.parse(JSON.stringify(this.dom));
      const start = performance.now(), dur = 380;
      cancelAnimationFrame(this.anim);
      const step = now => {
        const k = ease(Math.min(1, (now - start) / dur));
        const lerp = (a, b) => a + (b - a) * k;
        this.dom = {
          x0: lerp(from.x0, target.x0), x1: lerp(from.x1, target.x1),
          left: [lerp(from.left[0], target.left[0]), lerp(from.left[1], target.left[1])],
          right: [lerp(from.right[0], target.right[0]), lerp(from.right[1], target.right[1])],
        };
        this.draw();
        if (k < 1) this.anim = requestAnimationFrame(step);
      };
      this.anim = requestAnimationFrame(step);
    }

    // ---------------------------------------------------------------- géométrie
    resize() {
      const r = this.el.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      this.w = Math.max(50, r.width); this.h = Math.max(50, r.height);
      this.canvas.width = Math.round(this.w * dpr); this.canvas.height = Math.round(this.h * dpr);
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      this.draw();
    }
    hasRight() { return this.o.series.some(s => s.axis === 'right' && !this.hidden.has(s.key)); }
    plot() {
      const narrow = this.w < 520;
      const L = narrow ? 38 : 48, R = this.hasRight() ? (narrow ? 38 : 48) : 14;
      return { l: L, r: this.w - R, t: 10, b: this.h - 24 };
    }
    xs(t) { const p = this.plot(); return p.l + (t - this.dom.x0) / (this.dom.x1 - this.dom.x0 || 1) * (p.r - p.l); }
    ys(v, side) {
      const p = this.plot(), d = this.dom[side || 'left'];
      return p.b - (v - d[0]) / (d[1] - d[0] || 1) * (p.b - p.t);
    }
    tAt(px) { const p = this.plot(); return this.dom.x0 + (px - p.l) / (p.r - p.l) * (this.dom.x1 - this.dom.x0); }

    // ---------------------------------------------------------------- dessin
    draw() {
      if (!this.dom || !this.w) return;
      const ctx = this.ctx, p = this.plot();
      const c = { fg: cssVar('--fg'), muted: cssVar('--muted'), line: cssVar('--line'), faint: cssVar('--faint'), panel: cssVar('--panel'), panel2: cssVar('--panel-2') };
      const fontNum = '11px ' + cssVar('--f-num');
      ctx.clearRect(0, 0, this.w, this.h);
      ctx.font = fontNum;
      ctx.textBaseline = 'middle';

      // Zone future (au-delà de maintenant)
      if (this.o.now) {
        const nowX = this.xs(Date.now());
        if (nowX < p.r) {
          ctx.fillStyle = c.panel2;
          ctx.fillRect(Math.max(p.l, nowX), p.t, p.r - Math.max(p.l, nowX), p.b - p.t);
        }
      }

      // Grille horizontale + graduations gauche
      const [y0, y1] = this.dom.left;
      const ystep = niceStep(y1 - y0, Math.max(2, Math.floor((p.b - p.t) / 46)));
      ctx.strokeStyle = c.line; ctx.lineWidth = 1; ctx.fillStyle = c.muted; ctx.textAlign = 'right';
      for (let v = Math.ceil(y0 / ystep) * ystep; v <= y1 + 1e-9; v += ystep) {
        const y = Math.round(this.ys(v)) + 0.5;
        ctx.beginPath(); ctx.moveTo(p.l, y); ctx.lineTo(p.r, y); ctx.stroke();
        ctx.fillText(this.fmtTick(v, ystep) + (this.o.unit ? '' : ''), p.l - 6, y);
      }
      if (this.o.unit) { ctx.textAlign = 'left'; ctx.fillStyle = c.faint; ctx.fillText(this.o.unit, 4, p.t + 2); }
      if (this.hasRight()) {
        const [r0, r1] = this.dom.right;
        const rstep = niceStep(r1 - r0, Math.max(2, Math.floor((p.b - p.t) / 46)));
        ctx.textAlign = 'left'; ctx.fillStyle = c.muted;
        for (let v = Math.ceil(r0 / rstep) * rstep; v <= r1 + 1e-9; v += rstep) ctx.fillText(this.fmtTick(v, rstep), p.r + 6, this.ys(v, 'right'));
        if (this.o.rightUnit) { ctx.textAlign = 'right'; ctx.fillStyle = c.faint; ctx.fillText(this.o.rightUnit, this.w - 4, p.t + 2); }
      }

      // Graduations temporelles
      const span = this.dom.x1 - this.dom.x0;
      const maxTicks = Math.max(2, Math.floor((p.r - p.l) / 78));
      const tstep = TIME_STEPS.find(s => span / s <= maxTicks) || 30 * DAY;
      const tzOff = new Date(this.dom.x0).getTimezoneOffset() * MIN;
      let first = Math.ceil((this.dom.x0 - tzOff) / tstep) * tstep + tzOff;
      ctx.textAlign = 'center'; ctx.textBaseline = 'top'; ctx.fillStyle = c.muted;
      for (let t = first; t <= this.dom.x1; t += tstep) {
        const x = Math.round(this.xs(t)) + 0.5;
        ctx.strokeStyle = c.line; ctx.globalAlpha = .55;
        ctx.beginPath(); ctx.moveTo(x, p.t); ctx.lineTo(x, p.b); ctx.stroke();
        ctx.globalAlpha = 1;
        const d = new Date(t);
        const midnight = d.getHours() === 0 && d.getMinutes() === 0;
        const lbl = tstep >= DAY || midnight ? fmtDay.format(d) : fmtHM.format(d);
        ctx.font = (midnight && tstep < DAY ? '600 ' : '') + fontNum;
        if (x > p.l + 14 && x < p.r - 14) ctx.fillText(lbl, x, p.b + 7);
      }
      ctx.font = fontNum;
      ctx.strokeStyle = c.line; ctx.beginPath(); ctx.moveTo(p.l, p.b + .5); ctx.lineTo(p.r, p.b + .5); ctx.stroke();

      // Seuils
      ctx.save(); ctx.beginPath(); ctx.rect(p.l, p.t - 2, p.r - p.l, p.b - p.t + 4); ctx.clip();
      for (const th of this.o.thresholds) {
        const side = th.axis || 'left';
        if (th.value < this.dom[side][0] || th.value > this.dom[side][1]) {
          // Seuil hors de l'échelle (ex. critique 55 °C quand l'huile est à 35 °C) : l'échelle
          // n'est pas écrasée pour l'afficher, mais il est SIGNALÉ au bord du graphique.
          if (th.offscale && th.label) {
            const above = th.value > this.dom[side][1];
            ctx.fillStyle = cssVar(th.color); ctx.textAlign = 'right';
            ctx.textBaseline = above ? 'top' : 'bottom';
            ctx.fillText((above ? '▲ ' : '▼ ') + th.label + ' (hors échelle)', p.r - 4, above ? p.t + 2 : p.b - 2);
          }
          continue;
        }
        const y = Math.round(this.ys(th.value, side)) + 0.5;
        ctx.strokeStyle = cssVar(th.color); ctx.lineWidth = th.width || 1.2; ctx.setLineDash(th.dash || [3, 4]);
        ctx.beginPath(); ctx.moveTo(p.l, y); ctx.lineTo(p.r, y); ctx.stroke(); ctx.setLineDash([]);
        if (th.label) {
          ctx.fillStyle = cssVar(th.color); ctx.textAlign = 'right'; ctx.textBaseline = th.below ? 'top' : 'bottom';
          ctx.fillText(th.label, p.r - 4, y + (th.below ? 3 : -3));
        }
      }

      // Enveloppe min/max
      const d = this.data;
      for (const s of this.o.series) {
        if (!s.band || !d.band || this.hidden.has(s.key)) continue;
        ctx.fillStyle = cssVar(s.color); ctx.globalAlpha = .13;
        this.pathBand(d.band, s.axis); ctx.fill(); ctx.globalAlpha = 1;
      }
      // Séries
      for (const s of this.o.series) {
        if (this.hidden.has(s.key)) continue;
        const sd = d.series[s.key];
        if (!sd || !sd.t.length) continue;
        const col = cssVar(s.color);
        if (s.area) {
          const grad = ctx.createLinearGradient(0, p.t, 0, p.b);
          grad.addColorStop(0, col); grad.addColorStop(1, 'transparent');
          ctx.fillStyle = grad; ctx.globalAlpha = .16;
          this.pathSeries(sd, s.axis, true); ctx.globalAlpha = .16; ctx.fill(); ctx.globalAlpha = 1;
        }
        ctx.strokeStyle = col; ctx.lineWidth = s.width || 1.6; ctx.lineJoin = 'round'; ctx.lineCap = 'round';
        ctx.setLineDash(s.dash || []);
        this.pathSeries(sd, s.axis, false); ctx.stroke(); ctx.setLineDash([]);
        if (s.endpoint) {
          let i = sd.v.length - 1; while (i >= 0 && sd.v[i] == null) i--;
          if (i >= 0) this.dot(this.xs(sd.t[i]), this.ys(sd.v[i], s.axis), col, c.panel, 4);
        }
      }
      ctx.restore();

      // Ligne « maintenant »
      if (this.o.now) {
        const x = Math.round(this.xs(Date.now())) + .5;
        if (x > p.l && x < p.r) {
          ctx.strokeStyle = c.faint; ctx.setLineDash([2, 3]);
          ctx.beginPath(); ctx.moveTo(x, p.t); ctx.lineTo(x, p.b); ctx.stroke(); ctx.setLineDash([]);
        }
      }

      // Réticule
      if (this.hoverT != null && this.hoverT >= this.dom.x0 && this.hoverT <= this.dom.x1) {
        const x = Math.round(this.xs(this.hoverT)) + .5;
        ctx.strokeStyle = c.muted; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(x, p.t); ctx.lineTo(x, p.b); ctx.stroke();
        for (const s of this.o.series) {
          if (this.hidden.has(s.key)) continue;
          const sd = d.series[s.key]; if (!sd) continue;
          const i = nearestIndex(sd.t, this.hoverT);
          if (i < 0 || sd.v[i] == null || Math.abs(sd.t[i] - this.hoverT) > (d.gapMs || Infinity)) continue;
          this.dot(this.xs(sd.t[i]), this.ys(sd.v[i], s.axis), cssVar(s.color), c.panel, 3.5);
        }
      }
    }

    pathSeries(sd, axis, closeArea) {
      const ctx = this.ctx, gap = this.data.gapMs || Infinity, p = this.plot();
      ctx.beginPath();
      let open = false, firstX = 0, lastX = 0, prevT = null;
      const i0 = Math.max(0, lowerBound(sd.t, this.dom.x0) - 1), i1 = Math.min(sd.t.length, lowerBound(sd.t, this.dom.x1) + 1);
      const closeSeg = () => { if (open && closeArea) { ctx.lineTo(lastX, p.b); ctx.lineTo(firstX, p.b); ctx.closePath(); } open = false; };
      for (let i = i0; i < i1; i++) {
        const v = sd.v[i];
        if (v == null || (prevT != null && sd.t[i] - prevT > gap)) closeSeg();
        if (v == null) { prevT = null; continue; }
        const x = this.xs(sd.t[i]), y = this.ys(v, axis);
        if (!open) { ctx.moveTo(x, y); firstX = x; open = true; } else ctx.lineTo(x, y);
        lastX = x; prevT = sd.t[i];
      }
      closeSeg();
    }
    pathBand(b, axis) {
      const ctx = this.ctx; ctx.beginPath();
      const pts = [];
      for (let i = 0; i < b.t.length; i++) if (b.lo[i] != null && b.hi[i] != null) pts.push(i);
      if (!pts.length) return;
      pts.forEach((i, k) => { const x = this.xs(b.t[i]), y = this.ys(b.hi[i], axis); if (k) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
      for (let k = pts.length - 1; k >= 0; k--) { const i = pts[k]; ctx.lineTo(this.xs(b.t[i]), this.ys(b.lo[i], axis)); }
      ctx.closePath();
    }
    dot(x, y, fill, ring, r) {
      const ctx = this.ctx;
      ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fillStyle = fill; ctx.fill(); ctx.lineWidth = 2; ctx.strokeStyle = ring; ctx.stroke();
    }
    fmtTick(v, step) {
      const dec = step >= 1 ? 0 : Math.min(3, Math.ceil(-Math.log10(step)));
      return v.toFixed(dec).replace('.', ',').replace(/^-0$/, '0');
    }

    // ---------------------------------------------------------------- survol
    onPointer(e) {
      const r = this.canvas.getBoundingClientRect();
      const x = e.clientX - r.left, p = this.plot();
      if (x < p.l || x > p.r) { this.onLeave(); return; }
      this.pointerInside = true;
      const t = this.tAt(x);
      for (const ch of (this.o.group || [this])) ch.setHover(t, ch === this ? e : null);
    }
    onLeave() {
      this.pointerInside = false;
      for (const ch of (this.o.group || [this])) ch.setHover(null, null);
    }
    setHover(t, evt) {
      this.hoverT = t;
      this.draw();
      if (!this.tooltip) return;
      if (t == null) { if (!evt && !(this.o.group || []).some(ch => ch.pointerInside)) this.tooltip.hidden = true; return; }
      if (!evt || !this.o.tooltip) return;
      const rows = this.tooltipRows(t);
      if (!rows.length) { this.tooltip.hidden = true; return; }
      const tt = this.tooltip;
      tt.replaceChildren();
      const head = document.createElement('div'); head.className = 'tt-time'; head.textContent = fmtFull.format(new Date(rows.t));
      tt.append(head);
      for (const group of (this.o.group || [this])) {
        for (const row of group === this ? rows : group.tooltipRows(t)) {
          const line = document.createElement('div'); line.className = 'tt-row';
          const sw = document.createElement('i'); sw.style.background = cssVar(row.color);
          const lb = document.createElement('span'); lb.textContent = row.label;
          const val = document.createElement('b'); val.textContent = row.text;
          line.append(sw, lb, val); tt.append(line);
        }
      }
      tt.hidden = false;
      const w = tt.offsetWidth, h = tt.offsetHeight;
      let left = evt.clientX + 16, top = evt.clientY + 14;
      if (left + w > window.innerWidth - 8) left = evt.clientX - w - 16;
      if (top + h > window.innerHeight - 8) top = evt.clientY - h - 14;
      tt.style.left = Math.max(8, left) + 'px'; tt.style.top = Math.max(8, top) + 'px';
    }
    tooltipRows(t) {
      const rows = [], d = this.data;
      rows.t = t;
      let bestDist = Infinity;
      for (const s of this.o.series) {
        if (this.hidden.has(s.key) || s.noTooltip) continue;
        const sd = d.series[s.key]; if (!sd) continue;
        const i = nearestIndex(sd.t, t);
        if (i < 0 || sd.v[i] == null) continue;
        const dist = Math.abs(sd.t[i] - t);
        if (dist > (d.gapMs || Infinity)) continue;
        if (dist < bestDist) { bestDist = dist; rows.t = sd.t[i]; }
        const digits = s.digits != null ? s.digits : (s.axis === 'right' ? this.o.rightDigits : this.o.digits);
        const unit = s.unit != null ? s.unit : (s.axis === 'right' ? this.o.rightUnit : this.o.unit);
        rows.push({ label: s.label, color: s.color, text: sd.v[i].toFixed(digits).replace('.', ',') + (unit ? ' ' + unit : '') });
      }
      return rows;
    }
  }

  window.TimeChart = TimeChart;
})();
