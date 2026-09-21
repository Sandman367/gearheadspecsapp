/* Minimum fuel octane and max ethanol.

   One octane field, not two: "US grade" and "European grade" are two regional
   expressions of one fact. The picker offers the closed set of real pump
   grades, in whichever system the manual used, and the page shows the other
   region's equivalent alongside — looked up from published pairings, never
   computed, because AKI cannot be derived from RON without a number no manual
   publishes.

   Ethanol is a separate field with the opposite shape (a ceiling, not a
   floor), but the two are entered together: when you set the octane, the
   ethanol picker is right there, and saving writes both. */

let FUEL = null;   // {grades:[...], ethanol:[...], systems:[...]}

async function loadFuelVocabulary(){
  if(FUEL) return FUEL;
  FUEL = await API.get("/api/fuel-octane");
  FUEL.byKey = Object.fromEntries(FUEL.grades.map(g => [g.key, g]));
  FUEL.ethanolByKey = Object.fromEntries(FUEL.ethanol.map(e => [e.key, e]));
  return FUEL;
}

/* ---- display ------------------------------------------------------------ */

/* "US · AKI" / "Europe · RON": the scale is shown with the place it is the
   pump number in, because "AKI" on its own means nothing to most riders and
   "RON" reads as a typo. The equivalence is worded the same way. */
/* "87 Regular (AKI)" -> "Regular": the number and the scale are shown on
   their own, so the label carries only the name. */
function gradeName(g){
  return g.label.replace(/\s*\(.*\)\s*$/, "").replace(/^\d+\s*/, "");
}

function systemLabel(key){
  const s = (FUEL.systems || []).find(x => x.key === key);
  return s ? s.label : key;
}

/* `alt` drops the minimum/maximum tag: the manual's value is a floor or a
   ceiling, an alternate is just what somebody runs. */
function octaneValueHTML(value, opts = {}){
  const g = FUEL.byKey[String(value || "").trim().toUpperCase()];
  if(!g) return esc(String(value || ""));
  const eq = g.equivalent;
  const other = g.system === "AKI" ? "RON" : "AKI";
  const eqText = eq ? esc(eq.text.replace(/\s*(AKI|RON)\s*$/, "")) + ` <span class="octane-sys">${esc(systemLabel(other))}</span>` : "";
  return `<span class="octane">
      <span class="octane-stated"><b>${esc(String(g.value))}</b> <span class="octane-sys">${esc(systemLabel(g.system))}</span>
        <span class="octane-lbl">${esc(gradeName(g))}</span></span>
      ${eq ? `<span class="octane-eq ${eq.method}" title="${
          eq.method === "published"
            ? "A published pairing between the two pump scales."
            : "An estimate from AKI ≈ RON − 5. No published pairing exists for this grade."}">
          ≈ ${eqText}${eq.method === "formula" ? `<em>estimate</em>` : ""}
        </span>` : ""}
      ${opts.alt ? "" : `<span class="octane-min">minimum</span>`}
    </span>`;
}

function ethanolValueHTML(value, opts = {}){
  const e = FUEL.ethanolByKey[String(value || "").trim().toUpperCase()];
  if(!e) return esc(String(value || ""));
  return `<span class="ethanol"><b>${esc(e.key)}</b>
      <span class="octane-lbl">${esc(e.label.replace(/^E\d+\s*—\s*/, ""))}</span>
      ${opts.alt ? "" : `<span class="octane-min">maximum</span>`}</span>`;
}

/* ---- the picker --------------------------------------------------------- */

/* `withEthanol` puts the ethanol choice in the same box, so setting the octane
   from a manual page means setting both in one go. The two still save to
   their own fields. */
