// Hidable parameters panel. Renders curated SimParams sliders grouped by
// simulation component; each group is a collapsible <details>. The whole
// panel toggles via a button on the panel header.
//
// Sliders use the existing data-param hook in controls.ts — the input
// listener and refresh loop pick them up via querySelectorAll.

interface ParamSpec {
  key: string;
  label: string;
  tooltip: string;
  min: number;
  max: number;
  step: number;
  type?: "range" | "checkbox";
}

interface ParamGroup {
  title: string;
  params: ParamSpec[];
}

const GROUPS: ParamGroup[] = [
  {
    title: "Combat & flow",
    params: [
      { key: "base_rate", label: "Base rate", tooltip: "baseline progress per second on contested cells", min: 0, max: 3, step: 0.1 },
      { key: "pressure_coefficient", label: "Pressure coef", tooltip: "progress per (pressure-unit * second)", min: 0.01, max: 0.2, step: 0.01 },
      { key: "enemy_resistance_base", label: "Enemy resist", tooltip: "defender resistance magnitude per contested cell (supply-scaled at apply)", min: 0, max: 20, step: 0.5 },
      { key: "flip_threshold", label: "Flip threshold", tooltip: "progress magnitude required to capture a cell", min: 10, max: 500, step: 5 },
      { key: "repulse_threshold_ratio", label: "Repulse ratio", tooltip: "fraction of flip_threshold to repulse an incursion", min: 0.1, max: 1, step: 0.05 },
    ],
  },
  {
    title: "Supply",
    params: [
      { key: "supply_floor", label: "Supply floor", tooltip: "min effective supply factor (0..1)", min: 0, max: 1, step: 0.05 },
      { key: "supply_max_depth", label: "Max BFS depth", tooltip: "BFS depth at which defender supply hits 0", min: 1, max: 10, step: 1 },
      { key: "attacker_density_radius", label: "Attacker radius", tooltip: "SE same-faction neighbor count radius", min: 1, max: 4, step: 1 },
      { key: "fob_supply_bonus", label: "FOB bonus", tooltip: "added to attacker supply within FOB radius", min: 0, max: 1, step: 0.05 },
    ],
  },
  {
    title: "FOB & artillery",
    params: [
      { key: "fob_buff", label: "FOB buff", tooltip: "+rate to contested cells in FOB radius", min: 0, max: 20, step: 0.5 },
      { key: "fob_radius", label: "FOB radius", tooltip: "FOB area-of-effect radius (hex distance)", min: 1, max: 5, step: 1 },
      { key: "arty_buff", label: "Arty buff", tooltip: "+rate during artillery effect", min: 0, max: 30, step: 0.5 },
      { key: "arty_duration_s", label: "Arty duration", tooltip: "seconds of effect per shell", min: 1, max: 30, step: 0.5 },
      { key: "arty_range", label: "Arty range", tooltip: "max hex distance from artillery to firing target", min: 1, max: 8, step: 1 },
    ],
  },
  {
    title: "Fortress & nodes",
    params: [
      { key: "fortress_resist", label: "Fortress resist", tooltip: "added to enemy contribution within fortress radius", min: 0, max: 20, step: 0.5 },
      { key: "fortress_radius", label: "Fortress radius", tooltip: "fortress area of effect", min: 1, max: 6, step: 1 },
      { key: "fortress_siege_multiplier", label: "Siege mult", tooltip: "cells under fortress need this x progress to flip", min: 1, max: 4, step: 0.1 },
      { key: "node_resist", label: "Node resist", tooltip: "resistance node + neighbors", min: 0, max: 10, step: 0.5 },
    ],
  },
  {
    title: "Destroy salients",
    params: [
      { key: "salient_period_ticks", label: "Period (ticks)", tooltip: "strategic cadence", min: 10, max: 500, step: 10 },
      { key: "destroy_max_range", label: "Max range", tooltip: "hops from enemy front to target POI", min: 3, max: 15, step: 1 },
      { key: "salient_pressure_magnitude", label: "Pressure mag", tooltip: "offensive force stamped on corridor cells", min: 0, max: 500, step: 10 },
      { key: "destroy_salient_lifetime_s", label: "Lifetime (s)", tooltip: "seconds before a destroy salient expires", min: 10, max: 300, step: 5 },
    ],
  },
  {
    title: "Retaliation",
    params: [
      { key: "retaliation_gauge_threshold", label: "Gauge threshold", tooltip: "gauge value at which a conquer salient fires", min: 1, max: 20, step: 0.5 },
      { key: "retaliation_gauge_decay_per_tick", label: "Decay/tick", tooltip: "passive gauge decay each tick", min: 0, max: 2, step: 0.05 },
      { key: "retaliation_w_se_flip", label: "+SE flip", tooltip: "gauge += per SE capture", min: 0, max: 5, step: 0.1 },
      { key: "retaliation_w_enemy_flip", label: "-Enemy flip", tooltip: "gauge -= per enemy capture (clamped at 0)", min: 0, max: 5, step: 0.1 },
      { key: "conquer_pressure_magnitude", label: "Conquer mag", tooltip: "pressure stamped on conquer-salient patches", min: 0, max: 300, step: 10 },
    ],
  },
  {
    title: "Factories",
    params: [
      { key: "factory_period_ticks", label: "Period (ticks)", tooltip: "ticks between factory placement attempts", min: 10, max: 500, step: 10 },
      { key: "factory_soft_cap", label: "Soft cap", tooltip: "spawn freely below this count", min: 0, max: 10, step: 1 },
      { key: "factory_hard_cap", label: "Hard cap", tooltip: "never exceed this count", min: 0, max: 15, step: 1 },
      { key: "factory_radius", label: "Radius", tooltip: "max hex distance from factory to push", min: 1, max: 6, step: 1 },
      { key: "factory_pressure_magnitude", label: "Pressure mag", tooltip: "offensive force stamped on each active target", min: 0, max: 200, step: 5 },
    ],
  },
  {
    title: "Diver allocation",
    params: [
      { key: "diver_pool", label: "Pool", tooltip: "total SE force distributed each allocation pass", min: 0, max: 3000, step: 50 },
      { key: "allocation_temperature", label: "Temperature", tooltip: "softmax temperature; low concentrates, high spreads", min: 0.2, max: 3, step: 0.1 },
      { key: "diver_supply_max_hops", label: "Max hops", tooltip: "contested cells beyond this distance from SE-held cells are cut off", min: 0, max: 6, step: 1 },
      { key: "defense_priority_bias", label: "Defense bias", tooltip: "flat utility bonus on defensive contests so divers divert when SE is being pushed", min: 0, max: 10, step: 0.5 },
      { key: "allocation_chunk_count", label: "Chunk count", tooltip: "discrete deployment chunks per cycle (>1 introduces burst variance)", min: 1, max: 50, step: 1 },
    ],
  },
  {
    title: "High command",
    params: [
      { key: "high_command_enabled", label: "Enabled", tooltip: "if on, the SE planner places/moves FOBs and artillery from the requisition pool", min: 0, max: 1, step: 1, type: "checkbox" },
      { key: "requisition_per_tick", label: "Req/tick", tooltip: "smooth accrual rate of the build pool", min: 0, max: 5, step: 0.1 },
      { key: "fob_base_cost", label: "FOB base cost", tooltip: "base requisition cost for the first FOB; scales by (n+1)^exponent", min: 0, max: 500, step: 10 },
      { key: "arty_base_cost", label: "Arty base cost", tooltip: "base requisition cost for the first artillery; scales by (n+1)^exponent", min: 0, max: 500, step: 10 },
    ],
  },
];

