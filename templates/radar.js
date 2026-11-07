// Rain radar: RainViewer radar tiles over an OpenStreetMap base map, loaded by the
// visitor's browser when the page is opened. The page itself is only rebuilt three
// times a day, so a radar baked in at build time would be hours old.
(function () {
  var card = document.querySelector(".radar");
  if (!card) return;
  var mapEl = card.querySelector(".radar-map");
  var stage = card.querySelector(".radar-stage");
  var label = card.querySelector(".radar-time");
  var button = card.querySelector(".radar-play");
  var lat = parseFloat(card.getAttribute("data-lat"));
  var lon = parseFloat(card.getAttribute("data-lon"));
  var zoom = parseInt(card.getAttribute("data-zoom"), 10) || 7;
  var tz = card.getAttribute("data-tz") || "Australia/Melbourne";
  var W = 384, H = 256;          // the stage, in tile pixels (3:2 like the visible map box)
  var FRAMES = 6;                // newest six radar frames, about an hour
  var STEP = 700, HOLD = 1600;   // milliseconds per frame, and the pause on the latest one
  var frames = [], index = 0, timer = null, playing = false, started = false;
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function fail() {
    card.classList.add("is-off");
    label.textContent = "";
  }

  // Position of a latitude and longitude on the whole-world map at this zoom, in pixels.
  function world(la, lo, z) {
    var size = 256 * Math.pow(2, z);
    var s = Math.sin(la * Math.PI / 180);
    return { x: (lo + 180) / 360 * size, y: (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * size };
  }

  function clock(unix) {
    try {
      return new Intl.DateTimeFormat("en-AU", { hour: "numeric", minute: "2-digit", hour12: true, timeZone: tz })
        .format(new Date(unix * 1000)).replace(/\s/g, "").toLowerCase();
    } catch (e) { return ""; }
  }

  function build(host, past) {
    var c = world(lat, lon, zoom);
    var left = c.x - W / 2, top = c.y - H / 2, n = Math.pow(2, zoom);
    var tiles = [];
    for (var ty = Math.floor(top / 256); ty <= Math.floor((top + H - 1) / 256); ty++) {
      for (var tx = Math.floor(left / 256); tx <= Math.floor((left + W - 1) / 256); tx++) {
        if (ty < 0 || ty >= n) continue;
        tiles.push({ x: ((tx % n) + n) % n, y: ty, left: tx * 256 - left, top: ty * 256 - top });
      }
    }
    function layer(cls, urlFor) {
      var d = document.createElement("div");
      d.className = cls;
      tiles.forEach(function (t) {
        var img = new Image();
        img.alt = "";
        img.draggable = false;
        img.style.left = t.left + "px";
        img.style.top = t.top + "px";
        img.src = urlFor(t);
        d.appendChild(img);
      });
      stage.appendChild(d);
      return d;
    }
    layer("radar-base", function (t) { return "https://tile.openstreetmap.org/" + zoom + "/" + t.x + "/" + t.y + ".png"; });
    past.slice(-FRAMES).forEach(function (f) {
      frames.push({
        time: f.time,
        el: layer("radar-frame", function (t) { return host + f.path + "/256/" + zoom + "/" + t.x + "/" + t.y + "/2/1_1.png"; })
      });
    });
  }

  function fit() { stage.style.transform = "scale(" + (mapEl.clientWidth / W) + ")"; }

  function show(i) {
    index = i;
    frames.forEach(function (f, k) { f.el.classList.toggle("on", k === i); });
    var text = clock(frames[i].time);
    if (i === frames.length - 1) {
      var age = Math.round((Date.now() / 1000 - frames[i].time) / 60);
      text += age > 45 ? " · " + age + " min old" : " · latest";
    }
    label.textContent = text;
  }

  function tick() {
    show((index + 1) % frames.length);
    timer = setTimeout(tick, index === frames.length - 1 ? HOLD : STEP);
  }
  function play() {
    playing = true;
    button.textContent = "Pause";
    clearTimeout(timer);
    timer = setTimeout(tick, STEP);
  }
  function pause() {
    playing = false;
    button.textContent = "Play";
    clearTimeout(timer);
  }

  function start() {
    if (started) return;
    started = true;
    fetch("https://api.rainviewer.com/public/weather-maps.json", { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (d) {
        var past = d && d.radar && d.radar.past;
        if (!past || !past.length) throw new Error("no radar frames");
        build(d.host || "https://tilecache.rainviewer.com", past);
        card.classList.add("is-live");
        fit();
        show(frames.length - 1);
        if (reduce || frames.length < 2) { button.hidden = true; } else { play(); }
      })
      .catch(fail);
  }

  button.addEventListener("click", function () { if (playing) { pause(); } else { play(); } });
  document.addEventListener("visibilitychange", function () {
    if (!frames.length) return;
    if (document.hidden) { clearTimeout(timer); } else if (playing) { play(); }
  });
  if (window.ResizeObserver) { new ResizeObserver(fit).observe(mapEl); } else { window.addEventListener("resize", fit); }

  if ("IntersectionObserver" in window) {
    new IntersectionObserver(function (entries, observer) {
      if (entries[0].isIntersecting) { observer.disconnect(); start(); }
    }, { rootMargin: "300px" }).observe(card);
  } else {
    start();
  }
})();
