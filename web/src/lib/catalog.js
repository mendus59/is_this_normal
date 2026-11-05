// Shared catalog loading, search, and filtering for the Browse and Trends pages.
// Everything runs in the reader's browser against the published catalog.json.

export const TYPES = [
  { id: "scotus_case", label: "Supreme Court", short: "Supreme Court" },
  { id: "public_law", label: "Laws", short: "Law" },
  { id: "executive_document", label: "Executive documents", short: "Executive" },
  { id: "action_event", label: "Executive actions", short: "Executive action" },
];
export const TYPE_LABEL = Object.fromEntries(TYPES.map((t) => [t.id, t.short]));

export const BANDS = ["routine", "ordinary", "unusual", "novel", "unprecedented"];
export const NOT_ANALYZED = "none";

// Administrations by inauguration date. Applied to every artifact by its date,
// so for court cases it marks the period, not who decided it.
export const ADMINS = [
  { id: "clinton", label: "Clinton", start: "1993-01-20" },
  { id: "bush43", label: "G. W. Bush", start: "2001-01-20" },
  { id: "obama", label: "Obama", start: "2009-01-20" },
  { id: "trump45", label: "Trump (1st term)", start: "2017-01-20" },
  { id: "biden", label: "Biden", start: "2021-01-20" },
  { id: "trump47", label: "Trump (2nd term)", start: "2025-01-20" },
];

export function adminFor(date) {
  if (!date) return null;
  let found = null;
  for (const a of ADMINS) if (date >= a.start) found = a.id;
  return found;
}

export const humanize = (s) => (s ?? "").replaceAll("_", " ");
export const bandLabel = (b) => (b === NOT_ANALYZED ? "Not yet analyzed" : b[0].toUpperCase() + b.slice(1));

export async function loadCatalog() {
  const dataBase = import.meta.env.PUBLIC_DATA_BASE_URL ?? "";
  if (!dataBase) throw new Error("No data source configured yet. Set PUBLIC_DATA_BASE_URL (see SETUP.md).");
  let raw;
  try {
    const manifest = await (await fetch(`${dataBase}/manifest.json`, { cache: "no-cache" })).json();
    raw = await (await fetch(`${dataBase}/${manifest.prefix}/catalog.json`)).json();
  } catch {
    throw new Error(`Could not load the catalog from ${dataBase}. Check the bucket's public access and CORS settings.`);
  }
  return prepare(raw);
}

/** Adds derived fields used for search, filtering, and grouping. */
export function prepare(raw) {
  return raw.map((a) => ({
    ...a,
    year: a.date ? Number(a.date.slice(0, 4)) : null,
    admin: adminFor(a.date),
    bandKey: a.band ?? NOT_ANALYZED,
    _title: (a.title ?? "").toLowerCase(),
    _text: [a.title, a.summary, a.id, humanize(a.subtype)].filter(Boolean).join(" \n ").toLowerCase(),
  }));
}

/**
 * Parses a search box into terms. Supports "quoted phrases", -excluded words,
 * and OR between alternatives: `tariff OR "trade agreement" -steel`.
 * Returns groups: an artifact matches when every group has at least one hit.
 */
export function parseQuery(q) {
  const tokens = [];
  const re = /(-?)"([^"]+)"|(\S+)/g;
  let m;
  while ((m = re.exec(q ?? ""))) {
    if (m[2] !== undefined) tokens.push({ neg: m[1] === "-", text: m[2].toLowerCase() });
    else if (m[3] === "OR") tokens.push({ or: true });
    else if (m[3].startsWith("-") && m[3].length > 1) tokens.push({ neg: true, text: m[3].slice(1).toLowerCase() });
    else tokens.push({ neg: false, text: m[3].toLowerCase() });
  }
  const groups = [];
  const exclude = [];
  let joinNext = false;
  for (const t of tokens) {
    if (t.or) { joinNext = groups.length > 0; continue; }
    if (t.neg) { exclude.push(t.text); joinNext = false; continue; }
    if (joinNext) groups[groups.length - 1].push(t.text);
    else groups.push([t.text]);
    joinNext = false;
  }
  return { groups, exclude, empty: groups.length === 0 && exclude.length === 0 };
}