export function renderParamsPanel(host: HTMLElement): void {
  const frag = document.createDocumentFragment();
  for (const group of GROUPS) {
    const details = document.createElement("details");
    details.className = "param-group";
    const summary = document.createElement("summary");
    summary.textContent = group.title;
    details.appendChild(summary);
    for (const p of group.params) {
      details.appendChild(renderParamRow(p));
    }
    frag.appendChild(details);
  }
  host.replaceChildren(frag);
}

function renderParamRow(p: ParamSpec): HTMLElement {
  const row = document.createElement("div");
  row.className = "row";

  const label = document.createElement("label");
  label.textContent = p.label;
  label.title = p.tooltip;
  row.appendChild(label);

  if (p.type === "checkbox") {
    const input = document.createElement("input");
    input.type = "checkbox";
    input.dataset.param = p.key;
    input.dataset.kind = "bool";
    row.appendChild(input);
    const display = document.createElement("span");
    display.dataset.show = p.key;
    display.textContent = "off";
    row.appendChild(display);
  } else {
    const input = document.createElement("input");
    input.type = "range";
    input.min = String(p.min);
    input.max = String(p.max);
    input.step = String(p.step);
    input.dataset.param = p.key;
    row.appendChild(input);
    const display = document.createElement("span");
    display.dataset.show = p.key;
    display.textContent = "--";
    row.appendChild(display);
  }
  return row;
}
