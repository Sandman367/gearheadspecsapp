/* Wire colours, drawn from data.

   The swatch is an SVG built from the stored value every time, never a saved
   image: add a colour to the vocabulary and every wire that uses it redraws,
   with no assets to regenerate and nothing to go stale.

   The colour list and this bike's abbreviations come from /api/wire-colors, so
   the page and the server cannot disagree about what a colour is called. */

let WIRE = null;   // {colors:[{key,name,hex,light,abbr}], make, max_stripes}

async function loadWireVocabulary(make){
  if(WIRE && WIRE.make === (make || null)) return WIRE;
  WIRE = await API.get("/api/wire-colors?make=" + encodeURIComponent(make || ""));
  WIRE.byKey = Object.fromEntries(WIRE.colors.map(c => [c.key, c]));
  return WIRE;
}

const wireParts = v => String(v || "").split("/").map(p => p.trim()).filter(Boolean);

/* A wire does not always keep its colour: it leaves the switch black/white and
   carries on past a connector as blue/red. Same wire, and a rider tracing it
   has to be told, or they decide they are on the wrong one.

   "a > b @ where" is one wire in two segments; a value with no ">" is a run of
   one, so everything written before this reads unchanged. */
function wireRun(value){
  return String(value || "").split(">").map(p => p.trim()).filter(Boolean)
    .map(part => {
      const i = part.indexOf("@");
      return i === -1
        ? { value: part.trim(), where: "" }
        : { value: part.slice(0, i).trim(), where: part.slice(i + 1).trim() };
    });
}
/* The picker's live run. A segment that has been added but not yet coloured
   is dropped by wireRunValue -- it has no colours to write -- so re-reading
   the run from the string would lose it, and the next swatch would edit the
   PREVIOUS segment instead. Exactly the bug the SET already avoids by being
   passed through as an array. */
function wireRunOf(w){
  return Array.isArray(w._run) && w._run.length
    ? w._run.map(x => ({...x}))
    : wireRun(w.value);
}

function wirePutRun(w, run, seg){
  w._run = run.map(x => ({...x}));
  w.value = wireRunValue(run);
  if(seg !== undefined) w._seg = seg;
}

const wireRunValue = run => run
  .filter(s => wireParts(s.value).length)
  .map((s, i) => s.value + (i && s.where ? " @ " + s.where : "")).join(" > ");

/* A value is a LIST of wires. A kickstand switch has two; recorded one each
   they become two specs, or two "alternates" competing when both are correct.
   One wire is a list of one, so everything written before this still reads. */
function wireSet(value){
  return String(value || "").split(",").map(c => c.trim()).filter(Boolean)
    .map(chunk => {
      const i = chunk.indexOf(":");
      return i === -1
        ? { role: "", value: chunk.trim() }
        : { role: chunk.slice(0, i).trim(), value: chunk.slice(i + 1).trim() };
    });
}
const wireSetValue = set => set
  .filter(w => wireParts(w.value).length)
  .map(w => (w.role ? w.role + ": " : "") + w.value).join(", ");

function wireAbbr(value){
  return wireParts(value).map(k => (WIRE.byKey[k] || {}).abbr || k).join("/");
}

function wireName(value){
  const p = wireParts(value);
  if(!p.length) return "";
  const nm = k => (WIRE.byKey[k] || {}).name || k;
  return nm(p[0]) + p.slice(1).map(k => " / " + nm(k).toLowerCase() + " stripe").join("");
}

/* One jacket, up to two stripes, and a bit of copper at the cut end so it
   reads as a wire rather than a colour chip. */
