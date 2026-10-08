#!/usr/bin/env node
// Local stdio MCP server over a brain-kit data folder ($BRAIN_ROOT, default ~/brain).
// Tools: overview, search, zoom, read, save. Read-only except save, which only
// appends a markdown note to inbox/ (picked up by the next nightly run).

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { rgPath } from "@vscode/ripgrep";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";

const run = promisify(execFile);
const ROOT = process.env.BRAIN_ROOT || path.join(os.homedir(), "brain");
const ITEMS = path.join(ROOT, "state", "items.json");
const TREE = path.join(ROOT, "tree");

let cache = { mtime: 0, items: {}, byPath: {} };
function items() {
  const st = fs.statSync(ITEMS, { throwIfNoEntry: false });
  if (st && st.mtimeMs !== cache.mtime) {
    const data = JSON.parse(fs.readFileSync(ITEMS, "utf8"));
    const byPath = {};
    for (const r of Object.values(data)) byPath[r.path] = r;
    cache = { mtime: st.mtimeMs, items: data, byPath };
  }
  return cache;
}

const text = (s) => ({ content: [{ type: "text", text: s }] });
const card = (r) =>
  `${r.ai_title || r.title} · ${r.date} · ${r.project || "?"} · ${r.source}\n  ${r.summary || "(not summarised yet)"}\n  id: ${r.id} · raw: ${r.path}`;

function inRoot(p) {
  const abs = path.resolve(ROOT, p);
  if (!abs.startsWith(ROOT + path.sep)) throw new Error("path outside the brain");
  return abs;
}

function filterItem(r, { project, source, since, until }) {
  if (project && (r.project || "").toLowerCase() !== project.toLowerCase()) return false;
  if (source && !(r.source || "").toLowerCase().includes(source.toLowerCase())) return false;
  if (since && (r.date || "") < since) return false;
  if (until && (r.date || "") > until) return false;
  return true;
}

