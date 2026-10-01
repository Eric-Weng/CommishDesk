"""Story 6.0c Part A — the built-in :class:`~commishdesk.render.enhancer.WebEnhancer`.

The weekly page's Story 5B interaction script and its motion / script-gated
CSS, moved out of :mod:`commishdesk.render.weekly_web` and
:mod:`commishdesk.render.style` verbatim (plus the two audit fixes: the luck
rows get ``role="button"`` / ``tabindex="0"`` from the script, not the markup,
and the bump chart's team buttons and detail panel are revealed only here).
The CLI passes :data:`BUILTIN_ENHANCER` so self-hosted output stays animated at
tag N.

:data:`BUILTIN_CSS` and :data:`BUILTIN_JS` are importable as-is so Story 6.10
can copy them into the hosted app's ``.css`` / ``.js`` files; the app's
enhanced page must be byte-identical to this one's output (AD-40).

**Removed whole in Part B** (tag N+1). Nothing else in ``src`` may define an
enhancer.

**Pipeline fence (AD-1).** Standard library only.
"""

from __future__ import annotations

__all__ = ["BUILTIN_CSS", "BUILTIN_ENHANCER", "BUILTIN_JS"]


#: Motion and script-gated rules, appended after the weekly page's base CSS.
#: Every rule here is either a ``@keyframes``, scoped under the script-added
#: ``html.js-reveal`` class, styles a control the script itself creates (the
#: luck row's button role), or is the reduced-motion block — so the static
#: page without it is complete and has no dead controls.
BUILTIN_CSS = """
/* Story 5B.2 — reveal/draw-in motion. Hiding values only exist inside
   @keyframes and are gated by the script-added html.js-reveal ancestor. */
@keyframes reveal-in {
  from { opacity: 0; transform: translateY(12px); }
  to { opacity: 1; transform: translateY(0); }
}
@keyframes draw-in {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}
html.js-reveal [data-reveal].is-revealed {
  animation: reveal-in 500ms cubic-bezier(.16, 1, .3, 1) both;
}
html.js-reveal [data-reveal].is-revealed [data-draw] > *:not(title) {
  animation: draw-in 400ms cubic-bezier(.16, 1, .3, 1) both;
  animation-delay: calc(var(--draw-i, 0) * 25ms);
}

/* Story 5B.4 -- luck row preview (the script gives each row its button role)
   + grow-from-zero bars */
.luck-row { cursor: pointer; outline: none; }
.luck-row:focus-visible { outline: 3px solid var(--emph); outline-offset: 3px; }
.luck-row.is-active rect.luck-hit { stroke: var(--emph); stroke-width: 2; }
html.js-reveal [data-reveal].is-revealed .luck-bar {
  animation: luck-grow 500ms cubic-bezier(.16, 1, .3, 1) both;
  animation-delay: calc(var(--draw-i, 0) * 25ms);
}
@keyframes luck-grow {
  from { transform: scaleX(0); }
  to { transform: scaleX(var(--luck-len, 1)); }
}

/* Story 5B.5 -- expandable matchup and next-week cards (script-gated) */
html.js-reveal .card-expand { display: inline-flex; }
html.js-reveal .card:hover .game-detail,
html.js-reveal .card:hover .nw-detail,
html.js-reveal .card.is-pinned .game-detail,
html.js-reveal .card.is-pinned .nw-detail {
  display: block;
}
html.js-reveal .card.is-pinned {
  border-color: var(--emph);
  box-shadow: 0 0 0 2px var(--emph-wash), var(--shadow);
}

/* Story 5B.4 -- the all-play toggle shows only when the script can drive it */
html.js-reveal .standings-toggle { display: flex; }

/* Story 5B.3 -- bump chart: the team buttons and detail panel exist only with
   the script; the chart sits beside them in two columns. */
html.js-reveal .bump-layout { grid-template-columns: minmax(0, 1fr) 320px; }
html.js-reveal .bump-detail { display: block; }
html.js-reveal .pw-hits { display: flex; }
html.js-reveal [data-reveal].is-revealed .bump-team .bump-model {
  animation: bump-draw-line 400ms cubic-bezier(.16, 1, .3, 1) both;
  animation-delay: calc(var(--draw-i, 0) * 25ms);
}
html.js-reveal [data-reveal].is-revealed .bump-team .bump-point,
html.js-reveal [data-reveal].is-revealed .bump-team .bump-pub-line {
  animation: bump-draw-point 400ms cubic-bezier(.16, 1, .3, 1) both;
  animation-delay: calc(var(--draw-i, 0) * 25ms);
}
@keyframes bump-draw-line {
  from { stroke-dashoffset: var(--len, 0); }
  to { stroke-dashoffset: 0; }
}
@keyframes bump-draw-point {
  from { opacity: 0; }
  to { opacity: 1; }
}
@media (max-width: 900px) {
  html.js-reveal .bump-layout { grid-template-columns: minmax(0, 1fr); }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0s !important;
    animation-delay: 0s !important;
    transition-duration: 0s !important;
  }
}
""".strip()


