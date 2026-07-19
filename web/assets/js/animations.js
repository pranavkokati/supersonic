/**
 * Scroll-triggered reveal for the marketing page — a single IntersectionObserver
 * fading and rising each tracked element into place, staggered by DOM order.
 * Deliberately simple: no per-word text splitting, no illustrate/3D entrance.
 */

(function () {
  const REVEAL_SELECTOR =
    "h1.hero-df-title, .hero-df-lead, .hero-df-actions, .ship-card, h2.section-title, h2.section-title-lg, " +
    ".editorial-quote, .df-feature-card, .trust-card, .agent-card, .faq-item, .cta-df h2, .cta-df p, .cta-df .btn";

  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function init() {
    const targets = Array.from(document.querySelectorAll(REVEAL_SELECTOR));
    if (!targets.length) return;

    targets.forEach((el, i) => {
      el.classList.add("reveal-up");
      el.style.setProperty("--reveal-i", String(i % 6));
    });

    if (reducedMotion || typeof IntersectionObserver !== "function") {
      targets.forEach((el) => el.classList.add("is-in"));
      return;
    }

    // Anything already inside (or just below) the first viewport reveals
    // immediately instead of waiting on an observer callback. A backgrounded
    // or throttled tab can delay — sometimes indefinitely — the callback an
    // IntersectionObserver would otherwise fire on load, which previously
    // left the entire hero section (title, lead, CTAs, the ship card) stuck
    // at opacity:0 until the tab regained focus. First paint should never
    // depend on that callback for content already on screen.
    const vh = window.innerHeight || document.documentElement.clientHeight;
    const immediate = [];
    const deferred = [];
    targets.forEach((el) => {
      const rect = el.getBoundingClientRect();
      (rect.top < vh * 1.15 ? immediate : deferred).push(el);
    });
    immediate.forEach((el) => el.classList.add("is-in"));

    if (!deferred.length) return;

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          entry.target.classList.add("is-in");
          observer.unobserve(entry.target);
        });
      },
      { threshold: 0.12, rootMargin: "0px 0px -6% 0px" }
    );
    deferred.forEach((el) => observer.observe(el));

    // Belt-and-suspenders: if the observer never fires for some element
    // (backgrounded tab, a bug, a browser quirk), nothing should stay
    // invisible forever. Force reveal after a generous timeout.
    window.setTimeout(() => {
      deferred.forEach((el) => el.classList.add("is-in"));
      observer.disconnect();
    }, 2500);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
