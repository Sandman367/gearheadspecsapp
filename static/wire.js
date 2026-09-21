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
function wireValueHTML(value, id){
  const set = wireSet(value);
  if(!set.length || !wireParts(set[0].value).length) return esc(String(value || ""));
  if(set.length === 1 && !set[0].role){
    const v = set[0].value;
    return `<span class="wire">${wireSVG(v, id)}`
         + `<span class="wire-abbr">${esc(wireAbbr(v))}</span>`
         + `<span class="wire-name">${esc(wireName(v))}</span></span>`;
  }
  return `<span class="wire-set">${set.map((w, i) => `
    <span class="wire">
      <span class="wire-n">${i + 1}</span>
      ${wireSVG(w.value, id + "_" + i)}
      <span class="wire-abbr">${esc(wireAbbr(w.value))}</span>
      ${w.role ? `<span class="wire-role">${esc(w.role)}</span>` : ""}
      <span class="wire-name">${esc(wireName(w.value))}</span>
    </span>`).join("")}</span>`;
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
  const p = wireParts(set[idx].value);
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

  return `
    <div class="wire-picker" id="wp-${id}"
         data-value="${esc(wireSetValue(set))}"
         data-set="${esc(JSON.stringify(set))}" data-cur="${idx}">
      ${list}
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
  const parts = wireParts(set[cur].value);
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
  set[cur].value = parts.filter(Boolean).join("/");
  wireRedraw(b, set, cur);
});

/* Roles are typed, so they are read back on input rather than on redraw. */
document.addEventListener("input", (e) => {
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
