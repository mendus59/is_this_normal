// Turns a stored source text into readable paragraphs for the reader.
//
// The stored texts keep each source's own layout, because the pipeline reads them too:
// - Federal Register documents: the GPO "text" page, HTML around a fixed-width <pre> block,
//   with page markers ([[Page 8277]]), a masthead and a filing trailer.
// - Public laws: the same GPO fixed-width layout (older copies are flattened to one line),
//   with ``quotes'', -- dashes and <<NOTE: ...>> margin notes.
// - Supreme Court opinions from PDFs: lines wrapped at the page width, words hyphenated
//   across lines, and running heads ("Page Proof Pending Publication", "Cite as: ...").
// - Other opinions: one paragraph per line, or paragraphs split by blank lines.
// Each block returned is { text, head, level }: head marks a heading line, level is how deep
// a law's or order's lettered or numbered item sits among the items around it (0 for text).

const NAMED = {
  amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ", ensp: " ", emsp: " ", thinsp: " ",
  ndash: "–", mdash: "—", hyphen: "-", minus: "−", lsquo: "‘", rsquo: "’", sbquo: "‚", ldquo: "“", rdquo: "”",
  bdquo: "„", hellip: "…", sect: "§", para: "¶", middot: "·", bull: "•", deg: "°", copy: "©", reg: "®",
  trade: "™", frac12: "½", frac14: "¼", frac34: "¾", times: "×", divide: "÷", plusmn: "±", cent: "¢",
  pound: "£", euro: "€", yen: "¥", dagger: "†", Dagger: "‡", prime: "′", Prime: "″", laquo: "«", raquo: "»",
  iexcl: "¡", iquest: "¿", ordm: "º", ordf: "ª", sup1: "¹", sup2: "²", sup3: "³", shy: "",
  aacute: "á", Aacute: "Á", agrave: "à", Agrave: "À", acirc: "â", auml: "ä", atilde: "ã", aring: "å",
  ccedil: "ç", Ccedil: "Ç", eacute: "é", Eacute: "É", egrave: "è", Egrave: "È", ecirc: "ê", euml: "ë",
  iacute: "í", Iacute: "Í", igrave: "ì", icirc: "î", iuml: "ï", ntilde: "ñ", Ntilde: "Ñ",
  oacute: "ó", Oacute: "Ó", ograve: "ò", ocirc: "ô", ouml: "ö", Ouml: "Ö", otilde: "õ", oslash: "ø",
  uacute: "ú", Uacute: "Ú", ugrave: "ù", ucirc: "û", uuml: "ü", Uuml: "Ü", yacute: "ý", yuml: "ÿ",
  scaron: "š", Scaron: "Š", zcaron: "ž", Zcaron: "Ž", szlig: "ß", aelig: "æ", oelig: "œ",
};
// Old GPO pages use Windows-1252 code points 128-159 as numeric entities (&#150; is a dash).
const CP1252 = { 130: "‚", 132: "„", 133: "…", 134: "†", 135: "‡", 139: "‹", 145: "‘", 146: "’", 147: "“",
  148: "”", 149: "•", 150: "–", 151: "—", 153: "™", 155: "›" };

export function decodeEntities(s) {
  return String(s ?? "").replace(/&(#x[0-9a-f]+|#\d+|[a-z][a-z0-9]*);/gi, (m, e) => {
    if (e[0] === "#") {
      const n = e[1] === "x" || e[1] === "X" ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
      if (CP1252[n]) return CP1252[n];
      return n > 0 && n < 0x110000 && !(n >= 0xd800 && n < 0xe000) ? String.fromCodePoint(n) : m;
    }
    return NAMED[e] ?? m;
  });
}

/** An official summary: entities decoded, non-breaking spaces and runs of space folded. */
export function cleanSummary(s) {
  return decodeEntities(s).replace(/[\u00a0\s]+/g, " ").trim();
}