function wireSVG(value, id){
  const p = wireParts(value);
  if(!p.length) return "";
  const base = WIRE.byKey[p[0]];
  if(!base) return "";
  const W = 200, H = 44, jacket = 150;
  const outline = base.light ? "rgba(0,0,0,.45)" : "rgba(255,255,255,.22)";
  const cp = "wcp" + id;
  let s = `<svg class="wire-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(wireName(value))}">`;
  s += `<defs><clipPath id="${cp}"><rect x="2" y="8" width="${jacket}" height="28" rx="14"/></clipPath></defs>`;
  s += `<rect x="2" y="8" width="${jacket}" height="28" rx="14" fill="${base.hex}" stroke="${outline}" stroke-width="1.5"/>`;
  if(p[1]) s += `<rect x="0" y="${p[2] ? 15 : 19}" width="${jacket + 4}" height="6" fill="${(WIRE.byKey[p[1]]||{}).hex}" clip-path="url(#${cp})"/>`;
  if(p[2]) s += `<rect x="0" y="24" width="${jacket + 4}" height="6" fill="${(WIRE.byKey[p[2]]||{}).hex}" clip-path="url(#${cp})"/>`;
  s += `<rect x="10" y="11" width="${jacket - 16}" height="4" rx="2" fill="rgba(255,255,255,.28)"/>`;
  for(let i = 0; i < 7; i++){
    const y = 13 + i * 3;
    s += `<rect x="${jacket - 2}" y="${y}" width="${44 - Math.abs(i - 3) * 3}" height="2.2" rx="1.1" fill="${i % 2 ? "#C97A3A" : "#E39A55"}"/>`;
  }
  return s + "</svg>";
}

/* How a wire colour appears in a value line: one row per wire, so a two-wire
   switch reads as two wires rather than one run-on string. */
/* One wire, however many colours it wears along the way. The segments after
   the first are drawn behind an arrow and carry where the change happens,
   because "it turns blue/red" is useless without "at the connector under the
   seat". */
function wireOneHTML(value, id, opts = {}){
  const run = wireRun(value);
  const seg = (s, i) => `
    <span class="wire">
      ${i === 0 && opts.n ? `<span class="wire-n">${opts.n}</span>` : ""}
      ${wireSVG(s.value, id + "_s" + i)}
      <span class="wire-abbr">${esc(wireAbbr(s.value))}</span>
      ${i === 0 && opts.role ? `<span class="wire-role">${esc(opts.role)}</span>` : ""}
      <span class="wire-name">${esc(wireName(s.value))}</span>
    </span>`;
  if(run.length < 2) return seg(run[0] || { value, where: "" }, 0);
  return `<span class="wire-run">${run.map((s, i) => (i === 0 ? "" : `
      <span class="wire-change" title="This wire changes colour here — same wire, same circuit">
        <span class="arrow">&#8595;</span>${s.where
          ? `<span class="at">at ${esc(s.where)}</span>`
          : `<span class="at">changes colour</span>`}
      </span>`) + seg(s, i)).join("")}</span>`;
}

function wireValueHTML(value, id){
  const set = wireSet(value);
  if(!set.length || !wireParts(set[0].value).length) return esc(String(value || ""));
  // A wire that changes colour is bracketed on the page, so two wires that
  // both change cannot be read as one wire with four colours.
  const cls = w => "wire-one" + (wireRun(w.value).length > 1 ? " changes" : "");
  if(set.length === 1 && !set[0].role){
    return `<span class="${cls(set[0])}">${wireOneHTML(set[0].value, id)}</span>`;
  }
  return `<span class="wire-set">${set.map((w, i) =>
    `<span class="${cls(w)}">${wireOneHTML(w.value, id + "_" + i,
      { n: i + 1, role: w.role })}</span>`).join("")}</span>`;
}

/* ---- the picker ---------------------------------------------------------
   Free text is what this exists to prevent, so there is no colour text input:
   the only way to name a colour is to choose one that exists.

   The picker holds the whole SET. Swatches edit one wire at a time — the one
   marked current — and "Add another wire" appends an empty one and moves to it,
   which is the order somebody actually works in: get the first wire right, then
   realise there is a second. */
