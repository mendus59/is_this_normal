// Wires the Filters component to URL state. Calls onChange(filters) on every edit.
import { DEFAULT_FILTERS, readFilters, writeFilters, fillOptions, fillSubtypes } from "./catalog.js";

export function bindFilters(catalog, onChange, { extra = () => ({}) } = {}) {
  const el = {
    q: document.getElementById("f-q"),
    type: document.getElementById("f-type"),
    sub: document.getElementById("f-sub"),
    from: document.getElementById("f-from"),
    to: document.getElementById("f-to"),
    admin: document.getElementById("f-admin"),
    band: document.getElementById("f-band"),
    sort: document.getElementById("f-sort"),
  };
  // Citation-use checkboxes: all checked (the default) means no filter; the URL keeps
  // only a narrower choice, as a comma list.
  const boxes = [...document.querySelectorAll("input[name=f-cites]")];
  const citesValue = () => {
    const on = boxes.filter((b) => b.checked).map((b) => b.value);
    return on.length === boxes.length ? "" : on.join(",") || "-";
  };
  const setCites = (v) => {
    const on = v ? new Set(v.split(",")) : null;
    for (const b of boxes) b.checked = !on || on.has(b.value);
  };
  const initial = readFilters();
  setCites(initial.cites);
  fillOptions(el, catalog, initial);
  for (const [k, input] of Object.entries(el)) if (input) input.value = initial[k];
  fillSubtypes(el, catalog, initial.type);
  if (initial.sub) el.sub.value = initial.sub;

  const current = () => {
    const f = { ...DEFAULT_FILTERS };
    for (const [k, input] of Object.entries(el)) if (input) f[k] = input.value;
    if (boxes.length) f.cites = citesValue();
    // Keep the year range the right way round.
    if (f.from && f.to && Number(f.from) > Number(f.to)) [f.from, f.to] = [f.to, f.from];
    return f;
  };
  const fire = () => { const f = current(); writeFilters(f, extra()); onChange(f); };

  let t;
  el.q.addEventListener("input", () => { clearTimeout(t); t = setTimeout(fire, 150); });
  el.type.addEventListener("change", () => { fillSubtypes(el, catalog, el.type.value); fire(); });
  for (const k of ["sub", "from", "to", "admin", "band", "sort"]) el[k]?.addEventListener("change", fire);
  for (const b of boxes) b.addEventListener("change", fire);
  document.getElementById("f-reset").addEventListener("click", () => {
    for (const input of Object.values(el)) if (input) input.value = "";
    setCites("");
    fillSubtypes(el, catalog, "");
    fire();
  });

  /** Lets other controls (like a clicked year bar) set filters. */
  const set = (patch) => {
    for (const [k, v] of Object.entries(patch)) if (k === "cites") setCites(v); else if (el[k]) el[k].value = v;
    fire();
  };
  return { current, set, refresh: fire };
}