const PAGE = /^\s*\[\[Page [^\]]*\]\]\s*$/;
const GPO_DROP = [
  /^\s*Billing code/i, /^\s*<?GRAPHIC/i, /^\s*\[GRAPHIC\]/i, /^\s*\[TIFF OMITTED\]/i, /^\s*[-_=]{5,}\s*$/,
  /^\s*<all>\s*$/i, /^\s*<\/?DOC>\s*$/i, /^\s*\[DOCID:/, /^\s*Federal Register\s*\/\s*Vol\./, /^\s*\[(?:Federal Register Volume|FR Doc No:|Pages? \d)/,
];
const HEADING = /^(?:SEC(?:TION)?\.?\s+\d|Sec\.\s+\d|TITLE\s+[IVXLCDM\d]+\b|Subtitle\s+[A-Z]\b|PART\s+[IVXLCDM\d]+\b|CHAPTER\s+[\dIVXLC]+\b|DIVISION\s+[A-Z]\b)/;
const ITEM = /^\(([a-zA-Z]{1,4}|\d{1,3})\)\s+\S/;

const words = (s) => s.replace(/\s+/g, " ").trim();
const indent = (l) => l.match(/^\s*/)[0].length;
const caps = (t) => { const a = t.replace(/[^A-Za-z]/g, ""); return a.length >= 3 && a === a.toUpperCase(); };

/** GPO typography: ``quotes'', -- dashes, <<NOTE: ...>> margin notes. */
const dropNotes = (s) => s.replace(/<<\s*NOTE:[\s\S]*?>>/g, " ");
function gpoMarks(s) {
  return dropNotes(s).replace(/<bullet>/gi, "•").replace(/``/g, "“").replace(/''/g, "”").replace(/`/g, "‘").replace(/[ \t]*--[ \t]*/g, "—");
}

function isGpo(raw) {
  return /<pre[\s>]/i.test(raw) || /^\s*\[\[Page [^\]]*\]\]\s*$/m.test(raw);
}

function gpoLines(raw) {
  let s = raw.replace(/\r\n?/g, "\n");
  const pre = s.match(/<pre[^>]*>([\s\S]*?)(?:<\/pre>|$)/i);
  // A GPO page escapes the text inside <pre>, so tags come off before entities are decoded.
  // A stored plain copy (laws) is already decoded and may hold <<NOTE: ...>> literally.
  if (pre) s = decodeEntities(pre[1].replace(/<\/?(?:a|b|i|em|strong|span|font)\b[^>]*>/gi, "")
    .replace(/<[a-z\/!][^>\n]*>/gi, " "));
  s = dropNotes(s.replace(/\u00a0/g, " "));
  let lines = s.replace(/\u0000/g, "").split("\n");
  // Masthead: everything above the first page marker, when that comes early. An issue's
  // first document also carries a cover page; its text starts after the running head.
  const head = lines.slice(0, 80).findIndex((l) => /Federal Register\s*\/\s*Vol\./.test(l));
  const first = lines.findIndex((l, i) => i > head && PAGE.test(l));
  if (first >= 0 && first < 100) lines = lines.slice(first + 1);
  // Filing trailer, and anything the source page tacked on after it.
  const end = lines.findIndex((l) => /^\s*\[FR Doc\.? /.test(l));
  if (end >= 0) lines = lines.slice(0, end);
  const out = [];
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    if (PAGE.test(l)) {
      // Drop the marker. The blank lines around it are kept only where a sentence ended
      // there, so a sentence running over the page break joins up again.
      while (out.length && !out[out.length - 1].trim()) out.pop();
      while (i + 1 < lines.length && !lines[i + 1].trim()) i++;
      const before = out.length ? out[out.length - 1].trim() : "";
      const after = (lines[i + 1] ?? "").trim();
      if (/(?:[.:;!?”"]|--)$/.test(before) && /^[A-Z(“"`§[\d]/.test(after)) out.push("");
      continue;
    }
    if (GPO_DROP.some((re) => re.test(l))) {
      // The running head's second line names the section ("Presidential Documents").
      if (/Federal Register\s*\/\s*Vol\./.test(l) && i + 1 < lines.length && /^\s*\S.{0,40}$/.test(lines[i + 1])) i++;
      continue;
    }
    out.push(gpoMarks(l).replace(/\s+$/, ""));
  }
  return out;
}

