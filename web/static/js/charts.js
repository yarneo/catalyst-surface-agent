/* ============================================================
   charts.js — Chart.js wrappers, themed to match the dashboard
   ============================================================ */

/* single-series area chart with baseline (Live tab) */
function makeAreaChart(canvas, labels, data, opts) {
  const o = opts || {};
  const baseColor = o.color;
  return new Chart(canvas, {
    type: "line",
    data: {
      labels,
      datasets: [{
        data,
        borderColor: baseColor,
        borderWidth: 2.5,
        tension: 0.35,
        fill: true,
        backgroundColor: (ctx) => {
          const { chart } = ctx;
          if (!chart.chartArea) return "transparent";
          const c = baseColor.startsWith("oklch") ? baseColor : baseColor;
          const g = chart.ctx.createLinearGradient(0, chart.chartArea.top, 0, chart.chartArea.bottom);
          if (o.negative) {
            g.addColorStop(0, "transparent");
            g.addColorStop(1, withAlpha(baseColor, 0.30));
          } else {
            g.addColorStop(0, withAlpha(baseColor, 0.32));
            g.addColorStop(1, withAlpha(baseColor, 0.02));
          }
          return g;
        },
      }],
    },
    options: {
      interaction: { mode: "index", intersect: false },
      plugins: {
        tooltip: {
          callbacks: {
            title: (items) => (o.titleFmt ? o.titleFmt(labels[items[0].dataIndex]) : labels[items[0].dataIndex]),
            label: (c) => " " + (o.tfmt ? o.tfmt(c.parsed.y) : c.parsed.y),
          },
        },
        annotation: false,
      },
      scales: {
        x: { grid: { display: false }, border: { display: false },
             ticks: { maxRotation: 0, autoSkipPadding: 18 } },
        y: {
          grid: { color: Chart.GRID }, border: { display: false },
          ticks: { callback: (v) => (o.yfmt ? o.yfmt(v) : v), maxTicksLimit: 6 },
          suggestedMin: o.negative ? undefined : Math.min(...data) * 0.999,
        },
      },
      ...(o.baseline ? { } : {}),
    },
    plugins: o.baseline ? [baselinePlugin(o.baseline)] : [],
  });
}

// dotted baseline (e.g. $100k start)
function baselinePlugin(value) {
  return {
    id: "baseline",
    afterDraw(chart) {
      const y = chart.scales.y.getPixelForValue(value);
      const { left, right } = chart.chartArea;
      const ctx = chart.ctx;
      ctx.save();
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = "rgba(255,255,255,0.28)";
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(right, y); ctx.stroke();
      ctx.restore();
    },
  };
}

/* multi-line chart (Strategy growth, vol, drawdown overlays) */
function makeLineChart(canvas, labels, series, opts) {
  const o = opts || {};
  const datasets = series.map(s => ({
    label: s.label,
    data: s.data,
    borderColor: cssColor(s.color),
    backgroundColor: o.fill ? withAlpha(cssColor(s.color), 0.10) : "transparent",
    borderWidth: s.width || 1.5,
    borderDash: s.dash || [],
    tension: 0.2,
    fill: o.fill ? (s.fillTo != null ? s.fillTo : false) : false,
    pointRadius: 0,
    order: s.live ? 0 : 1,
  }));
  const extraPlugins = [];
  if (o.bands) extraPlugins.push(bandPlugin(o.bands));
  return new Chart(canvas, {
    type: "line",
    data: { labels, datasets },
    plugins: extraPlugins,
    options: {
      interaction: { mode: "index", intersect: false },
      plugins: {
        tooltip: {
          callbacks: {
            title: (items) => o.titleFmt ? o.titleFmt(labels[items[0].dataIndex]) : "",
            label: (c) => " " + c.dataset.label + ": " + (o.tfmt ? o.tfmt(c.parsed.y) : c.parsed.y),
          },
        },
      },
      scales: {
        x: {
          type: "category",
          grid: { color: Chart.GRID, drawTicks: false },
          border: { display: false },
          ticks: {
            autoSkip: false,
            maxRotation: 0,
            callback: o.xTickYears ? yearTicks(labels) : undefined,
            color: cssColor("var(--text-3)"),
          },
        },
        y: o.log ? {
          type: "logarithmic",
          grid: { color: Chart.GRID }, border: { display: false },
          min: o.ymin, max: o.ymax,
          ticks: { callback: o.yfmt || ((v) => v), maxTicksLimit: 7,
                   color: cssColor("var(--text-3)") },
        } : {
          grid: { color: Chart.GRID }, border: { display: false },
          min: o.ymin, max: o.ymax,
          ticks: { callback: o.yfmt || ((v) => v), maxTicksLimit: 7,
                   color: cssColor("var(--text-3)") },
        },
      },
    },
  });
}

/* stacked area (adaptive weights) */
function makeStackedArea(canvas, labels, series, opts) {
  const o = opts || {};
  const datasets = series.map(s => ({
    label: s.label,
    data: s.data,
    borderColor: cssColor(s.color),
    backgroundColor: withAlpha(cssColor(s.color), 0.55),
    borderWidth: 0,
    fill: true,
    pointRadius: 0,
    tension: 0.0,
  }));
  return new Chart(canvas, {
    type: "line",
    data: { labels, datasets },
    options: {
      interaction: { mode: "index", intersect: false },
      plugins: {
        tooltip: { callbacks: {
          title: (items) => o.titleFmt ? o.titleFmt(labels[items[0].dataIndex]) : "",
          label: (c) => " " + c.dataset.label + ": " + c.parsed.y + "%",
        } },
      },
      scales: {
        x: { stacked: true, grid: { display: false }, border: { display: false },
             ticks: { autoSkip: false, maxRotation: 0,
                      callback: yearTicks(labels), color: cssColor("var(--text-3)") } },
        y: { stacked: true, min: 0, max: 100, grid: { color: Chart.GRID }, border: { display: false },
             ticks: { callback: (v) => v + "%", color: cssColor("var(--text-3)") } },
      },
    },
  });
}

// shaded vertical band(s) over an x index range, e.g. 2008 GFC
function bandPlugin(bands) {
  return {
    id: "bands",
    beforeDatasetsDraw(chart) {
      const { ctx, chartArea, scales } = chart;
      bands.forEach(b => {
        const x1 = scales.x.getPixelForValue(b.from);
        const x2 = scales.x.getPixelForValue(b.to);
        ctx.save();
        ctx.fillStyle = b.color || "rgba(255,90,90,0.10)";
        ctx.fillRect(x1, chartArea.top, x2 - x1, chartArea.bottom - chartArea.top);
        if (b.label) {
          ctx.fillStyle = "rgba(255,140,140,0.85)";
          ctx.font = "600 11px 'IBM Plex Sans'";
          ctx.fillText(b.label, x1 + 6, chartArea.top + 14);
        }
        ctx.restore();
      });
    },
  };
}

/* convert any css color (incl oklch) to an rgba with given alpha by drawing it */
const _alphaCache = {};
function withAlpha(color, alpha) {
  const key = color + "|" + alpha;
  if (_alphaCache[key]) return _alphaCache[key];
  // resolve to rgb via a throwaway canvas
  const cv = document.createElement("canvas");
  cv.width = cv.height = 1;
  const cx = cv.getContext("2d");
  cx.fillStyle = color;
  cx.fillRect(0, 0, 1, 1);
  const [r, g, b] = cx.getImageData(0, 0, 1, 1).data;
  const out = `rgba(${r},${g},${b},${alpha})`;
  _alphaCache[key] = out;
  return out;
}
