/** Landing — mobile nav (scroll motion in animations.js, which also handles
 * the hero ship-card's entrance via the shared IntersectionObserver reveal). */

document.querySelectorAll(".api-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    const parent = tab.closest(".mock-code") || tab.closest(".api-panel");
    parent?.querySelectorAll(".api-tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    const req = document.getElementById("api-code-request");
    const res = document.getElementById("api-code-response");
    const showRes = tab.dataset.tab === "response";
    if (showRes) {
      req?.classList.add("hidden");
      res?.classList.remove("hidden");
    } else {
      res?.classList.add("hidden");
      req?.classList.remove("hidden");
    }
  });
});

function initMobileNav() {
  const btn = document.getElementById("df-menu-btn");
  const menu = document.getElementById("df-mobile-nav");
  if (!btn || !menu) return;
  btn.addEventListener("click", () => {
    const open = menu.classList.toggle("hidden");
    btn.setAttribute("aria-expanded", open ? "false" : "true");
  });
}

initMobileNav();