# The single inline interaction layer for Story 5B.2 / 5B.3 / 5B.4 / 5B.5. The
# page is already finished without script; this only adds the enabling class,
# observes [data-reveal] elements, drives the power-rank bump chart's hover /
# pin detail, makes each luck row a focusable button and previews it on focus /
# tap, flips the standings all-play view, and pins an expandable matchup /
# next-week card. It writes markup only through createElement/textContent (no
# innerHTML/eval), and sets only class/style/ARIA state.
BUILTIN_JS = """(function () {
  "use strict";
  var root = document.documentElement;
  root.classList.add("js-reveal");

  var charts = document.querySelectorAll("[data-draw]");
  Array.prototype.forEach.call(charts, function (chart) {
    var drawIndex = 0;
    Array.prototype.forEach.call(chart.children, function (child) {
      if (child.tagName.toLowerCase() !== "title") {
        child.style.setProperty("--draw-i", String(drawIndex));
        drawIndex += 1;
      }
    });
  });

  Array.prototype.forEach.call(document.querySelectorAll("[data-luck-section]"), function (section) {
    var preview = section.querySelector(".luck-preview");
    Array.prototype.forEach.call(section.querySelectorAll(".luck-row[data-luck-preview]"), function (row) {
      row.setAttribute("role", "button");
      row.setAttribute("tabindex", "0");
      function selectRow() {
        if (!preview) { return; }
        preview.hidden = false;
        preview.textContent = row.getAttribute("data-luck-preview") || "";
      }
      row.addEventListener("click", selectRow);
      row.addEventListener("focus", function () {
        row.classList.add("is-active");
        selectRow();
      });
      row.addEventListener("blur", function () {
        row.classList.remove("is-active");
      });
    });
  });

  Array.prototype.forEach.call(document.querySelectorAll("[data-standings]"), function (section) {
    var toggle = section.querySelector(".standings-toggle");
    if (!toggle) { return; }
    Array.prototype.forEach.call(toggle.querySelectorAll("button[data-view]"), function (button) {
      button.addEventListener("click", function () {
        var allplay = button.getAttribute("data-view") === "allplay";
        section.classList.toggle("is-allplay", allplay);
        Array.prototype.forEach.call(toggle.querySelectorAll("button[data-view]"), function (node) {
          node.classList.toggle("is-active", node === button);
          node.setAttribute("aria-pressed", node === button ? "true" : "false");
        });
        Array.prototype.forEach.call(section.querySelectorAll(".rec-act, .legend-act"), function (node) {
          node.setAttribute("aria-hidden", allplay ? "true" : "false");
        });
        Array.prototype.forEach.call(section.querySelectorAll(".rec-ap, .legend-ap"), function (node) {
          node.setAttribute("aria-hidden", allplay ? "false" : "true");
        });
      });
    });
  });

  Array.prototype.forEach.call(document.querySelectorAll(".card-expand"), function (button) {
    var card = button.closest(".card");
    if (!card) { return; }
    function renderPinned() {
      var pinned = card.classList.contains("is-pinned");
      button.setAttribute("aria-expanded", pinned ? "true" : "false");
      var state = button.querySelector(".card-expand-state");
      if (state) {
        state.textContent = pinned ? "Pinned open" : "";
      }
    }
    button.addEventListener("click", function () {
      card.classList.toggle("is-pinned");
      renderPinned();
    });
    renderPinned();
  });

  Array.prototype.forEach.call(document.querySelectorAll("[data-bump-chart]"), function (bumpRoot) {
    var detail = bumpRoot.querySelector("[data-bump-detail]");
    var buttons = bumpRoot.querySelectorAll(".pw-hit[data-team]");
    var lines = bumpRoot.querySelectorAll(".bump-team[data-team]");
    var hoverTeam = null;
    var pinnedTeam = null;

    function clearDetail() {
      while (detail.firstChild) {
        detail.removeChild(detail.firstChild);
      }
    }

    function showDefault() {
      clearDetail();
      var p = document.createElement("p");
      p.className = "bd-default";
      p.textContent = "Hover or select a team to inspect its ranks.";
      detail.appendChild(p);
    }

    function showTeam(team, pinned) {
      clearDetail();
      var button = bumpRoot.querySelector('.pw-hit[data-team="' + CSS.escape(team) + '"]');
      var name = button ? button.getAttribute("data-name") : team;
      var heading = document.createElement("p");
      heading.className = "bd-team";
      heading.textContent = name + (pinned ? " \u00b7 Pinned open" : "");
      detail.appendChild(heading);

      var list = document.createElement("ul");
      list.className = "bd-list";
      Array.prototype.forEach.call(
        bumpRoot.querySelectorAll('.bump-point.bump-model-dot[data-team="' + CSS.escape(team) + '"]'),
        function (dot) {
          var week = dot.getAttribute("data-week");
          var model = dot.getAttribute("data-rank");
          var published = dot.getAttribute("data-pub");
          var line = document.createElement("li");
          line.textContent = "Week " + week + ": model " + model + (published ? ", published " + published : "");
          list.appendChild(line);
        }
      );
      detail.appendChild(list);
    }

    function setActive() {
      var active = pinnedTeam || hoverTeam;
      var isPinned = pinnedTeam !== null;
      Array.prototype.forEach.call(lines, function (line) {
        var team = line.getAttribute("data-team");
        line.classList.toggle("is-active", active === team);
        line.classList.toggle("is-dim", active !== null && active !== team);
      });
      Array.prototype.forEach.call(buttons, function (button) {
        var team = button.getAttribute("data-team");
        button.classList.toggle("is-active", active === team);
        button.classList.toggle("is-dim", active !== null && active !== team);
        button.classList.toggle("is-pinned", isPinned && pinnedTeam === team);
        button.setAttribute("aria-pressed", (isPinned && pinnedTeam === team) ? "true" : "false");
        var state = button.querySelector(".hit-state");
        if (state) {
          state.textContent = (isPinned && pinnedTeam === team) ? "Pinned open" : "";
        }
      });
      if (active !== null) {
        showTeam(active, isPinned);
      } else {
        showDefault();
      }
    }

    function bindTeamHover(element, team) {
      element.addEventListener("mouseenter", function () {
        if (pinnedTeam === null || pinnedTeam === team) {
          hoverTeam = team;
          setActive();
        }
      });
      element.addEventListener("mouseleave", function () {
        if (hoverTeam === team) {
          hoverTeam = null;
          setActive();
        }
      });
    }

    Array.prototype.forEach.call(buttons, function (button) {
      var team = button.getAttribute("data-team");
      if (!team) {
        return;
      }
      bindTeamHover(button, team);
      button.addEventListener("click", function () {
        if (pinnedTeam === team) {
          pinnedTeam = null;
        } else {
          pinnedTeam = team;
        }
        hoverTeam = null;
        setActive();
      });
    });

    Array.prototype.forEach.call(lines, function (line) {
      var team = line.getAttribute("data-team");
      if (team) {
        bindTeamHover(line, team);
      }
    });

    setActive();
  });

  var targets = document.querySelectorAll("[data-reveal]");
  function show(target) {
    target.classList.add("is-revealed");
  }

  if (!("IntersectionObserver" in window)) {
    Array.prototype.forEach.call(targets, show);
    return;
  }

  var observer = new IntersectionObserver(function (entries, obs) {
    entries.forEach(function (entry) {
      if (entry.isIntersecting) {
        show(entry.target);
        obs.unobserve(entry.target);
      }
    });
  }, { rootMargin: "0px 0px -8% 0px", threshold: 0.1 });

  Array.prototype.forEach.call(targets, function (target) {
    observer.observe(target);
  });
})();"""


class _BuiltinEnhancer:
    """The Story 5B interaction layer as a :class:`~commishdesk.render.enhancer.WebEnhancer`."""

    def css(self) -> str:
        return BUILTIN_CSS

    def js(self) -> str:
        return BUILTIN_JS


BUILTIN_ENHANCER = _BuiltinEnhancer()
