// Small dependency-free SVG charts: a stacked bar chart by year and a line chart.
// Colors come from CSS custom properties (--series-N) so dark mode swaps in one place.

const NS = "http://www.w3.org/2000/svg";
const svgEl = (tag, attrs = {}) => {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, String(v));
  return e;
};
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const fmt = (n) => n.toLocaleString(undefined, { maximumFractionDigits: 1 });

function niceMax(v) {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  const m = v / p;
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10) * p;
}

/** Rect with only the top corners rounded (the bar's data end). */
function topRounded(x, y, w, h, r) {
  r = Math.min(r, w / 2, h);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

function legend(series) {
  const ul = document.createElement("ul");
  ul.className = "viz-legend";
  for (const s of series) {
    const li = document.createElement("li");
    const sw = document.createElement("span");
    sw.className = s.line ? "sw line" : "sw";
    sw.style.background = s.color;
    li.append(sw, s.label);
    ul.append(li);
  }
  return ul;
}

function tooltip(host) {
  const tip = document.createElement("div");
  tip.className = "viz-tip";
  tip.hidden = true;
  host.append(tip);
  return {
    show(html, x, y) {
      tip.innerHTML = html;
      tip.hidden = false;
      const hw = host.clientWidth;
      const tw = tip.offsetWidth;
      tip.style.left = `${Math.max(0, Math.min(hw - tw, x - tw / 2))}px`;
      tip.style.top = `${Math.max(0, y - tip.offsetHeight - 10)}px`;
    },
    hide() { tip.hidden = true; },
  };
}

function yAxis(svg, { left, right, top, plotH, max, width, percent }) {
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const v = (max / ticks) * i;
    const y = top + plotH - (v / max) * plotH;
    svg.append(svgEl("line", { x1: left, x2: width - right, y1: y, y2: y, class: i === 0 ? "viz-base" : "viz-grid" }));
    const t = svgEl("text", { x: left - 6, y: y + 4, "text-anchor": "end", class: "viz-tick" });
    t.textContent = percent ? `${fmt(v)}%` : fmt(v);
    svg.append(t);
  }
}

function xLabels(svg, years, xOf, y, step, partialYear) {
  const every = step < 22 ? (step < 12 ? 5 : 2) : 1;
  const last = years.length - 1;
  years.forEach((yr, i) => {
    // Always label the last year; drop a regular label that would crowd it.
    if (i !== last && (i % every || (every > 1 && last - i < Math.ceil(every * 0.6)))) return;
    const t = svgEl("text", { x: xOf(i), y, "text-anchor": "middle", class: "viz-tick" });
    const label = every > 1 ? String(yr) : "’" + String(yr).slice(2);
    t.textContent = yr === partialYear ? `${label}*` : label;
    svg.append(t);
  });
}

/**
 * Stacked bars, one per year.
 * series: [{key, label, color}], values: {key: number[]} aligned to years.
 */
export function stackedBars(host, { years, series, values, height: fixedHeight, percent = false, partialYear = null, unit = "items", onBarClick }) {
  host.replaceChildren();
  host.classList.add("viz");
  if (series.length > 1) host.append(legend(series));
  const width = Math.max(320, host.clientWidth);
  const height = fixedHeight ?? (width < 520 ? 220 : 280);
  const m = { top: 12, right: 8, bottom: 28, left: percent ? 44 : 48 };
  const plotW = width - m.left - m.right;
  const plotH = height - m.top - m.bottom;
  const totals = years.map((_, i) => series.reduce((s, x) => s + (values[x.key][i] ?? 0), 0));
  const shown = (k, i) => {
    const v = values[k][i] ?? 0;
    return percent ? (totals[i] ? (100 * v) / totals[i] : 0) : v;
  };
  const max = percent ? 100 : niceMax(Math.max(1, ...totals));
  const step = plotW / years.length;
  const barW = Math.max(2, Math.min(28, step * 0.7));
  const xOf = (i) => m.left + step * i + step / 2;

  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width, height, role: "img", class: "viz-svg" });
  svg.setAttribute("aria-label", `${unit} per year, ${years[0]} to ${years[years.length - 1]}`);
  yAxis(svg, { left: m.left, right: m.right, top: m.top, plotH, max, width, percent });

  const tip = tooltip(host);
  years.forEach((yr, i) => {
    const g = svgEl("g", { class: "viz-bar" });
    let y = m.top + plotH;
    const nonzero = series.filter((s) => shown(s.key, i) > 0);
    nonzero.forEach((s, j) => {
      const h = (shown(s.key, i) / max) * plotH;
      const gap = j > 0 ? 2 : 0;
      const top = j === nonzero.length - 1;
      const hh = Math.max(0, h - gap);
      y -= h;
      if (hh <= 0) return;
      g.append(svgEl("path", {
        d: top ? topRounded(xOf(i) - barW / 2, y, barW, hh, 4) : `M${xOf(i) - barW / 2},${y}h${barW}v${hh}h${-barW}Z`,
        style: `fill:${s.color}`,
      }));
    });
    const hit = svgEl("rect", { x: m.left + step * i, y: m.top, width: step, height: plotH, class: "viz-hit" });
    if (onBarClick) hit.style.cursor = "pointer";
    g.append(hit);
    const show = () => {
      g.classList.add("on");
      const rows = series.filter((s) => (values[s.key][i] ?? 0) > 0).reverse().map((s) =>
        `<div class="r"><span class="sw" style="background:${esc(s.color)}"></span>${esc(s.label)}<b>${percent ? fmt(shown(s.key, i)) + "%" : fmt(values[s.key][i])}</b></div>`).join("");
      const head = `<div class="h">${yr}${yr === partialYear ? " (year to date)" : ""}<b>${fmt(totals[i])} ${unit}</b></div>`;
      const rect = host.getBoundingClientRect();
      const svgRect = svg.getBoundingClientRect();
      tip.show(head + (series.length > 1 ? rows : "") + (onBarClick ? `<div class="f">Click to browse ${yr}</div>` : ""),
        svgRect.left - rect.left + (xOf(i) * svgRect.width) / width, svgRect.top - rect.top + m.top);
    };
    hit.addEventListener("pointerenter", show);
    hit.addEventListener("pointerleave", () => { g.classList.remove("on"); tip.hide(); });
    if (onBarClick) hit.addEventListener("click", () => onBarClick(yr));
    svg.append(g);
  });
  xLabels(svg, years, xOf, height - 8, step, partialYear);
  host.append(svg);
  if (partialYear && years.includes(partialYear)) {
    const note = document.createElement("p");
    note.className = "viz-note";
    note.textContent = `* ${partialYear} is year to date.`;
    host.append(note);
  }
}

