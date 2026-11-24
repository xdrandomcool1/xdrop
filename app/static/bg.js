// Deep blue gradient backdrop with a stippled dot field: dots cluster into soft
// "continent" blobs toward the left and right edges and thin out in the centre,
// plus a few dotted vertical streaks. Drawn once per resize on a DPR-aware canvas.
(function () {
  function hash(i, j) { var h = Math.sin(i * 127.1 + j * 311.7) * 43758.5453; return h - Math.floor(h); }
  function sm(a, b, v) { var t = Math.max(0, Math.min(1, (v - a) / (b - a))); return t * t * (3 - 2 * t); }
  function noise(x, y) {
    var i = Math.floor(x), j = Math.floor(y), fx = x - i, fy = y - j;
    var ux = fx * fx * (3 - 2 * fx), uy = fy * fy * (3 - 2 * fy);
    var a = hash(i, j), b = hash(i + 1, j), c = hash(i, j + 1), d = hash(i + 1, j + 1);
    return a + (b - a) * ux + (c - a) * uy + (a - b - c + d) * ux * uy;
  }
  function fbm(x, y) { return noise(x, y) * 0.55 + noise(x * 2.1, y * 2.1) * 0.3 + noise(x * 4.3, y * 4.3) * 0.15; }

  function drawDots(c) {
    var dpr = Math.min(2, window.devicePixelRatio || 1);
    var W = window.innerWidth, H = window.innerHeight, s = W < 720 ? 6 : 7;
    c.width = Math.round(W * dpr); c.height = Math.round(H * dpr);
    var g = c.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, H);
    for (var x = 0, col = 0; x < W; x += s, col++) {
      var u = x / W, side = sm(0.1, 0.36, Math.abs(u - 0.5));
      var streak = hash(col, 7) > 0.955 ? 0.55 * hash(col, 9) : 0;
      for (var y = 0, row = 0; y < H; y += s, row++) {
        var v = y / H, r = hash(col, row), r2 = hash(row + 31, col + 17);
        var land = sm(0.44, 0.6, fbm(x / 170 + 3, y / 170 + 1));
        var p = land * side * (0.45 + 0.55 * sm(0.05, 0.6, v));
        p = Math.max(p, streak * side * sm(0.1, 0.5, v) * (r > 0.35 ? 1 : 0));
        if (r2 < 0.006) p = Math.max(p, 0.6);
        if (r < p) {
          g.fillStyle = "rgba(205,222,255," + (0.32 + 0.6 * p * hash(row, col + 3)).toFixed(3) + ")";
          g.fillRect(x, y, 1.6, 1.6);
        }
      }
    }
  }

  var w = document.createElement("div");
  w.className = "bg-wrap"; w.setAttribute("aria-hidden", "true");
  w.innerHTML = '<div class="bg-grad"></div><canvas class="dots"></canvas><div class="bg-glow"></div>';
  document.body.prepend(w);
  var canvas = w.querySelector(".dots");
  drawDots(canvas);
  var timer, lastW = window.innerWidth;
  window.addEventListener("resize", function () {
    clearTimeout(timer);
    timer = setTimeout(function () {
      // mobile URL-bar show/hide changes only the height; skip those redraws
      if (window.innerWidth === lastW && Math.abs(canvas.height / (window.devicePixelRatio || 1) - window.innerHeight) < 120) return;
      lastW = window.innerWidth; drawDots(canvas);
    }, 150);
  });
})();
