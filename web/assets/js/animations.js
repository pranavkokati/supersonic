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
    const targets = document.querySelectorAll(REVEAL_SELECTOR);
    targets.forEach((el, i) => {
      el.classList.add("reveal-up");
      el.style.setProperty("--reveal-i", String(i % 6));
    });

    if (reducedMotion) {
      targets.forEach((el) => el.classList.add("is-in"));
      return;
    }

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
    targets.forEach((el) => observer.observe(el));
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
