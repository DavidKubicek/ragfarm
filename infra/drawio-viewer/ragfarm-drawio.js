/* ragfarm-drawio.js — everything needed to turn model-authored draw.io XML into
 * a rendered diagram, in ONE file the model only has to reference by URL.
 *
 * WHY THIS FILE EXISTS
 * The vision preset's RULE 5 gives the model an HTML wrapper to copy verbatim.
 * Every line of that wrapper is a line the model can get wrong, and on
 * 2026-08-09 it did: on a 31-node diagram it reproduced the whole page
 * correctly but silently DROPPED the inline bootstrap that sets the
 * data-mxgraph attribute, while faithfully emitting the one-line
 * <script src=".../viewer-static.min.js"> right after it. Result: a complete,
 * valid <mxfile> that pastes into draw.io online perfectly, and a blank pane in
 * chat, because nothing ever handed the XML to the viewer.
 *
 * The lesson is not "instruct harder". It is that boilerplate the model must
 * retype is a liability proportional to its length, so this file absorbs it:
 * the path overrides, the XML hand-off, the viewer load, and the error
 * reporting. What is left in the prompt is a wrapper short enough to be copied
 * reliably, and the model's real job — the XML — is the only part that varies.
 *
 * It also means a blank pane is no longer possible without an explanation:
 * every failure below paints a readable message where the diagram would be.
 *
 * Served by the drawio-viewer nginx from infra/drawio-viewer/, alongside the
 * webapp mirror. Tracked in git (see .gitignore) because it is ours, not
 * upstream's — scripts/fetch-drawio-viewer.sh must not clobber it.
 */
(function () {
  "use strict";

  // Same origin this script was loaded from, so the URLs follow the <script src>
  // the model wrote and there is no second place to keep the host in sync.
  var base = (function () {
    var s = document.currentScript;
    if (s && s.src) return s.src.replace(/\/[^\/]*$/, "");
    return window.location.origin;
  })();

  function ready(fn) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", fn);
    } else {
      fn();
    }
  }

  ready(function () {
    var box = document.getElementById("ragfarm-graph") ||
              document.querySelector(".mxgraph");
    if (!box) return;  // nothing to render into; not our page

    function fail(msg) {
      box.removeAttribute("data-mxgraph");
      box.style.font = "13px sans-serif";
      box.style.color = "#b85450";
      box.style.padding = "8px";
      box.textContent = msg;
    }

    var src = document.getElementById("ragfarm-xml");
    var xml = src ? (src.textContent || "").trim() : "";
    if (!xml) {
      return fail("No diagram XML found — the answer is missing its " +
                  "<script type=\"application/xml\" id=\"ragfarm-xml\"> block.");
    }
    if (xml.indexOf("</mxfile>") < 0) {
      return fail("Diagram XML was cut off before </mxfile> — the answer did not " +
                  "finish. Ask again, or ask for a simpler diagram.");
    }

    // The viewer reads its diagram from the data-mxgraph JSON. It does NOT read a
    // child <xml> element; that form throws 'can't access property "length", a is
    // undefined' and draws nothing.
    box.setAttribute("data-mxgraph", JSON.stringify({
      highlight: "#0000ff", nav: true, resize: true,
      toolbar: "zoom layers lightbox", xml: xml
    }));

    // Resource paths for shapes/stencils/styles. Set unconditionally: the model
    // may or may not have copied the window.*_PATH block, and ours is right.
    window.STYLE_PATH = base + "/styles";
    window.SHAPES_PATH = base + "/shapes";
    window.STENCIL_PATH = base + "/stencils";
    window.DRAW_MATH_URL = base + "/math4/es5";
    window.GRAPH_IMAGE_PATH = base + "/img";

    var s = document.createElement("script");
    s.src = base + "/js/viewer-static.min.js";
    s.onerror = function () {
      fail("draw.io viewer failed to load from " + base + "/js/ — the local " +
           "mirror is missing (scripts/fetch-drawio-viewer.sh) or this browser " +
           "cannot reach that host.");
    };
    document.body.appendChild(s);
  });
})();