function wirePickerHTML(id, current, cur){
  // An array is passed straight through. Round-tripping via the string would
  // drop a wire that has been added but not yet coloured — which is exactly
  // the state "Add another wire" creates, so the new wire vanished and the
  // next swatch edited the previous one instead.
  const set = Array.isArray(current) ? current.map(w => ({...w}))
                                     : wireSet(current);
  if(!set.length) set.push({role: "", value: ""});
  let idx = cur === undefined ? set.length - 1 : Math.min(cur, set.length - 1);
  // Within the current wire, the swatches edit one SEGMENT: a wire that
  // changes colour at a connector is still one wire, and the picker follows
  // it along rather than making the rider file two specs.
  const run = wireRunOf(set[idx]);
  if(!run.length) run.push({value: "", where: ""});
  let seg = Math.min(Number(set[idx]._seg ?? run.length - 1), run.length - 1);
  if(seg < 0) seg = 0;
  const p = wireParts(run[seg].value);
  const multi = set.length > 1;

  const row = (field, chosen, allowNone, label, hint) => `
    <div class="wp-row" data-field="${field}">
      <div class="wp-label">${esc(label)}${hint ? ` <em>${esc(hint)}</em>` : ""}</div>
      <div class="wp-swatches">
        ${allowNone ? `<button type="button" class="wp-sw none${!chosen ? " on" : ""}"
            data-wire-pick="${id}" data-field="${field}" data-key=""
            title="No stripe"><span class="wp-chip"></span><span class="wp-k">none</span></button>` : ""}
        ${WIRE.colors.map(c => `
          <button type="button" class="wp-sw${chosen === c.key ? " on" : ""}"
                  data-wire-pick="${id}" data-field="${field}" data-key="${esc(c.key)}"
                  title="${esc(c.name)}">
            <span class="wp-chip" style="background:${c.hex}"></span>
            <span class="wp-k">${esc(c.abbr)}</span>
          </button>`).join("")}
      </div>
    </div>`;

  // The list only appears once there is more than one wire. On the common
  // single-wire spec it would be a row of chrome around one item.
  const list = multi ? `
    <div class="wp-list">
      ${set.map((w, i) => `
        <div class="wp-item${i === idx ? " on" : ""}" data-wire-go="${id}" data-i="${i}">
          <span class="wp-i">${i + 1}</span>
          ${wireParts(w.value).length
            ? wireSVG(w.value, id + "_l" + i) +
              `<span class="wire-abbr">${esc(wireAbbr(w.value))}</span>`
            : `<span class="wp-empty-item">not chosen yet</span>`}
          <input class="wp-role" type="text" maxlength="${WIRE.max_role || 40}"
                 placeholder="what it goes to (optional)" value="${esc(w.role)}"
                 data-wire-role="${id}" data-i="${i}"
                 onclick="event.stopPropagation()">
          <button type="button" class="wp-del" data-wire-del="${id}" data-i="${i}"
                  title="Remove this wire">×</button>
        </div>`).join("")}
    </div>` : "";

  // Shown once a wire has more than one colour, or as the single button that
  // says a wire CAN have more than one -- which is the thing riders do not
  // know they are allowed to record.
  const segs = `
    <div class="wp-segs">
      ${run.length > 1 ? run.map((sg, i) => `
        <button type="button" class="wp-seg${i === seg ? " on" : ""}"
                data-wire-seg="${id}" data-i="${i}">
          ${i ? `<span class="wp-seg-arrow">&#8594;</span>` : ""}
          ${wireParts(sg.value).length
            ? wireSVG(sg.value, id + "_g" + i) +
              `<span class="wire-abbr">${esc(wireAbbr(sg.value))}</span>`
            : `<span class="wp-empty-item">not chosen</span>`}
        </button>`).join("") : ""}
      <button type="button" class="wp-seg-add" data-wire-seg-add="${id}"
              ${run.length >= (WIRE.max_segments || 4) || !p.length ? "disabled" : ""}
              title="The same wire, a different colour after a connector">
        + colour change
      </button>
      ${run.length > 1 ? `<button type="button" class="wp-del"
          data-wire-seg-del="${id}" data-i="${seg}"
          title="Drop this length of the run">×</button>` : ""}
    </div>
    ${seg > 0 ? `<div class="wp-where">
        <label>Where it changes</label>
        <input type="text" maxlength="${WIRE.max_where || 60}"
               placeholder="e.g. 6-pin connector under the seat"
               value="${esc(run[seg].where || "")}"
               data-wire-where="${id}" data-i="${seg}">
      </div>` : ""}`;

  return `
    <div class="wire-picker" id="wp-${id}"
         data-value="${esc(wireSetValue(set))}"
         data-set="${esc(JSON.stringify(set))}" data-cur="${idx}" data-seg="${seg}">
      ${list}
      ${segs}
      <div class="wp-preview">${wirePreviewHTML(p, multi ? idx + 1 : 0)}</div>
      ${row("base", p[0] || "", false, "Base color")}
      ${row("s1", p[1] || "", true, "Stripe", "optional")}
      ${p[1] ? row("s2", p[2] || "", true, "Second stripe", "rare") : ""}
      <div class="wp-actions">
        <button type="button" class="wp-add" data-wire-add="${id}"
                ${set.length >= (WIRE.max_wires || 8) || !p.length ? "disabled" : ""}>
          + Add another wire
        </button>
        <span class="wp-note">A switch with two wires is one spec, not two.
          Add the second here rather than proposing it as a competing value.</span>
      </div>
    </div>`;
}

