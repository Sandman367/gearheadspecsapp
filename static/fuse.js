/* Fuses: drawn, not described.
 *
 * A blade fuse is colour coded by its rating, so the page can show the rider
 * the part they are looking for instead of spelling it out. The colour comes
 * from the SIZE and the rating together -- a violet regular blade is 3A and a
 * violet Maxi is 100A -- which is why the server sends the colour with the
 * value rather than the page working it out from the number.
 */
let FUSE = null;   // {families:[...], colors:[...], max_fuses, max_role}

async function loadFuseVocabulary(){
  if(FUSE) return FUSE;
  FUSE = await API.get("/api/fuses");
  FUSE.byKey = Object.fromEntries(FUSE.families.map(f => [f.key, f]));
  return FUSE;
}

function fuseSet(value){
  return String(value || "").split(",").map(c => c.trim()).filter(Boolean)
    .map(chunk => {
      const i = chunk.indexOf(":");
      return i === -1
        ? { role: "", value: chunk.trim() }
        : { role: chunk.slice(0, i).trim(), value: chunk.slice(i + 1).trim() };
    });
}
const fuseSetValue = set => set
  .filter(f => f.value && f.value.trim())
  .map(f => (f.role ? f.role + ": " : "") + f.value.trim()).join(", ");

/* "mini 10A" -> {family, amp, spec}. The vocabulary is the authority on what
   colour that is, so nothing here guesses one. */
function fuseParts(value){
  const words = String(value || "").split(/\s+/).filter(Boolean);
  let family = null, amp = null;
  for(const w of words){
    const n = parseFloat(w.replace(/a$/i, ""));
    if(!isNaN(n) && /^[\d.]+a?$/i.test(w)) amp = n;
    else family = (w || "").toLowerCase();
  }
  const f = FUSE && FUSE.byKey[family];
  const row = f && f.amps.find(x => x.amp === amp);
  return { family, amp, f, row };
}

/* The part, at the shape and colour it really is, with its rating printed on
   it the way the real one is. A rider is matching what is in their hand, so
   the drawing does the matching: the number is on the fuse, not only beside
   it.

   Clear is not "no colour": a 25A blade and a glass fuse really are
   translucent, so both get the glass treatment rather than being left blank. */
/* The chart's own photograph where there is one, the drawn fuse where there
   is not -- the chart does not cover every Maxi rating or the longer torpedo
   series, and those are real parts that should still appear. */
function fuseArt(value, id){
  const { row } = fuseParts(value);
  if(row && row.img){
    return `<span class="fuse-pic"><img src="${esc(row.img)}" alt="" loading="lazy"></span>`;
  }
  return fuseSVG(value, id);
}