function fuelPickerHTML(id, current, opts = {}){
  const cur = String(current || "").toUpperCase();
  const curSys = cur.split(":")[0] || (opts.defaultSystem || "AKI");
  const grades = FUEL.grades.filter(g => g.system === curSys);
  const common = grades.filter(g => g.is_common), rare = grades.filter(g => !g.is_common);

  const grade = g => `
    <button type="button" class="fp-grade${g.key === cur ? " on" : ""}${g.is_common ? "" : " rare"}"
            data-fuel-pick="${id}" data-key="${esc(g.key)}" title="${esc(g.region_note)}">
      <span class="fp-num">${esc(String(g.value))}</span>
      <span class="fp-name">${esc(gradeName(g))}</span>
      ${g.equivalent ? `<span class="fp-eq ${g.equivalent.method}">≈ ${
          esc(g.equivalent.text.replace(/\s*(AKI|RON)\s*$/, ""))} ${esc(systemLabel(g.system === "AKI" ? "RON" : "AKI"))}</span>` : ""}
    </button>`;

  return `
    <div class="fuel-picker" id="fp-${id}" data-value="${esc(cur)}" data-system="${esc(curSys)}"
         data-ethanol="${esc((opts.ethanol || "").toUpperCase())}">
      <div class="fp-row">
        <div class="fp-label">Which pump scale does the manual use?</div>
        <div class="fp-systems">
          ${FUEL.systems.map(sy => `
            <button type="button" class="fp-sys${sy.key === curSys ? " on" : ""}"
                    data-fuel-sys="${id}" data-key="${esc(sy.key)}" title="${esc(sy.note)}">${esc(sy.label)}</button>`).join("")}
        </div>
      </div>
      <div class="fp-row">
        <div class="fp-label">Minimum grade the manual names</div>
        <div class="fp-grades">${common.map(grade).join("")}</div>
        ${rare.length ? `<div class="fp-sub">Regional and less common</div>
          <div class="fp-grades">${rare.map(grade).join("")}</div>` : ""}
      </div>
      ${opts.withEthanol ? `
      <div class="fp-row fp-ethanol">
        <div class="fp-label">Ethanol the fuel system tolerates <em>— saved to Max Ethanol</em></div>
        <div class="fp-grades">
          ${FUEL.ethanol.map(e => `
            <button type="button" class="fp-grade${e.key === (opts.ethanol || "").toUpperCase() ? " on" : ""}"
                    data-fuel-eth="${id}" data-key="${esc(e.key)}" title="${esc(e.note)}">
              <span class="fp-num">${esc(e.key)}</span>
              <span class="fp-name">${esc(e.label.replace(/^E\d+\s*—\s*/, ""))}</span>
            </button>`).join("")}
        </div>
      </div>` : ""}
      <div class="fp-note">The stored value is what the manual says, in the scale
        it says it in. The other scale is shown as an equivalence — a published
        pairing where one exists, otherwise an estimate and marked as one.</div>
    </div>`;
}

/* Ethanol on its own, for the Max Ethanol row and its alternates. */
function fuelPickerEthanolOnlyHTML(id, current){
  const cur = String(current || "").toUpperCase();
  return `
    <div class="fuel-picker" id="fp-${id}" data-value="" data-system="" data-ethanol="${esc(cur)}">
      <div class="fp-row fp-ethanol">
        <div class="fp-label">Most ethanol the fuel system tolerates</div>
        <div class="fp-grades">
          ${FUEL.ethanol.map(e => `
            <button type="button" class="fp-grade${e.key === cur ? " on" : ""}"
                    data-fuel-eth="${id}" data-key="${esc(e.key)}" title="${esc(e.note)}">
              <span class="fp-num">${esc(e.key)}</span>
              <span class="fp-name">${esc(e.label.replace(/^E\d+\s*—\s*/, ""))}</span>
            </button>`).join("")}
        </div>
      </div>
      <div class="fp-note">A ceiling, not a target: E0 means ethanol-free fuel
        only, usually because the lines and carb parts are original rubber.</div>
    </div>`;
}

function fuelPickerValue(id){
  const box = byId("fp-" + id);
  return box ? box.dataset.value : "";
}
function fuelPickerEthanol(id){
  const box = byId("fp-" + id);
  return box ? box.dataset.ethanol : "";
}

document.addEventListener("click", (e) => {
  const sys = e.target.closest("[data-fuel-sys]");
  if(sys){
    const box = byId("fp-" + sys.dataset.fuelSys);
    if(!box) return;
    // Switching scale clears the grade: 91 AKI and 91 RON are different fuels.
    const withEth = !!box.querySelector(".fp-ethanol");
    box.outerHTML = fuelPickerHTML(sys.dataset.fuelSys, "", {
      defaultSystem: sys.dataset.key, withEthanol: withEth, ethanol: box.dataset.ethanol });
    return;
  }
  const pick = e.target.closest("[data-fuel-pick]");
  if(pick){
    const box = byId("fp-" + pick.dataset.fuelPick);
    if(!box) return;
    const withEth = !!box.querySelector(".fp-ethanol");
    box.outerHTML = fuelPickerHTML(pick.dataset.fuelPick, pick.dataset.key, {
      withEthanol: withEth, ethanol: box.dataset.ethanol });
    return;
  }
  const eth = e.target.closest("[data-fuel-eth]");
  if(eth){
    const box = byId("fp-" + eth.dataset.fuelEth);
    if(!box) return;
    const next = box.dataset.ethanol === eth.dataset.key ? "" : eth.dataset.key;
    // An ethanol-only picker has no grade scale; redraw it as itself.
    box.outerHTML = box.dataset.system
      ? fuelPickerHTML(eth.dataset.fuelEth, box.dataset.value, {
          defaultSystem: box.dataset.system, withEthanol: true, ethanol: next })
      : fuelPickerEthanolOnlyHTML(eth.dataset.fuelEth, next);
  }
});