/** One line per series over years. series: [{key, label, color}], values: {key: number[]} */
export function lines(host, { years, series, values, partialYear = null, unit = "items" }) {
  host.replaceChildren();
  host.classList.add("viz");
  if (series.length > 1) host.append(legend(series.map((s) => ({ ...s, line: true }))));
  const width = Math.max(320, host.clientWidth);
  const height = width < 520 ? 220 : 280;
  const m = { top: 12, right: 12, bottom: 28, left: 48 };
  const plotW = width - m.left - m.right;
  const plotH = height - m.top - m.bottom;
  const max = niceMax(Math.max(1, ...series.flatMap((s) => values[s.key])));
  const step = years.length > 1 ? plotW / (years.length - 1) : plotW;
  const xOf = (i) => m.left + step * i;
  const yOf = (v) => m.top + plotH - (v / max) * plotH;

  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width, height, role: "img", class: "viz-svg" });
  svg.setAttribute("aria-label", `${unit} per year by search term`);
  yAxis(svg, { left: m.left, right: m.right, top: m.top, plotH, max, width });
  for (const s of series) {
    const d = values[s.key].map((v, i) => `${i ? "L" : "M"}${xOf(i)},${yOf(v)}`).join("");
    svg.append(svgEl("path", { d, fill: "none", style: `stroke:${s.color}`, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
  }
  const cross = svgEl("line", { y1: m.top, y2: m.top + plotH, class: "viz-cross" });
  cross.style.display = "none";
  svg.append(cross);
  const dots = series.map((s) => {
    const c = svgEl("circle", { r: 4, style: `fill:${s.color}`, class: "viz-dot" });
    c.style.display = "none";
    svg.append(c);
    return c;
  });
  xLabels(svg, years, xOf, height - 8, step, partialYear);
  const tip = tooltip(host);
  const hit = svgEl("rect", { x: m.left - step / 2, y: m.top, width: plotW + step, height: plotH, class: "viz-hit" });
  svg.append(hit);
  hit.addEventListener("pointermove", (ev) => {
    const r = svg.getBoundingClientRect();
    const px = ((ev.clientX - r.left) * width) / r.width;
    const i = Math.max(0, Math.min(years.length - 1, Math.round((px - m.left) / step)));
    cross.style.display = "";
    cross.setAttribute("x1", xOf(i)); cross.setAttribute("x2", xOf(i));
    series.forEach((s, j) => { dots[j].style.display = ""; dots[j].setAttribute("cx", xOf(i)); dots[j].setAttribute("cy", yOf(values[s.key][i])); });
    const rows = [...series].sort((a, b) => values[b.key][i] - values[a.key][i])
      .map((s) => `<div class="r"><span class="sw line" style="background:${esc(s.color)}"></span>${esc(s.label)}<b>${fmt(values[s.key][i])}</b></div>`).join("");
    const host_r = host.getBoundingClientRect();
    tip.show(`<div class="h">${years[i]}${years[i] === partialYear ? " (year to date)" : ""}</div>${rows}`,
      r.left - host_r.left + (xOf(i) * r.width) / width, r.top - host_r.top + m.top);
  });
  hit.addEventListener("pointerleave", () => { cross.style.display = "none"; dots.forEach((d) => (d.style.display = "none")); tip.hide(); });
  host.append(svg);
}

/** Plain table of the same numbers, for screen readers and exact values. */
export function table(host, { years, series, values, total = true }) {
  const t = document.createElement("table");
  t.className = "viz-table";
  const head = ["Year", ...series.map((s) => s.label), ...(total && series.length > 1 ? ["Total"] : [])];
  t.innerHTML = `<thead><tr>${head.map((h) => `<th scope="col">${esc(h)}</th>`).join("")}</tr></thead>`;
  const tb = document.createElement("tbody");
  years.forEach((y, i) => {
    const vals = series.map((s) => values[s.key][i] ?? 0);
    const cells = [...vals, ...(total && series.length > 1 ? [vals.reduce((a, b) => a + b, 0)] : [])];
    const tr = document.createElement("tr");
    tr.innerHTML = `<th scope="row">${y}</th>${cells.map((v) => `<td>${fmt(v)}</td>`).join("")}`;
    tb.append(tr);
  });
  t.append(tb);
  host.replaceChildren(t);
}