function fuseSVG(value, id){
  const { f, row } = fuseParts(value);
  const shape = (f && f.shape) || "blade";
  const legs = (f && f.legs) || 2;
  const hex = row && row.hex;
  const clear = !hex || row.color === "clear";
  const body = clear ? "#D8D8D2" : hex;
  const gid = "fg" + String(id).replace(/[^\w]/g, "");
  const txt = row ? row.text.replace(/A$/, "") : "";
  // Dark jackets need light type on them and pale ones need dark.
  const ink = clear || ["yellow", "tan", "pink", "clear", "gray"].includes(row && row.color)
    ? "rgba(0,0,0,0.72)" : "rgba(255,255,255,0.95)";

  const shade = `<linearGradient id="${gid}" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="${body}" stop-opacity="${clear ? 0.55 : 1}"/>
      <stop offset="0.42" stop-color="#FFFFFF" stop-opacity="0.3"/>
      <stop offset="1" stop-color="${body}" stop-opacity="${clear ? 0.55 : 1}"/>
    </linearGradient>`;
  const cap = (x) => `<rect x="${x}" y="4" width="7" height="12" rx="1.5"
      fill="#B9BCC2" stroke="#7E8189" stroke-width="0.7"/>`;

  if(shape === "glass"){
    // Clear body, the element visible through it, metal caps at both ends.
    return `<svg class="fuse-svg" viewBox="0 0 46 20" width="46" height="20" aria-hidden="true">
      <rect x="7" y="4" width="32" height="12" rx="1.5"
            fill="rgba(226,226,220,0.35)" stroke="#8A8A84" stroke-width="0.8"/>
      ${cap(1)}${cap(38)}
      <path d="M10 10 h6 l2 -3 l2 6 l2 -6 l2 6 l2 -3 h6" fill="none"
            stroke="#9A7B4A" stroke-width="1.1"/>
      <text x="23" y="13.5" text-anchor="middle" font-size="8"
            font-family="var(--mono)" fill="rgba(0,0,0,0.55)">${esc(txt)}</text>
    </svg>`;
  }

  if(shape === "sfe"){
    // White ceramic, a coloured band around the middle that carries the rating.
    return `<svg class="fuse-svg" viewBox="0 0 46 20" width="46" height="20" aria-hidden="true">
      <rect x="7" y="4" width="32" height="12" rx="1.5"
            fill="#EFEFE9" stroke="#A9A9A2" stroke-width="0.8"/>
      <rect x="15" y="4" width="16" height="12" fill="${body}"
            fill-opacity="${clear ? 0.35 : 1}"/>
      ${cap(1)}${cap(38)}
      <text x="23" y="13.5" text-anchor="middle" font-size="8"
            font-family="var(--mono)" fill="${ink}">${esc(txt)}</text>
    </svg>`;
  }

  if(shape === "torpedo"){
    // Pointed at both ends, which is the whole way you recognise one.
    return `<svg class="fuse-svg" viewBox="0 0 46 20" width="46" height="20" aria-hidden="true">
      <defs>${shade}</defs>
      <path d="M2 10 L11 4.5 H35 L44 10 L35 15.5 H11 Z"
            fill="url(#${gid})" stroke="#7E8189" stroke-width="0.8"/>
      <path d="M2 10 L11 4.5 V15.5 Z M44 10 L35 4.5 V15.5 Z" fill="#B9BCC2" stroke="#7E8189" stroke-width="0.6"/>
      <text x="23" y="13.2" text-anchor="middle" font-size="8"
            font-family="var(--mono)" fill="${ink}">${esc(txt)}</text>
    </svg>`;
  }

  if(shape === "case"){
    // A box cartridge: taller than a blade, rating on the top face.
    return `<svg class="fuse-svg" viewBox="0 0 30 24" width="30" height="24" aria-hidden="true">
      <defs>${shade}</defs>
      <rect x="2" y="1.5" width="26" height="9" rx="1.5"
            fill="${body}" fill-opacity="${clear ? 0.6 : 0.85}" stroke="rgba(0,0,0,0.4)" stroke-width="0.8"/>
      <rect x="2" y="8" width="26" height="10" rx="1.5"
            fill="url(#${gid})" stroke="rgba(0,0,0,0.45)" stroke-width="0.9"/>
      <rect x="7" y="17" width="4" height="6" rx="0.6" fill="#C9A15E" stroke="#8A6A32" stroke-width="0.6"/>
      <rect x="19" y="17" width="4" height="6" rx="0.6" fill="#C9A15E" stroke="#8A6A32" stroke-width="0.6"/>
      <text x="15" y="8.6" text-anchor="middle" font-size="8"
            font-family="var(--mono)" fill="${ink}">${esc(txt)}</text>
    </svg>`;
  }

  // A blade. The rating is moulded into the top, the element shows through a
  // window, and the legs are what tells a Micro3 from the rest.
  const w = legs === 3 ? 34 : 28;
  const at = legs === 3 ? [8, 17, 26] : legs === 1 ? [17] : [10, 21];
  return `<svg class="fuse-svg" viewBox="0 0 ${w} 24" width="${w}" height="24" aria-hidden="true">
    <defs>${shade}</defs>
    ${at.map(x => `<rect x="${x - 2}" y="15" width="4" height="8" rx="0.6"
        fill="#C9A15E" stroke="#8A6A32" stroke-width="0.6"/>`).join("")}
    <rect x="1.5" y="1.5" width="${w - 3}" height="15" rx="2.5"
          fill="url(#${gid})" stroke="rgba(0,0,0,0.45)" stroke-width="1"/>
    <rect x="${w / 2 - 5}" y="7.5" width="10" height="7" rx="1"
          fill="rgba(255,255,255,0.3)" stroke="rgba(0,0,0,0.18)" stroke-width="0.5"/>
    <path d="M${w / 2 - 3.5} 11.5 l2 -2.5 l1.5 2.5 l2 -2.5" fill="none"
          stroke="rgba(0,0,0,0.5)" stroke-width="0.9"/>
    <text x="${w / 2}" y="6.6" text-anchor="middle" font-size="7"
          font-family="var(--mono)" fill="${ink}">${esc(txt)}</text>
  </svg>`;
}

function fuseName(value){
  const { f, row, amp } = fuseParts(value);
  if(!f) return String(value || "");
  const rating = row ? row.text : (amp ? amp + "A" : "");
  return `${f.name} ${rating}`.trim();
}

/* One fuse on the page: the part, its rating, its size, and what it protects.
   The rating leads because that is what a rider is matching. */