function wirePreviewHTML(parts, n){
  if(!parts.length || !parts[0]){
    return `<div class="wp-empty">${n ? `Wire ${n} — pick` : "Pick"} the base colour to start</div>`;
  }
  const v = parts.join("/");
  return `${wireSVG(v, "pv" + (n || ""))}
    <div>${n ? `<div class="wp-which">Wire ${n}</div>` : ""}
      <div class="wp-big">${esc(wireAbbr(v))}</div>
      <div class="wp-desc">${esc(wireName(v))}</div></div>`;
}

/* Reads the picker's state back out. Wires with no colour chosen are dropped,
   so an "Add another wire" the user thought better of does not block the save. */
function wirePickerValue(id){
  const box = byId("wp-" + id);
  return box ? wireSetValue(wireSet(box.dataset.value)) : "";
}

function wireCurrentSet(box){
  try {
    const raw = JSON.parse(box.dataset.set || "[]");
    if(Array.isArray(raw) && raw.length) return raw;
  } catch(e){ /* fall through to the string form */ }
  return wireSet(box.dataset.value);
}

function wireRedraw(box, set, cur){
  box.outerHTML = wirePickerHTML(box.id.slice(3), set, cur);
}

/* One delegated handler for every picker on the page. */
document.addEventListener("click", (e) => {
  const box = id => byId("wp-" + id);

  const go = e.target.closest("[data-wire-go]");
  if(go){
    const b = box(go.dataset.wireGo);
    if(b) wireRedraw(b, wireCurrentSet(b), Number(go.dataset.i));
    return;
  }

  const segGo = e.target.closest("[data-wire-seg]");
  if(segGo){
    const b = box(segGo.dataset.wireSeg);
    if(!b) return;
    const set = wireCurrentSet(b);
    const cur = Number(b.dataset.cur) || 0;
    if(set[cur]) set[cur]._seg = Number(segGo.dataset.i);
    wireRedraw(b, set, cur);
    return;
  }

  const segAdd = e.target.closest("[data-wire-seg-add]");
  if(segAdd){
    const b = box(segAdd.dataset.wireSegAdd);
    if(!b) return;
    const set = wireCurrentSet(b);
    const cur = Number(b.dataset.cur) || 0;
    const run = wireRunOf(set[cur]);
    run.push({value: "", where: ""});
    // The new length has no colour yet, so it is not written into the value
    // until one is picked -- but the picker has to move to it regardless,
    // which is why the run is carried as an array and not re-read from the
    // string it would have vanished from.
    wirePutRun(set[cur], run, run.length - 1);
    wireRedraw(b, set, cur);
    return;
  }

  const segDel = e.target.closest("[data-wire-seg-del]");
  if(segDel){
    const b = box(segDel.dataset.wireSegDel);
    if(!b) return;
    const set = wireCurrentSet(b);
    const cur = Number(b.dataset.cur) || 0;
    const run = wireRunOf(set[cur]);
    if(run.length <= 1) return;
    run.splice(Number(segDel.dataset.i), 1);
    wirePutRun(set[cur], run, Math.min(Number(segDel.dataset.i), run.length - 1));
    wireRedraw(b, set, cur);
    return;
  }

  const add = e.target.closest("[data-wire-add]");
  if(add){
    const b = box(add.dataset.wireAdd);
    if(!b) return;
    const set = wireCurrentSet(b);
    set.push({role: "", value: ""});
    wireRedraw(b, set, set.length - 1);
    return;
  }

  const del = e.target.closest("[data-wire-del]");
  if(del){
    const b = box(del.dataset.wireDel);
    if(!b) return;
    const set = wireCurrentSet(b);
    if(set.length <= 1) return;
    set.splice(Number(del.dataset.i), 1);
    wireRedraw(b, set, Math.min(Number(del.dataset.i), set.length - 1));
    return;
  }

  const btn = e.target.closest("[data-wire-pick]");
  if(!btn) return;
  const b = box(btn.dataset.wirePick);
  if(!b) return;

  const set = wireCurrentSet(b);
  const cur = Number(b.dataset.cur) || 0;
  if(!set[cur]) set[cur] = {role: "", value: ""};
  const run = wireRunOf(set[cur]);
  if(!run.length) run.push({value: "", where: ""});
  const sgi = Math.min(Number(b.dataset.seg) || 0, run.length - 1);
  const parts = wireParts(run[sgi].value);
  const idx = {base: 0, s1: 1, s2: 2}[btn.dataset.field];
  const key = btn.dataset.key;

  if(!key){
    // Dropping a stripe drops the one under it too: a second stripe with no
    // first is not a wire anybody's diagram shows.
    parts.length = idx;
  } else {
    while(parts.length < idx) parts.push(parts[parts.length - 1] || key);
    parts[idx] = key;
    // A stripe the same colour as its jacket is invisible on the bike and
    // meaningless in the value.
    for(let i = 0; i < parts.length; i++){
      for(let j = 0; j < i; j++) if(parts[i] === parts[j]) parts.splice(i--, 1);
    }
  }
  run[sgi].value = parts.filter(Boolean).join("/");
  wirePutRun(set[cur], run, sgi);
  wireRedraw(b, set, cur);
});

