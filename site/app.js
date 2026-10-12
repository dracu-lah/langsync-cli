(() => {
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Menu bar clock and the terminal's login line, in the visitor's own locale.
  const now = new Date();
  const clock = document.getElementById("clock");
  const tick = () => {
    const d = new Date();
    clock.textContent = d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" }) + "  " +
      d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  };
  tick();
  setInterval(tick, 15000);

  const login = document.getElementById("login");
  login.textContent = "Last login: " + now.toLocaleString(undefined, {
    weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).replace(/,/g, "") + " on ttys001";

  const h = now.getHours();
  const part = h < 5 ? "Up late" : h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : h < 22 ? "Good evening" : "Up late";
  document.getElementById("greeting").textContent = `${part}. Welcome to LangSync.`;

  // Type each section's command once when it scrolls into view. The text is
  // already in the HTML, so crawlers and no-JS visitors read it as is.
  const type = (el) => {
    const full = el.textContent;
    el.textContent = "";
    el.classList.add("typing");
    let i = 0;
    const step = () => {
      el.textContent = full.slice(0, ++i);
      if (i < full.length) setTimeout(step, 28 + Math.random() * 40);
      else setTimeout(() => el.classList.remove("typing"), 350);
    };
    step();
  };
  if (!reduce && "IntersectionObserver" in window) {
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (!e.isIntersecting) continue;
        io.unobserve(e.target);
        type(e.target);
      }
    }, { rootMargin: "0px 0px -15% 0px" });
    document.querySelectorAll("[data-type]").forEach((el) => io.observe(el));
  }

  // Copy buttons.
  document.querySelectorAll("[data-copy] .copy").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const text = btn.closest("[data-copy]").dataset.copy;
      try { await navigator.clipboard.writeText(text); } catch { return; }
      const label = btn.querySelector("span");
      const icon = btn.querySelector(".ph");
      btn.classList.add("done");
      icon.className = "ph ph-check";
      if (label) label.textContent = "Copied";
      setTimeout(() => {
        btn.classList.remove("done");
        icon.className = "ph ph-copy";
        if (label) label.textContent = "Copy";
      }, 1600);
    });
  });

  // Screenshot tabs, with arrow-key support.
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  const select = (tab) => {
    for (const t of tabs) {
      const on = t === tab;
      t.setAttribute("aria-selected", on);
      t.tabIndex = on ? 0 : -1;
      document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
    }
  };
  tabs.forEach((t, i) => {
    t.addEventListener("click", () => select(t));
    t.addEventListener("keydown", (e) => {
      const d = /Right|Down/.test(e.key) ? 1 : /Left|Up/.test(e.key) ? -1 : 0;
      if (!d) return;
      const next = tabs[(i + d + tabs.length) % tabs.length];
      select(next);
      next.focus();
    });
  });

  // Pause the hero recording while it is off screen, and keep it still for reduced motion.
  const video = document.querySelector(".hero-media video");
  if (video) {
    if (reduce) { video.removeAttribute("autoplay"); video.pause(); video.controls = true; }
    else if ("IntersectionObserver" in window) {
      // Open on a frame that already shows output instead of an empty prompt.
      video.addEventListener("loadedmetadata", () => { if (video.currentTime < 1) video.currentTime = 3.6; }, { once: true });
      new IntersectionObserver(([e]) => (e.isIntersecting ? video.play().catch(() => {}) : video.pause())).observe(video);
    }
  }
})();