function terms(q) {
  return (q.match(/"[^"]+"|\S+/g) || []).map((t) => t.replace(/^"|"$/g, "")).filter((t) => t.length > 1);
}

async function rg(args) {
  try {
    const { stdout } = await run(rgPath, args, { cwd: ROOT, maxBuffer: 64 * 1024 * 1024 });
    return stdout;
  } catch (e) {
    if (e.code === 1) return ""; // no matches
    throw e;
  }
}

const server = new McpServer({ name: "brain", version: "1.0.0" }, {
  instructions:
    "The user's personal brain: their Claude Code, Codex, Claude.ai and ChatGPT conversations, Wispr Flow meetings, Notion pages and notes, " +
    "with a summary tree on top. To answer a question about the user's past work or decisions: search first (it covers summaries and " +
    "full text), then zoom on an item id to see its section map, then read the raw file around the matching lines. Use overview " +
    "for orientation. Use save when the user asks to remember or note something.",
});

server.registerTool("overview", {
  description: "Top of the summary tree: who the user is, every project and its current state, recent months. Links to project and month nodes you can zoom into.",
  inputSchema: {},
}, async () => {
  const f = path.join(TREE, "overview.md");
  return text(fs.existsSync(f) ? fs.readFileSync(f, "utf8") : "Overview not built yet. Use search.");
});

server.registerTool("search", {
  description:
    "Search the brain. Matches item summaries/titles and tree node summaries first, then the full raw text of every conversation, " +
    "meeting and note (ripgrep, case-insensitive, all words must appear in the file; quote a phrase to keep it together; " +
    "wrap in /slashes/ for a regex). Returns item ids, raw paths and matching lines with line numbers for read().",
  inputSchema: {
    query: z.string().describe('Words to find, e.g. launch pricing, or "price per seat"'),
    scope: z.enum(["all", "summaries", "raw"]).default("all"),
    project: z.string().optional().describe("Exact project name, as listed in overview"),
    source: z.string().optional().describe("e.g. Claude Code, Codex, Claude, ChatGPT, Wispr, Notion"),
    since: z.string().optional().describe("YYYY-MM-DD"),
    until: z.string().optional().describe("YYYY-MM-DD"),
    limit: z.number().int().min(1).max(50).default(12),
  },
}, async ({ query, scope, project, source, since, until, limit }) => {
  const { items: all, byPath } = items();
  const flt = { project, source, since, until };
  const isRe = /^\/.+\/$/.test(query);
  const ts = isRe ? [query.slice(1, -1)] : terms(query);
  if (!ts.length) return text("Empty query.");
  const out = [];

  if (scope !== "raw") {
    const scored = [];
    for (const r of Object.values(all)) {
      if (!filterItem(r, flt)) continue;
      const hay = `${r.ai_title || ""} ${r.title || ""} ${r.project || ""} ${r.summary || ""} ${(r.sections || []).map((s) => s.note).join(" ")}`.toLowerCase();
      let hit = 0;
      for (const t of ts) {
        if (isRe ? new RegExp(t, "i").test(hay) : hay.includes(t.toLowerCase())) hit++;
      }
      if (hit === ts.length || (ts.length > 2 && hit >= ts.length - 1)) scored.push([hit, r]);
    }
    scored.sort((a, b) => b[0] - a[0] || (b[1].date || "").localeCompare(a[1].date || ""));
    if (scored.length) {
      out.push(`## Summary matches (${scored.length})\n`);
      for (const [, r] of scored.slice(0, limit)) {
        let s = card(r);
        const secs = (r.sections || []).filter((x) => ts.some((t) => x.note.toLowerCase().includes(t.toLowerCase())));
        for (const x of secs.slice(0, 3)) s += `\n  section lines ${x.lines[0]}-${x.lines[1]}: ${x.note}`;
        out.push(s);
      }
    }
    // Tree nodes
    const nodeHits = await rg(["-l", "-i", ...(isRe ? ["-e", ts[0]] : ts.flatMap((t) => ["-e", t])), "tree"]);
    const nodes = nodeHits.split("\n").filter(Boolean).filter((f) => {
      const body = fs.readFileSync(path.join(ROOT, f), "utf8").toLowerCase();
      return isRe || ts.every((t) => body.includes(t.toLowerCase()));
    });
    if (nodes.length) {
      out.push(`\n## Tree nodes mentioning it (zoom with these ids)\n`);
      out.push(nodes.slice(0, 10).map((f) => "- " + f.replace(/^tree\//, "").replace(/\.md$/, "")).join("\n"));
    }
  }

  if (scope !== "summaries") {
    // Files containing every term, ranked by total hits.
    let files = null;
    const counts = {};
    for (const t of ts) {
      const res = await rg(["-c", "-i", ...(isRe ? ["-e", t] : ["-F", "-e", t]), "raw", "inbox"]);
      const here = new Set();
      for (const line of res.split("\n").filter(Boolean)) {
        const i = line.lastIndexOf(":");
        const f = line.slice(0, i), n = Number(line.slice(i + 1));
        here.add(f);
        counts[f] = (counts[f] || 0) + Math.log2(1 + n);
      }
      files = files === null ? here : new Set([...files].filter((f) => here.has(f)));
    }
    let ranked = [...(files || [])].filter((f) => {
      const r = byPath[f];
      return !r || filterItem(r, flt);
    });
    ranked.sort((a, b) => counts[b] - counts[a]);
    // Re-rank the strongest candidates by lines where every term appears together.
    const together = {};
    if (ts.length > 1 && !isRe) {
      const cand = ranked.slice(0, 80);
      const res = cand.length ? await rg(["-n", "-i", "-H", ...ts.flatMap((t) => ["-F", "-e", t]), ...cand]) : "";
      for (const l of res.split("\n")) {
        const m = l.match(/^(.*?):\d+:/);
        if (!m) continue;
        const low = l.slice(m[0].length).toLowerCase();
        if (ts.every((t) => low.includes(t.toLowerCase()))) together[m[1]] = (together[m[1]] || 0) + 1;
      }
    }
    ranked.sort((a, b) =>
      Math.log2(1 + (together[b] || 0)) * 3 + counts[b] - (Math.log2(1 + (together[a] || 0)) * 3 + counts[a]) ||
      (byPath[b]?.date || "").localeCompare(byPath[a]?.date || ""));
    if (ranked.length) {
      out.push(`\n## Full-text matches (${ranked.length} files)\n`);
      for (const f of ranked.slice(0, limit)) {
        const r = byPath[f];
        const lines = await rg(["-n", "-i", "-m", "40", ...(isRe ? ["-e", ts[0]] : ts.flatMap((t) => ["-F", "-e", t])), f]);
        const best = lines.split("\n").filter(Boolean)
          .map((l) => {
            const low = l.toLowerCase();
            return [ts.filter((t) => low.includes(t.toLowerCase())).length, l];
          })
          .sort((a, b) => b[0] - a[0]).slice(0, 4)
          .map(([, l]) => "    " + (l.length > 300 ? l.slice(0, 300) + "…" : l));
        out.push((r ? card(r) : `${f}`) + "\n  matching lines (line:text):\n" + best.join("\n"));
      }
    }
  }
  return text(out.length ? out.join("\n") : `No matches for ${query}. Try fewer or different words, or check overview.`);
});

server.registerTool("zoom", {
  description:
    "Open one node of the tree and see its summary plus links to its children. Node ids: overview, projects/<slug> (e.g. projects/work), " +
    "projects/<slug>/<YYYY-MM>, months/<YYYY-MM>, weeks/<YYYY-Www>, days/<YYYY-MM-DD>. Or pass an item id (e.g. claude:…, claude-code:…) " +
    "to get its summary and section map with line ranges for read().",
  inputSchema: { node: z.string() },
}, async ({ node }) => {
  const { items: all } = items();
  node = node.replace(/^tree\//, "").replace(/\.md$/, "");
  if (all[node]) {
    const r = all[node];
    let s = `# ${r.ai_title || r.title}\n\n- id: ${r.id}\n- original title: ${r.title}\n- date: ${r.date} (updated ${r.updated})\n` +
      `- project: ${r.project || "?"}\n- source: ${r.source}\n- raw: ${r.path} (${r.chars?.toLocaleString()} chars)\n` +
      `- in tree: days/${r.date} · projects/${(r.project || "unsorted").toLowerCase().replace(/[^a-z0-9]+/g, "-")}/${r.date.slice(0, 7)}\n\n${r.summary || ""}\n`;
    if (r.sections?.length) s += `\n## Sections\n\n` + r.sections.map((x) => `- lines ${x.lines[0]}-${x.lines[1]}: ${x.note}`).join("\n");
    return text(s);
  }
  const f = path.join(TREE, node + ".md");
  if (!f.startsWith(TREE) || !fs.existsSync(f)) return text(`No node ${node}. Start from overview.`);
  return text(fs.readFileSync(f, "utf8"));
});

server.registerTool("read", {
  description: "Read a raw file (or tree node) by path or item id, paged by line. Use the line numbers from search or a section map.",
  inputSchema: {
    target: z.string().describe("raw/... path, tree/... path, or item id"),
    offset: z.number().int().min(1).default(1).describe("first line, 1-based"),
    limit: z.number().int().min(1).max(1500).default(250).describe("number of lines"),
  },
}, async ({ target, offset, limit }) => {
  const { items: all } = items();
  const rel = all[target]?.path || target;
  const abs = inRoot(rel);
  if (!fs.existsSync(abs)) return text(`Not found: ${rel}`);
  const lines = fs.readFileSync(abs, "utf8").split("\n");
  const end = Math.min(lines.length, offset - 1 + limit);
  const body = lines.slice(offset - 1, end).map((l, i) => `${offset + i}\t${l.length > 4000 ? l.slice(0, 4000) + "…" : l}`).join("\n");
  const more = end < lines.length ? `\n\n[lines ${offset}-${end} of ${lines.length}; continue with offset=${end + 1}]` : `\n\n[end, ${lines.length} lines]`;
  return text(`${rel}\n\n${body}${more}`);
});

server.registerTool("save", {
  description: "Save a note into the user's brain (inbox/), e.g. a decision, idea or fact they dictate from any chat. Ingested and summarised on the next nightly run; searchable immediately.",
  inputSchema: {
    note: z.string().describe("The note, in the user's words where possible"),
    title: z.string().optional(),
    project: z.string().optional().describe("Project name, as listed in overview"),
    app: z.string().optional().describe("Which app/chat this came from"),
  },
}, async ({ note, title, project, app }) => {
  const now = new Date();
  const stamp = now.toISOString().replace(/[:T]/g, "-").slice(0, 19);
  const t = (title || note.split("\n")[0]).slice(0, 80).trim();
  const slug = t.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 50) || "note";
  const dir = path.join(ROOT, "inbox");
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, `${stamp}-${slug}.md`);
  const meta = [`- saved: ${now.toLocaleString("en-GB")}`, project && `- project: ${project}`, app && `- from: ${app}`].filter(Boolean).join("\n");
  fs.writeFileSync(file, `# ${t}\n\n${meta}\n\n---\n\n${note.trim()}\n`, { flag: "wx" });
  return text(`Saved to ${path.relative(ROOT, file)}. It's searchable now and will be summarised on the next nightly run.`);
});

await server.connect(new StdioServerTransport());