/* Where a wire changes colour is typed, so it is read back on input. It is
   deliberately NOT a redraw: rebuilding the picker on every keystroke would
   take the caret with it. */
document.addEventListener("input", (e) => {
  const w = e.target.closest("[data-wire-where]");
  if(w){
    const b = byId("wp-" + w.dataset.wireWhere);
    if(!b) return;
    const set = wireCurrentSet(b);
    const cur = Number(b.dataset.cur) || 0;
    const run = wireRunOf(set[cur]);
    const i = Number(w.dataset.i);
    if(run[i]){
      run[i].where = w.value;
      wirePutRun(set[cur], run, i);
      b.dataset.set = JSON.stringify(set);
      b.dataset.value = wireSetValue(set);
    }
    return;
  }

  const inp = e.target.closest("[data-wire-role]");
  if(!inp) return;
  const b = byId("wp-" + inp.dataset.wireRole);
  if(!b) return;
  const set = wireCurrentSet(b);
  const i = Number(inp.dataset.i);
  if(!set[i]) return;
  // Commas and colons are the separators, so they cannot appear inside a role.
  set[i].role = inp.value.replace(/[,:]/g, " ").slice(0, WIRE.max_role || 40);
  b.dataset.value = wireSetValue(set);
  b.dataset.set = JSON.stringify(set);
});
