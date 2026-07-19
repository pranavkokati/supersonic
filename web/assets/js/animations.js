/**
 * Linear landing-page motion (ported from blur-pop-up-by-words + illustrate)
 * https://github.com/anoopraju31/linear-landing-page
 *
 * Headlines: blur + rise per word
 * Body: fade-rise stagger (no blur)
 * Hero mock: 3D illustrate entrance
 */

(function () {
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const HEADLINE_SELECTOR =
    "h1.hero-df-title, h2.section-title, h2.section-title-lg, h2.editorial-quote, .cta-df h2, .df-feature-copy h3, .trust-card h3";

  const BODY_SELECTOR =
    ".hero-df-lead, .hero-df-actions, .pipeline-flow, .section-desc, .quickstart-block, .sponsor-row, .cta-df p, .cta-df .btn, .df-feature-copy p";

  const STAGGER_PARENTS =
    ".trust-grid, .agent-grid, .df-features-inner, .faq-list, #features";

  function splitHeadline(el) {
    if (el.dataset.animSplit) return;
    const text = el.textContent.replace(/\s+/g, " ").trim();
    if (!text) return;
    el.dataset.animSplit = "1";
    el.setAttribute("aria-label", text);
    el.classList.add("anim-headline");
    el.textContent = "";

    const inner = document.createElement("span");
    inner.className = "anim-headline-inner";
    const words = text.split(" ");

    words.forEach((word, i) => {
      const span = document.createElement("span");
      span.className = "anim-word";
      span.style.setProperty("--word-i", String(i));
      span.textContent = word;
      inner.appendChild(span);
      if (i < words.length - 1) inner.appendChild(document.createTextNode(" "));
    });
    el.appendChild(inner);
  }

  function revealWords(headline) {
    headline.querySelectorAll(".anim-word").forEach((w) => w.classList.add("is-in"));
  }

  function initHeadlines() {
    document.querySelectorAll(HEADLINE_SELECTOR).forEach(splitHeadline);

    if (reducedMotion) {
      document.querySelectorAll(".anim-headline").forEach(revealWords);
      return;
    }

    const hero = document.querySelector("h1.hero-df-title.anim-headline");
    if (hero) {
      requestAnimationFrame(() => {
        hero.querySelectorAll(".anim-word").forEach((w, i) => {
          setTimeout(() => w.classList.add("is-in"), 80 + i * 100);
        });
      });
    }

    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((e) => {
          if (!e.isIntersecting) return;
          revealWords(e.target);
          io.unobserve(e.target);
        });
      },
      { threshold: 0.15, rootMargin: "0px 0px -5% 0px" }
    );

    document.querySelectorAll(".anim-headline").forEach((h) => {
      if (h === hero) return;
      io.observe(h);
    });
  }

  function initBody() {
    const heroLead = document.querySelector(".hero-df-lead");
    const heroActions = document.querySelector(".hero-df-actions");
    const heroPipe = document.querySelector(".pipeline-flow");

    if (reducedMotion) {
      document.querySelectorAll(BODY_SELECTOR).forEach((el) => el.classList.add("is-in"));
      return;
    }

    [heroLead, heroActions, heroPipe].forEach((el, idx) => {
      if (!el) return;
      el.classList.add("anim-body");
      const delay = 1 + idx * 0.15;
      setTimeout(() => el.classList.add("is-in"), delay * 1000);
    });

    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((e) => {
          if (!e.isIntersecting) return;
          e.target.classList.add("is-in");
          io.unobserve(e.target);
        });
      },
      { threshold: 0.12, rootMargin: "0px 0px -8% 0px" }
    );

    document.querySelectorAll(BODY_SELECTOR).forEach((el) => {
      if (el === heroLead || el === heroActions || el === heroPipe) return;
      el.classList.add("anim-body");
      io.observe(el);
    });
  }

  function initStaggerGrids() {
    document.querySelectorAll(STAGGER_PARENTS).forEach((parent) => {
      parent.classList.add("anim-stagger");
      [...parent.children].forEach((child, i) => {
        child.style.setProperty("--stagger-i", String(i));
      });

      if (reducedMotion) {
        parent.classList.add("is-in");
        return;
      }

      const io = new IntersectionObserver(
        (entries) => {
          entries.forEach((e) => {
            if (!e.isIntersecting) return;
            e.target.classList.add("is-in");
            io.unobserve(e.target);
          });
        },
        { threshold: 0.1 }
      );
      io.observe(parent);
    });
  }

  function initIllustrate() {
    const mock = document.getElementById("product-mock");
    if (!mock) return;
    mock.classList.add("anim-illustrate");
    if (reducedMotion) {
      mock.classList.add("is-in");
      return;
    }
    setTimeout(() => mock.classList.add("is-in"), 2000);
  }

  function init() {
    initHeadlines();
    initBody();
    initStaggerGrids();
    initIllustrate();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
