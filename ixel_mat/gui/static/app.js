// The Ixel window: a rail of views (Ask, Board, Machines, Health, Settings) and the page each one shows.
// Every view stays in the page while another is on screen, so a question keeps streaming while you
// look at the Board. A view's module may export shown() and hidden(), called as it comes and goes.

import { $, $$, stayPresent } from "./common.js";
import * as appearance from "./appearance.js";
import * as ask from "./ask.js";
import * as board from "./board.js";
import * as health from "./health.js";
import * as machines from "./machines.js";
import * as settings from "./settings.js";

const VIEWS = {
  ask: { title: "Ask", module: ask },
  board: { title: "Board", module: board },
  machines: { title: "Machines", module: machines },
  health: { title: "Health", module: health },
  settings: { title: "Settings", module: settings },
};
const HOME = "ask";
let current = null;

function wanted() {
  const match = location.hash.match(/^#\/([a-z]+)$/);
  const view = match && match[1];
  const button = view && $(`.rail-item[data-view="${view}"]`);
  return button && !button.hidden ? view : HOME;
}

function show(view, focus) {
  if (view !== current) {
    const leaving = current === null ? Object.keys(VIEWS).filter((name) => name !== view) : [current];
    for (const name of leaving) if (VIEWS[name].module.hidden) VIEWS[name].module.hidden();
    current = view;
  }
  for (const section of $$(".view")) section.hidden = section.dataset.view !== view;
  for (const button of $$(".rail-item")) {
    if (button.dataset.view === view) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  document.title = view === HOME ? "Ixel" : `${VIEWS[view].title} · Ixel`;
  if (VIEWS[view].module.shown) VIEWS[view].module.shown();
  if (focus) (view === HOME ? $("#question") : $(`#view-${view} .page-title`)).focus();
}

let moved = false;  // the person chose a view (rather than the page opening on one)
for (const button of $$(".rail-item")) {
  button.addEventListener("click", () => {
    moved = true;
    const hash = `#/${button.dataset.view}`;
    if (location.hash === hash) show(button.dataset.view, true);
    else location.hash = hash;
  });
}
window.addEventListener("hashchange", () => show(wanted(), moved));

health.start({ dot: $(".rail-item[data-view=health] .rail-dot") });
board.start({ dot: $(".rail-item[data-view=board] .rail-dot") });
settings.start({ changed: ask.settingsChanged });
appearance.start();
machines.start();
show(wanted(), false);
stayPresent();