function gpoBlocks(raw) {
  const lines = gpoLines(raw);
  const paras = [];
  let cur = null;
  let prev = null;
  const flush = () => { if (cur) paras.push(cur.parts.join(" ")); cur = null; };
  for (const l of lines) {
    if (!l.trim()) { flush(); prev = null; continue; }
    const t = l.trim();
    const n = indent(l);
    let start = !cur;
    if (cur && prev) {
      if (HEADING.test(t)) start = true;
      else if (ITEM.test(t) && (n > prev.n || n >= cur.n || /(?:[.:;]|—|--|\b(?:and|or))$/.test(prev.t))) start = true;
      else if (caps(prev.t) && caps(t)) start = false;
      // A deeper indent opens a paragraph (GPO indents first lines), unless the line plainly
      // continues the sentence (a centered title wrapping onto a lowercase line).
      else if (n > prev.n + 1 && /^[A-Z0-9(“"‘'§[]/.test(t) && !/[-—,]$/.test(prev.t)) start = true;
    }
    if (start) { flush(); cur = { n, parts: [] }; }
    cur.parts.push(t);
    prev = { n, t };
  }
  flush();
  return paras.map((p) => words(p).replace(/\s*—\s*/g, "—")).filter(Boolean);
}

/** A law text that an older load flattened onto one line. */
function flatLawBlocks(raw) {
  let s = decodeEntities(raw).replace(/\u00a0/g, " ").replace(/-{5,}/g, " ");
  s = gpoMarks(s).replace(/\[\[Page [^\]]*\]\]/g, " ").replace(/<all>|<\/?DOC>|\[DOCID:[^\]]*\]/gi, " ")
    .replace(/^\s*\[[^\]]*Public Law[^\]]*\]\s*(?:\[From the U\.S\. Government Publishing Office\])?/i, "");
  s = s
    .replace(/\s+(?=(?:(?:SEC\.|SECTION)\s+\d+[A-Za-z]?\.|TITLE\s+[IVXLCDM]+[—\s]|Subtitle\s+[A-Z]—|An Act\s|Be it enacted|Approved\s+[A-Z][a-z]+\.? \d|LEGISLATIVE HISTORY|[A-Z]+ REPORTS?:|CONGRESSIONAL RECORD))/g, "\n")
    .replace(/([.;:—])\s*(?=\((?:[a-z]{1,4}|\d{1,3}|[A-Z]{1,4})\)\s+[A-Za-z“(])/g, "$1\n")
    .replace(/(;\s+(?:and|or)|\b(?:and|or)(?=\s+\([^)]{1,4}\)\s+[A-Z“(]))\s+(?=\((?:[a-z]{1,4}|\d{1,3}|[A-Z]{1,4})\)\s+\S)/g, "$1\n")
    .replace(/(\b(?:SEC\.|SECTION)\s+\d+[A-Za-z]?\.\s+[A-Z0-9 ,;'’—-]{3,}?\.)\s+(?=[A-Z(][a-z(])/g, "$1\n");
  return s.split("\n").map((p) => words(p).replace(/\s*—\s*/g, "—")).filter(Boolean);
}

const RUNNING = [/^Page Proof Pending Publication$/i, /^Cite as: /, /^\d+$/, /^(?:Syllabus|Opinion of the Court)$/];
const DELIVERED = /^(?:(?:Justice|JUSTICE|Chief Justice|CHIEF JUSTICE) [A-Z][A-Za-z]+ (?:delivered|announced)|PER CURIAM\.?$|Per Curiam\.?$)/;
const KEEP_SHORT = /^(?:[IVXLC]+|[A-Z]|\d{1,2}|\*\s*\*\s*\*)$/;

function isWrapped(lines) {
  const full = lines.filter((l) => l.trim());
  if (full.length < 8) return false;
  const lens = full.map((l) => l.length).sort((a, b) => a - b);
  const median = lens[Math.floor(lens.length / 2)];
  const hyphens = full.filter((l) => /[a-z] ?-$/.test(l)).length;
  return median < 100 && (hyphens >= 3 || hyphens / full.length > 0.02);
}

function wrappedBlocks(raw) {
  let lines = raw.replace(/\r\n?/g, "\n").split("\n").map((l) => l.replace(/\s+/g, " ").trim());
  // Running heads repeat on every page, sometimes with the page number attached.
  const key = (l) => l.replace(/^\d+\s+|\s+\d+$/g, "");
  const seen = new Map();
  for (const l of lines) if (l && l.length <= 70) seen.set(key(l), (seen.get(key(l)) ?? 0) + 1);
  lines = lines.filter((l) => !l || KEEP_SHORT.test(l) ||
    !(RUNNING.some((re) => re.test(l)) || (l.length <= 70 && seen.get(key(l)) >= 3)));
  const full = lines.filter(Boolean);
  const lens = full.map((l) => l.length).sort((a, b) => a - b);
  const width = lens[Math.floor(lens.length * 0.9)] || 60;
  const hyphenated = new Set((raw.match(/\b[a-z]+-[a-z]+\b/gi) ?? []).map((w) => w.toLowerCase()));
  const paras = [];
  let cur = "";
  const flush = () => { if (cur.trim()) paras.push(cur.trim()); cur = ""; };
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    if (!l) { flush(); continue; }
    const head = KEEP_SHORT.test(l) && !/^\d/.test(l);
    const opens = DELIVERED.test(l);
    if (head || opens) flush();
    if (!cur) cur = l;
    else {
      const m = cur.match(/([A-Za-z]+) ?-$/);
      const next = l.match(/^([a-z]+)/);
      if (m && next) {
        // Rejoin a word split at the line end; keep the hyphen only for a word that is
        // hyphenated elsewhere in the text ("foreign-born").
        const joined = `${m[1]}-${next[1]}`.toLowerCase();
        cur = cur.replace(/ ?-$/, hyphenated.has(joined) ? "-" : "") + l;
      } else cur += " " + l;
    }
    const nextLine = lines[i + 1] ?? "";
    if (head || (opens && /\.$/.test(l)) || (l.length < width * 0.8 && /[.:?!”"’)\]]$/.test(l)) || (nextLine && KEEP_SHORT.test(nextLine) && !/^\d/.test(nextLine))) flush();
  }
  flush();
  return paras;
}

/** Text whose paragraphs are separated by blank lines, or one paragraph per line. */
function plainBlocks(raw) {
  const s = raw.replace(/\r\n?/g, "\n");
  const blank = (s.match(/\n[ \t]*\n/g) ?? []).length;
  const lines = s.split("\n").filter((l) => l.trim()).length;
  const parts = blank >= Math.max(2, lines * 0.2) ? s.split(/\n[ \t]*\n+/) : s.split("\n");
  return parts.map(words).filter((p) => p && !RUNNING[0].test(p));
}

/** Classify a paragraph: headings, and the outline depth of lettered and numbered items. */
function shape(paras, outline) {
  let lastLetter = "";
  let prevLevel = 0;
  const stack = [];
  return paras.map((text) => {
    const letters = text.replace(/[^A-Za-z]/g, "");
    const head = text.length <= 120 && (
      KEEP_SHORT.test(text) && !/^\d/.test(text) ||
      (letters.length >= 4 && letters === letters.toUpperCase() && !/^\(/.test(text)));
    let level = 0;
    const m = outline && text.match(/^\(([a-zA-Z]{1,4}|\d{1,3})\)/);
    if (m) {
      const k = m[1];
      if (/^\d+$/.test(k)) level = 2;
      else if (/^[ivxl]+$/.test(k) && (k.length > 1 || !lastLetter || prevLevel >= 3 ||
        String.fromCharCode(k.charCodeAt(0) - 1) !== lastLetter)) level = 4;
      else if (/^[a-z]$/.test(k)) { level = 1; lastLetter = k; }
      else if (/^[A-Z]$/.test(k)) level = 3;
      else if (/^[IVXL]+$/.test(k)) level = 5;
      else level = 6;
    }
    if (m || head) prevLevel = level;
    // Indent by depth among the items open around this one, so (i) under (a) sits one step in.
    if (!level) { stack.length = 0; return { text, head, level: 0 }; }
    while (stack.length && stack[stack.length - 1] >= level) stack.pop();
    stack.push(level);
    return { text, head, level: stack.length };
  });
}

/** Readable paragraphs for a stored text: [{ text, head, level }]. */
export function formatText(raw) {
  const s = String(raw ?? "");
  if (!s.trim()) return [];
  if (isGpo(s)) return shape(gpoBlocks(s), true);
  if (!/\n/.test(s.trim()) && /\[\[Page |&lt;&lt;NOTE|<<NOTE/.test(s)) return shape(flatLawBlocks(s), true);
  const lines = s.replace(/\r\n?/g, "\n").split("\n");
  const blankSeparated = (s.match(/\n[ \t]*\n/g) ?? []).length >= Math.max(2, lines.length * 0.2);
  if (!blankSeparated && isWrapped(lines)) return shape(wrappedBlocks(s), false);
  return shape(plainBlocks(s), false);
}
