// Run with `npm test` (node --test). Fixtures are excerpts of stored texts from each source.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { formatText, decodeEntities, cleanSummary } from "../src/lib/textfmt.js";

const fixture = (name) => readFileSync(new URL(name, import.meta.url), "utf8");
const texts = (blocks) => blocks.map((b) => b.text);

test("entities, including old Windows-1252 numbers", () => {
  assert.equal(decodeEntities("A&nbsp;&amp;&nbsp;B &quot;x&quot; &#150; &#8212; &rsquo;s &eacute; &bogus;"),
    "A & B \"x\" – — ’s é &bogus;");
  assert.equal(cleanSummary("to&nbsp;certain persons.&nbsp; Under"), "to certain persons. Under");
});

test("Federal Register page: masthead, cover page, page breaks and trailer removed", () => {
  const blocks = formatText(fixture("fixtures-fr.html"));
  const all = texts(blocks).join("\n");
  assert.equal(blocks[0].text, "Executive Order 13663 of March 20, 2014");
  for (const junk of ["<html", "<pre", "[[Page", "Federal Register Volume", "FR Doc", "Billing code",
    "GRAPHIC", "CONTENTS", "Agricultural Marketing", "Vol. 79", "\u0000"]) {
    assert.ok(!all.includes(junk), `still has ${junk}`);
  }
  // A paragraph broken by a page marker stays separate from the next section.
  assert.ok(texts(blocks).some((t) => t.endsWith("except by agreement of the parties.")));
  assert.ok(texts(blocks).some((t) => t.startsWith("Sec. 4. Records Maintenance.")));
  assert.ok(texts(blocks).some((t) => t.startsWith("Sec. 5. Expiration.")));
  assert.ok(texts(blocks).includes("THE WHITE HOUSE,"));
});

test("GovInfo law: sections as headings, lettered items nested, GPO marks cleaned", () => {
  const blocks = formatText(fixture("fixtures-law.htm"));
  const find = (start) => blocks.find((b) => b.text.startsWith(start));
  assert.deepEqual(find("SEC. 2."), { text: "SEC. 2. DEPARTMENT OF HOMELAND SECURITY DATA FRAMEWORK.", head: true, level: 0 });
  assert.equal(find("SECTION 1.").head, true);
  assert.equal(find("This Act may be cited").text, "This Act may be cited as the “Department of Homeland Security Data Framework Act of 2018”.");
  assert.equal(find("(a) In General").level, 1);
  assert.equal(find("(1) Development").level, 2);
  assert.equal(find("(A) All information").level, 3);
  // Items separated by a page marker and items on consecutive lines are both split.
  assert.equal(find("(C) are trained").text, "(C) are trained in applicable standards for safeguarding and using such information.");
  assert.equal(find("(2) Guidance").text, "(2) Guidance.—The Secretary of Homeland Security shall—");
  const all = texts(blocks).join("\n");
  for (const junk of ["NOTE", "&lt;", "``", "''", "[[Page", "--"]) assert.ok(!all.includes(junk), `still has ${junk}`);
});

test("the same law stored as plain lines reads the same as the GovInfo page", () => {
  const page = fixture("fixtures-law.htm");
  const pre = page.match(/<pre>([\s\S]*)<\/pre>/)[1]
    .replace(/<[^>]+>/g, "").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
  assert.deepEqual(formatText(pre), formatText(page));
});

test("a law flattened onto one line still splits into sections and items", () => {
  const flat = "[115th Congress Public Law 331] [From the U.S. Government Publishing Office] [[Page 4483]] AN ACT OF 2018 " +
    "Be it enacted by the Senate and House, &lt;&lt;NOTE: 6 USC 101 note.&gt;&gt; SECTION 1. SHORT TITLE. This Act may be cited as the " +
    "``Data Act''. SEC. 2. DATA FRAMEWORK. (a) In General.--(1) Development.--The Secretary shall act. (2) Requirements.--The Secretary " +
    "shall ensure: (A) all information; and (B) any intelligence. Approved December 19, 2018.";
  assert.deepEqual(texts(formatText(flat)), [
    "AN ACT OF 2018", "Be it enacted by the Senate and House,", "SECTION 1. SHORT TITLE.",
    "This Act may be cited as the “Data Act”.", "SEC. 2. DATA FRAMEWORK.", "(a) In General.—",
    "(1) Development.—The Secretary shall act.", "(2) Requirements.—The Secretary shall ensure:",
    "(A) all information; and", "(B) any intelligence.", "Approved December 19, 2018."]);
});

test("PDF opinion: lines rejoined, hyphenation undone, running heads dropped", () => {
  const blocks = formatText(fixture("fixtures-print.txt"));
  const all = texts(blocks).join("\n");
  assert.ok(!all.includes("Page Proof Pending Publication"));
  assert.ok(all.includes("the Government may regard him as not yet admitted."));
  assert.ok(all.includes("meaning that he was allowed to physically enter the country"));
  assert.ok(texts(blocks).includes("Justice Thomas delivered the opinion of the Court."));
  // Part headings stand alone.
  assert.ok(blocks.some((b) => b.text === "I" && b.head));
  assert.ok(blocks.some((b) => b.text === "A" && b.head));
  // Ordinary paragraphs are not cut at every line.
  assert.ok(blocks.length < 30, `${blocks.length} paragraphs`);
});

test("one paragraph per line, and blank-line paragraphs", () => {
  assert.deepEqual(texts(formatText("First paragraph of the opinion, which is long.\nSecond one.\nI\nThird.")),
    ["First paragraph of the opinion, which is long.", "Second one.", "I", "Third."]);
  assert.deepEqual(texts(formatText("§1350. Tort\n\nThe district courts shall\nhave jurisdiction.\n\n(June 25, 1948.)\n\nNotes")),
    ["§1350. Tort", "The district courts shall have jurisdiction.", "(June 25, 1948.)", "Notes"]);
  assert.deepEqual(formatText(""), []);
});
