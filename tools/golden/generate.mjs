// Golden-vector generator (dev tool, run by hand: `npm run generate`).
//
// Executes the vendor web-UI functions that aiorehom ports to Python, and
// writes their outputs for seeded inputs to tests/golden/*.json.  The vendor
// sources are READ from the directory named by $REHOM_WEBUI_SRC and bundled
// IN MEMORY with esbuild: no vendor code is ever written to this repository,
// and none is distributed with it.  Only the generated vectors are stored.
//
// $REHOM_WEBUI_SRC is required (there is no default).  Output is
// byte-identical across runs (seeded PRNG, no timestamps).

import { createHash } from "node:crypto";
import { existsSync, readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import * as esbuild from "esbuild";

const HERE = dirname(fileURLToPath(import.meta.url));
if (!process.env.REHOM_WEBUI_SRC) {
  console.error("set REHOM_WEBUI_SRC to the vendor web-UI source directory (not distributed)");
  process.exit(1);
}
const SRC = resolve(process.env.REHOM_WEBUI_SRC);
const OUT = resolve(HERE, "../../tests/golden");
const SEED = 20260925;

const SOURCES = {
  running: "sharedhooks/zone/runningProgram.js",
  programMap: "sharedhooks/zone/useProgramMap.js",
  transition: "sharedhooks/zone/getNextTransitionTime.js",
  constants: "sharedhooks/constants.js",
};

if (!existsSync(SRC) || !Object.values(SOURCES).every((p) => existsSync(join(SRC, p)))) {
  console.error(`vendor sources not found under ${SRC} (set REHOM_WEBUI_SRC)`);
  process.exit(1);
}

// ---------------------------------------------------------------------------
// In-memory bundle of the vendor functions
// ---------------------------------------------------------------------------

const ENTRY = `
export { computeRunningProgram } from "./${SOURCES.running}";
export { getProgramIndexMap, getProgramMap } from "./${SOURCES.programMap}";
export { getNextTransitionTime } from "./${SOURCES.transition}";
export {
  MODE, SET_POINT, ZONE_SETP, DEUM_MODE, ZONE_PLAN_MODE, DEUM_CYCLE_MODE,
  DEUM_MODE_NAMES, DEUM_ENABLE_MODE,
} from "./${SOURCES.constants}";
`;

const reactShim = {
  name: "react-shim",
  setup(build) {
    build.onResolve({ filter: /^react$/ }, () => ({ path: "react", namespace: "react-shim" }));
    build.onLoad({ filter: /.*/, namespace: "react-shim" }, () => ({
      contents: "export const useMemo = (fn) => fn();",
      loader: "js",
    }));
  },
};

const bundle = await esbuild.build({
  stdin: { contents: ENTRY, resolveDir: SRC, sourcefile: "golden-entry.js", loader: "js" },
  bundle: true,
  write: false,
  format: "esm",
  platform: "node",
  nodePaths: [join(HERE, "node_modules")],
  plugins: [reactShim],
  logLevel: "silent",
});
const ui = await import(
  "data:text/javascript;base64," + Buffer.from(bundle.outputFiles[0].text).toString("base64")
);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function mulberry32(seed) {
  let a = seed;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const makeRng = () => {
  const next = mulberry32(SEED);
  const int = (lo, hi) => lo + Math.floor(next() * (hi - lo + 1)); // inclusive
  const pick = (items) => items[Math.floor(next() * items.length)];
  const chance = (p) => next() < p;
  const shuffle = (items) => {
    const out = items.slice();
    for (let i = out.length - 1; i > 0; i--) {
      const j = Math.floor(next() * (i + 1));
      [out[i], out[j]] = [out[j], out[i]];
    }
    return out;
  };
  return { next, int, pick, chance, shuffle };
};

const req = createRequire(join(HERE, "package.json"));
const versions = {
  node: process.versions.node,
  esbuild: esbuild.version,
  lodash: req("lodash/package.json").version,
};

function sha256(rel) {
  return createHash("sha256").update(readFileSync(join(SRC, rel))).digest("hex");
}

function writeVectors(name, fn, sources, cases) {
  const meta = {
    function: fn,
    sources,
    source_sha256: Object.fromEntries(sources.map((s) => [s, sha256(s)])),
    seed: SEED,
    ...versions,
    count: cases.length,
  };
  const body = cases.map((c) => JSON.stringify(c)).join(",\n");
  const text = `{"meta": ${JSON.stringify(meta)},\n"cases": [\n${body}\n]}\n`;
  writeFileSync(join(OUT, name), text);
  console.log(`${name}: ${cases.length} cases`);
}

const undef = (v) => (v === null ? undefined : v);
const csv = (levels) => levels.join(",");
const SLOTS = 48;

function rlProgram(rng) {
  // 1-6 run-length segments of levels 0-3 (valid alphabet only).
  const segments = rng.int(1, 6);
  const cuts = new Set();
  while (cuts.size < segments - 1) cuts.add(rng.int(1, SLOTS - 1));
  const bounds = [0, ...[...cuts].sort((a, b) => a - b), SLOTS];
  const out = [];
  for (let s = 0; s + 1 < bounds.length; s++) {
    const level = String(rng.int(0, 3));
    for (let i = bounds[s]; i < bounds[s + 1]; i++) out.push(level);
  }
  return csv(out);
}

const toState = (rows) => ({
  data: rows.map(([SubUni, Key, Valore]) => ({ SubUni, Key, Valore })),
});

// ---------------------------------------------------------------------------
// running_program.json: full cartesian product
// ---------------------------------------------------------------------------

function runningProgramCases() {
  const rng = makeRng();
  const P1 = csv(Array.from({ length: SLOTS }, () => String(rng.int(0, 3))));
  const P2 = csv(
    Array.from({ length: SLOTS }, (_, i) =>
      i % 5 === 0 ? rng.pick(["x", "4", ""]) : String(rng.int(0, 3)),
    ),
  );
  const C1 = csv(Array.from({ length: SLOTS }, () => String(rng.int(0, 3))));
  const normalise = (out) =>
    out === undefined ? null : out.map((v) => (/^[0-3]$/.test(String(v)) ? String(v) : "?")).join(",");
  const cases = [];
  for (const mode of ["0", "1", "2", "x", null])
    for (const setPoint of ["0", "1", "2", "3", "x", null])
      for (const setp of ["0", "1", "2", "3", "4", "5", "x"])
        for (const crono of [false, true])
          for (const today of [null, P1, P2])
            for (const cronoProgram of [null, C1]) {
              // Emulates useRunningProgram.js (not executed): the scheduled program is the
              // PROG_GIORNO record (or undefined); the crono program is split, or 48 zeros.
              const scheduled = today === null ? undefined : { Key: "PROG_GIORNO_INVERNO", Valore: today };
              const cronoArg = cronoProgram === null ? new Array(SLOTS).fill(0) : cronoProgram.split(",");
              const out = ui.computeRunningProgram(
                undef(mode), undef(setPoint), setp, scheduled, crono, cronoArg,
              );
              cases.push({
                mode, set_point: setPoint, setp, crono, today, crono_program: cronoProgram,
                out: normalise(out),
              });
            }
  return cases;
}

// ---------------------------------------------------------------------------
// program_map.json: random rows
// ---------------------------------------------------------------------------

function programMapCases() {
  const rng = makeRng();
  const seasons = ["0", "1", 1, 0, "", "2", "01", null];
  const keys = [
    "PROG_SETT_ESTATE", "PROG_SETT_INVERNO", "PROG_GIORNO_ESTATE", "PROG_GIORNO_INVERNO",
  ];
  const unrelated = ["NOME", "PROG_SETT", "PROG_GIORNO", "prog_sett_estate", "PROG_SETT_ESTATE_OLD"];
  const subunis = ["0", "1", "2", "3", "4", "5", "6", "00", "7", "99", "10", ""];
  const presets = ["0", "1", "2", "3", "5", "12", "99", "00", "x"];
  const cases = [];
  for (let n = 0; n < 500; n++) {
    const season = rng.pick(seasons);
    const rows = [];
    const count = rng.int(5, 40);
    for (let r = 0; r < count; r++) {
      const roll = rng.next();
      if (roll < 0.1) {
        rows.push([rng.pick(subunis), rng.pick(unrelated), String(rng.int(0, 9))]);
      } else if (roll < 0.55) {
        const key = rng.pick(keys.slice(0, 2));
        rows.push([rng.pick(subunis), key, rng.pick(presets)]);
      } else {
        const key = rng.pick(keys.slice(2));
        const valore = rng.chance(0.3)
          ? rlProgram(rng)
          : Array.from({ length: rng.int(1, 4) }, () => String(rng.int(0, 3))).join(",");
        rows.push([rng.pick(presets), key, valore]);
      }
    }
    if (rows.length > 1 && rng.chance(0.5)) {
      const [s, k] = rows[rng.int(0, rows.length - 1)];
      rows.push([s, k, rng.pick(presets)]); // duplicate identity: the last row wins
    }
    const state = toState(rows);
    const index = ui.getProgramIndexMap(undef(season), state);
    const programs = ui.getProgramMap(undef(season), state);
    const outPrograms = Object.fromEntries(
      Object.entries(programs).map(([k, v]) => [k, v === undefined ? null : v]),
    );
    cases.push({ season, rows, out: { index, programs: outPrograms } });
  }
  return cases;
}

// ---------------------------------------------------------------------------
// next_transition.json (+ _dangling)
// ---------------------------------------------------------------------------

const SUFFIX = { ESTATE: "ESTATE", INVERNO: "INVERNO" };
const PRESET_IDS = ["0", "1", "2", "3", "4", "5", "7", "12"];

function seasonRows(rng, suffix, { bindProbability = 0.85 } = {}) {
  const ids = rng.shuffle(PRESET_IDS).slice(0, rng.int(1, 4));
  const rows = ids.map((id) => [id, `PROG_GIORNO_${suffix}`, rlProgram(rng)]);
  for (let day = 0; day < 7; day++) {
    if (rng.chance(bindProbability)) rows.push([String(day), `PROG_SETT_${suffix}`, rng.pick(ids)]);
  }
  return rows;
}

function transition(season, rows, day, slot) {
  const out = ui.getNextTransitionTime({
    season: undef(season),
    zoneInterfaceState: toState(rows),
    currDay: day,
    currDaySlice: slot,
  });
  return out.length === 4 ? [...out, null] : out;
}

function constantProgram(level) {
  return csv(new Array(SLOTS).fill(String(level)));
}

function edgeTransitionCases(rng) {
  const cases = [];
  const add = (season, rows, day, slot) =>
    cases.push({ season, rows, day, slot, out: transition(season, rows, day, slot) });
  const week = (suffix, preset) =>
    Array.from({ length: 7 }, (_, d) => [String(d), `PROG_SETT_${suffix}`, preset]);

  // Same level for the whole week: wrap (one preset, and two presets with equal content).
  for (const [level, day, slot] of [[0, 0, 0], [3, 3, 17], [1, 6, 47], [2, 2, 30]]) {
    add("1", [["1", "PROG_GIORNO_ESTATE", constantProgram(level)], ...week("ESTATE", "1")], day, slot);
  }
  const twoPresets = [
    ["1", "PROG_GIORNO_INVERNO", constantProgram(2)],
    ["2", "PROG_GIORNO_INVERNO", constantProgram(2)],
    ...Array.from({ length: 7 }, (_, d) => [String(d), "PROG_SETT_INVERNO", d % 2 ? "1" : "2"]),
  ];
  for (const [day, slot] of [[0, 0], [5, 12]]) add("0", twoPresets, day, slot);

  // Only today bound: the walk stops at midnight.
  for (const [day, slot] of [[0, 0], [2, 40], [6, 47], [4, 10]]) {
    const program = rng.chance(0.5) ? constantProgram(3) : rlProgram(rng);
    add("0", [["5", "PROG_GIORNO_INVERNO", program], [String(day), "PROG_SETT_INVERNO", "5"]], day, slot);
  }

  // Change exactly at slot 47 / at midnight.
  const lateChange = csv([...new Array(47).fill("1"), "3"]);
  const allDays = [["1", "PROG_GIORNO_ESTATE", lateChange], ...week("ESTATE", "1")];
  for (const [day, slot] of [[2, 46], [2, 47], [6, 47], [0, 0]]) add("1", allDays, day, slot);
  const midnight = [
    ["1", "PROG_GIORNO_ESTATE", constantProgram(1)],
    ["2", "PROG_GIORNO_ESTATE", constantProgram(3)],
    ...Array.from({ length: 7 }, (_, d) => [String(d), "PROG_SETT_ESTATE", d === 4 ? "2" : "1"]),
  ];
  for (const [day, slot] of [[3, 47], [3, 0], [4, 47], [6, 47]]) add("1", midnight, day, slot);

  // Today unbound: not running.
  const noToday = [
    ["1", "PROG_GIORNO_INVERNO", rlProgram(rng)],
    ...[0, 1, 2, 4, 5, 6].map((d) => [String(d), "PROG_SETT_INVERNO", "1"]),
  ];
  for (const slot of [0, 23, 47]) add("0", noToday, 3, slot);
  add("1", noToday, 3, 10); // wrong season: nothing bound at all

  // Sunday "0" binds, "00" does not.
  const zeroes = [
    ["1", "PROG_GIORNO_INVERNO", constantProgram(2)],
    ["2", "PROG_GIORNO_INVERNO", constantProgram(0)],
    ["00", "PROG_SETT_INVERNO", "2"],
    ["0", "PROG_SETT_INVERNO", "1"],
    ["1", "PROG_SETT_INVERNO", "2"],
  ];
  add("0", zeroes, 0, 5);
  add("0", zeroes.filter(([s, k]) => !(s === "0" && k === "PROG_SETT_INVERNO")), 0, 5);

  // Non-canonical seasons (compat only: JS `+season === 1`).
  for (const season of ["01", 1, 0, "", null, "2", "1.0", " 1 ", "0x1", "1e0", "abc", "-1"]) {
    const rows = rng.shuffle([...seasonRows(rng, "ESTATE", { bindProbability: 1 }), ...seasonRows(rng, "INVERNO", { bindProbability: 1 })]);
    add(season, rows, rng.int(0, 6), rng.int(0, 47));
  }
  return cases;
}

function nextTransitionCases() {
  const rng = makeRng();
  const cases = edgeTransitionCases(rng);
  const odd = [1, 0, "01", "", null, "2"];
  for (let n = 0; n < 3000; n++) {
    const season = rng.chance(0.9) ? rng.pick(["0", "1"]) : rng.pick(odd);
    const rows = rng.shuffle([...seasonRows(rng, SUFFIX.ESTATE), ...seasonRows(rng, SUFFIX.INVERNO)]);
    const day = rng.int(0, 6);
    const slot = rng.int(0, 47);
    cases.push({ season, rows, day, slot, out: transition(season, rows, day, slot) });
  }
  return cases;
}

function danglingCases() {
  const rng = makeRng();
  const cases = [];
  for (let n = 0; n < 20; n++) {
    const season = rng.pick(["0", "1"]);
    const suffix = season === "1" ? "ESTATE" : "INVERNO";
    const day = rng.int(0, 6);
    const slot = rng.int(0, 47);
    // Today is bound to a preset that has no PROG_GIORNO row (the JS throws).
    const rows = seasonRows(rng, suffix).filter(([s, k]) => !(s === String(day) && k === `PROG_SETT_${suffix}`));
    rows.push([String(day), `PROG_SETT_${suffix}`, "42"]);
    const shuffled = rng.shuffle(rows);
    let out;
    try {
      ui.getNextTransitionTime({
        season,
        zoneInterfaceState: toState(shuffled),
        currDay: day,
        currDaySlice: slot,
      });
      out = { js_error: null };
    } catch (err) {
      out = { js_error: err.constructor.name };
    }
    cases.push({ season, rows: shuffled, day, slot, out });
  }
  return cases;
}

// ---------------------------------------------------------------------------
// constants.json
// ---------------------------------------------------------------------------

function constantsCases() {
  const names = [
    "MODE", "SET_POINT", "ZONE_SETP", "DEUM_MODE", "ZONE_PLAN_MODE", "DEUM_CYCLE_MODE",
    "DEUM_MODE_NAMES", "DEUM_ENABLE_MODE",
  ];
  return names.map((name) => ({ name, value: ui[name] }));
}

// ---------------------------------------------------------------------------

mkdirSync(OUT, { recursive: true });
writeVectors("running_program.json", "computeRunningProgram", [SOURCES.running, SOURCES.constants], runningProgramCases());
writeVectors("program_map.json", "getProgramIndexMap+getProgramMap", [SOURCES.programMap], programMapCases());
writeVectors("next_transition.json", "getNextTransitionTime", [SOURCES.transition, SOURCES.programMap], nextTransitionCases());
writeVectors("next_transition_dangling.json", "getNextTransitionTime", [SOURCES.transition, SOURCES.programMap], danglingCases());
writeVectors("constants.json", "constants", [SOURCES.constants], constantsCases());