function fuseOneHTML(value, id, opts = {}){
  const { f, row } = fuseParts(value);
  if(!f) return esc(String(value || ""));
  return `
    <span class="fuse">
      ${opts.n ? `<span class="fuse-n">${opts.n}</span>` : ""}
      ${fuseArt(value, id)}
      <span class="fuse-amp">${esc(row ? row.text : "")}</span>
      <span class="fuse-family">${esc(f.name)}${
        row && row.color_name ? ` · ${esc(row.color_name)}` : ""}</span>
      ${opts.role ? `<span class="fuse-role">${esc(opts.role)}</span>` : `<span></span>`}
      ${f.codes ? `<span class="fuse-codes" title="What a manual or a parts bin may call this size">${esc(f.codes)}</span>` : `<span></span>`}
    </span>`;
}

function fuseValueHTML(value, id){
  const set = fuseSet(value);
  if(!set.length) return esc(String(value || ""));
  if(set.length === 1 && !set[0].role){
    return `<span class="fuse-one">${fuseOneHTML(set[0].value, id)}</span>`;
  }
  return `<span class="fuse-set">${set.map((x, i) =>
    `<span class="fuse-one">${fuseOneHTML(x.value, id + "_" + i,
      { n: i + 1, role: x.role })}</span>`).join("")}</span>`;
}

/* ---- the picker ----------------------------------------------------------
   Size first, then rating, because the ratings a size comes in depend on the
   size -- and offering 50A on a mini would be offering a part that does not
   exist. No colours show until a size is chosen; then each rating appears
   as the part itself, photo or drawing, so a rider picks the fuse that
   looks like the one in their hand -- and that picture is what the spec
   then shows. */
function fusePickerHTML(id, current, cur){
  const set = Array.isArray(current) ? current.map(x => ({...x})) : fuseSet(current);
  if(!set.length) set.push({role: "", value: ""});
  const idx = cur === undefined ? set.length - 1 : Math.min(cur, set.length - 1);
  const { family, amp } = fuseParts(set[idx].value);
  const fam = FUSE.byKey[family];
  const multi = set.length > 1;

  // What it protects belongs with the fuse you have just picked, not only
  // once a second one exists. It appears as soon as there is a fuse to
  // attach it to, which is the moment a rider knows the answer.
  const protects = (fam && amp) ? `
    <div class="wp-row">
      <div class="wp-label">What it protects</div>
      <input type="text" class="fp-protect" maxlength="${FUSE.max_role || 40}"
             placeholder="e.g. headlight, ignition, horn"
             value="${esc(set[idx].role || "")}"
             data-fuse-role="${id}" data-i="${idx}">
    </div>` : "";

  const list = multi ? `
    <div class="fp-list">
      ${set.map((x, i) => `
        <div class="fp-item${i === idx ? " on" : ""}" data-fuse-go="${id}" data-i="${i}">
          <span class="fp-i">${i + 1}</span>
          ${x.value ? fuseArt(x.value, id + "_l" + i) +
              `<span class="fuse-amp">${esc(fuseParts(x.value).row?.text || "")}</span>`
            : `<span class="fp-empty-item">not chosen yet</span>`}
          <input class="fp-role" type="text" maxlength="${FUSE.max_role || 40}"
                 placeholder="what it protects (optional)" value="${esc(x.role)}"
                 data-fuse-role="${id}" data-i="${i}" onclick="event.stopPropagation()">
          <button type="button" class="wp-del" data-fuse-del="${id}" data-i="${i}"
                  title="Remove this fuse">&times;</button>
        </div>`).join("")}
    </div>` : "";

  return `
    <div class="fuse-picker" id="fp-${id}"
         data-value="${esc(fuseSetValue(set))}"
         data-set="${esc(JSON.stringify(set))}" data-cur="${idx}">
      ${list}
      <div class="fp-preview">${
        fam && amp
          ? `${fuseArt(set[idx].value, "fpv" + id)}
             <div><div class="fp-big">${esc(fuseParts(set[idx].value).row?.text || "")}</div>
               <div class="fp-desc">${esc(fam.name)}${
                 fuseParts(set[idx].value).row?.color_name
                   ? " · " + esc(fuseParts(set[idx].value).row.color_name) : ""}</div>
               <div class="fp-mm">${esc(fam.codes)}${fam.mm ? ` · ${esc(fam.mm)} mm` : ""}</div></div>`
          : `<div class="wp-empty">Pick a size, then a rating</div>`}</div>

      <div class="wp-row">
        <div class="wp-label">Type</div>
        <div class="fp-fams">
          ${FUSE.families.map(f => `
            <button type="button" class="fp-fam${f.key === family ? " on" : ""}"
                    data-fuse-fam="${id}" data-key="${esc(f.key)}"
                    title="${esc(f.codes)}${f.mm ? ` — ${esc(f.mm)} mm` : ""}">
              ${esc(f.name)}</button>`).join("")}
        </div>
      </div>

      ${fam ? `<div class="wp-row">
        <div class="wp-label">Rating <em>what a ${esc(fam.name)} comes in</em></div>
        <div class="wp-swatches">
          ${fam.amps.map(a => `
            <button type="button" class="wp-sw fp-sw${a.amp === amp ? " on" : ""}"
                    data-fuse-amp="${id}" data-amp="${a.amp}"
                    title="${esc(a.text)}${a.color_name ? ` — ${esc(a.color_name)}` : " — not colour coded"}">
              <span class="wp-k">${esc(a.text)}</span>
              ${fuseArt(`${fam.key} ${a.amp}A`, id + "_sw" + String(a.amp).replace(".", "_"))}
            </button>`).join("")}
        </div>
      </div>` : ""}

      ${protects}

      <div class="wp-actions">
        <button type="button" class="wp-add" data-fuse-add="${id}"
                ${set.length >= (FUSE.max_fuses || 8) || !(fam && amp) ? "disabled" : ""}>
          + Add another fuse
        </button>
        <span class="wp-note">A row of fuses is one spec. Add them here rather
          than proposing each as a competing value.</span>
      </div>
    </div>`;
}