export function matchesQuery(a, parsed) {
  if (parsed.empty) return true;
  for (const ex of parsed.exclude) if (a._text.includes(ex)) return false;
  return parsed.groups.every((alts) => alts.some((t) => a._text.includes(t)));
}

/** Higher is better: title hits count more than summary hits. */
export function relevance(a, parsed) {
  let score = 0;
  for (const alts of parsed.groups) {
    for (const t of alts) {
      if (a._title.includes(t)) score += a._title.startsWith(t) ? 6 : 4;
      else if (a._text.includes(t)) score += 1;
    }
  }
  return score;
}

export const DEFAULT_FILTERS = { q: "", type: "", sub: "", from: "", to: "", admin: "", band: "", sort: "" };

export function readFilters(search = location.search) {
  const p = new URLSearchParams(search);
  const f = { ...DEFAULT_FILTERS };
  for (const k of Object.keys(f)) f[k] = p.get(k) ?? "";
  return f;
}

export function writeFilters(f, extra = {}) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries({ ...f, ...extra })) if (v) p.set(k, v);
  const qs = p.toString();
  history.replaceState(null, "", qs ? `?${qs}` : location.pathname);
}

export function applyFilters(catalog, f, parsed = parseQuery(f.q)) {
  const from = f.from ? Number(f.from) : -Infinity;
  const to = f.to ? Number(f.to) : Infinity;
  return catalog.filter((a) =>
    (!f.type || a.type === f.type) &&
    (!f.sub || a.subtype === f.sub) &&
    (!f.admin || a.admin === f.admin) &&
    (!f.band || a.bandKey === f.band) &&
    (!(f.from || f.to) || (a.year !== null && a.year >= from && a.year <= to)) &&
    matchesQuery(a, parsed));
}

export function yearSpan(catalog) {
  let lo = Infinity, hi = -Infinity;
  for (const a of catalog) if (a.year !== null) { lo = Math.min(lo, a.year); hi = Math.max(hi, a.year); }
  return lo === Infinity ? [] : Array.from({ length: hi - lo + 1 }, (_, i) => lo + i);
}

/** Subtypes present for the chosen type (or all types), most common first. */
export function subtypesFor(catalog, type) {
  const counts = new Map();
  for (const a of catalog) if (a.subtype && (!type || a.type === type)) counts.set(a.subtype, (counts.get(a.subtype) ?? 0) + 1);
  return [...counts].sort((x, y) => y[1] - x[1]).map(([s]) => s);
}

/** Fills the shared filter controls' option lists from the data. */
export function fillOptions(el, catalog, f) {
  const years = yearSpan(catalog);
  for (const sel of [el.from, el.to]) {
    const keep = sel.value;
    sel.replaceChildren(new Option("Any", ""), ...years.map((y) => new Option(String(y), String(y))));
    sel.value = keep;
  }
  fillSubtypes(el, catalog, f.type);
}

export function fillSubtypes(el, catalog, type) {
  const keep = el.sub.value;
  const subs = subtypesFor(catalog, type);
  el.sub.replaceChildren(new Option("All", ""), ...subs.map((s) => new Option(humanize(s), s)));
  el.sub.value = subs.includes(keep) ? keep : "";
  el.sub.disabled = subs.length === 0;
}

// Fixed color per entity, so a filter never repaints the survivors.
export const TYPE_COLOR = {
  scotus_case: "var(--series-1)", public_law: "var(--series-2)",
  executive_document: "var(--series-3)", action_event: "var(--series-4)",
};
export const BAND_COLOR = Object.fromEntries([...BANDS, NOT_ANALYZED].map((b) => [b, `var(--band-${b})`]));

/** Counts rows per year for each key. Returns {key: number[]} aligned to years. */
export function countByYear(rows, years, keyOf, keys) {
  const idx = new Map(years.map((y, i) => [y, i]));
  const out = Object.fromEntries(keys.map((k) => [k, years.map(() => 0)]));
  for (const a of rows) {
    const i = idx.get(a.year);
    if (i === undefined) continue;
    const k = keyOf(a);
    if (out[k]) out[k][i] += 1;
  }
  return out;
}

export function typeSeries(rows) {
  const present = new Set(rows.map((a) => a.type));
  return TYPES.filter((t) => present.has(t.id)).map((t) => ({ key: t.id, label: t.label, color: TYPE_COLOR[t.id] }));
}

export const thisYear = () => new Date().getUTCFullYear();