function fusePickerValue(id){
  const box = byId("fp-" + id);
  return box ? box.dataset.value : "";
}

function fuseCurrentSet(box){
  try {
    const raw = JSON.parse(box.dataset.set || "[]");
    if(Array.isArray(raw) && raw.length) return raw;
  } catch(e){ /* fall through */ }
  return fuseSet(box.dataset.value);
}

function fuseRedraw(box, set, cur){
  box.outerHTML = fusePickerHTML(box.id.slice(3), set, cur);
}

document.addEventListener("click", (e) => {
  const box = id => byId("fp-" + id);

  const go = e.target.closest("[data-fuse-go]");
  if(go){ const b = box(go.dataset.fuseGo);
    if(b) fuseRedraw(b, fuseCurrentSet(b), Number(go.dataset.i)); return; }

  const add = e.target.closest("[data-fuse-add]");
  if(add){ const b = box(add.dataset.fuseAdd); if(!b) return;
    const set = fuseCurrentSet(b); set.push({role: "", value: ""});
    fuseRedraw(b, set, set.length - 1); return; }

  const del = e.target.closest("[data-fuse-del]");
  if(del){ const b = box(del.dataset.fuseDel); if(!b) return;
    const set = fuseCurrentSet(b); if(set.length <= 1) return;
    set.splice(Number(del.dataset.i), 1);
    fuseRedraw(b, set, Math.min(Number(del.dataset.i), set.length - 1)); return; }

  const fam = e.target.closest("[data-fuse-fam]");
  if(fam){
    const b = box(fam.dataset.fuseFam); if(!b) return;
    const set = fuseCurrentSet(b);
    const cur = Number(b.dataset.cur) || 0;
    const key = fam.dataset.key;
    const { amp } = fuseParts(set[cur].value);
    // Keep the rating only if the new size actually comes in it. Changing
    // mini 3A to maxi must not leave a 3A maxi, which does not exist.
    const keeps = FUSE.byKey[key].amps.some(a => a.amp === amp);
    set[cur].value = keeps ? `${key} ${amp}A` : key;
    fuseRedraw(b, set, cur);
    return;
  }

  const ampBtn = e.target.closest("[data-fuse-amp]");
  if(ampBtn){
    const b = box(ampBtn.dataset.fuseAmp); if(!b) return;
    const set = fuseCurrentSet(b);
    const cur = Number(b.dataset.cur) || 0;
    const { family } = fuseParts(set[cur].value);
    if(!family) return;
    const a = FUSE.byKey[family].amps.find(x => x.amp === Number(ampBtn.dataset.amp));
    set[cur].value = `${family} ${a.text}`;
    fuseRedraw(b, set, cur);
    return;
  }
});

document.addEventListener("input", (e) => {
  const inp = e.target.closest("[data-fuse-role]");
  if(!inp) return;
  const b = byId("fp-" + inp.dataset.fuseRole);
  if(!b) return;
  const set = fuseCurrentSet(b);
  const i = Number(inp.dataset.i);
  if(set[i]){
    set[i].role = inp.value;
    b.dataset.set = JSON.stringify(set);
    b.dataset.value = fuseSetValue(set);
  }
});
