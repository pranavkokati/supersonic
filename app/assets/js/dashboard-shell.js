/** Chrome: sidebar + view switching. No OAuth popups needed anymore —
 * settings are plain key/value fields (see dashboard.js). */

(() => {
  const sidebar = document.querySelector("#project-sidebar");
  const toggle = document.querySelector("#sidebar-toggle");
  const close = document.querySelector("#sidebar-close");

  function setSidebar(open) {
    if (!sidebar || !toggle) return;
    sidebar.classList.toggle("open", open);
    sidebar.setAttribute("aria-hidden", String(!open));
    toggle.setAttribute("aria-expanded", String(open));
  }

  toggle?.addEventListener("click", () => setSidebar(!sidebar?.classList.contains("open")));
  close?.addEventListener("click", () => setSidebar(false));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") setSidebar(false); });

  function setView(name) {
    document.querySelectorAll(".sn-view").forEach((v) => v.classList.toggle("hidden", v.id !== `view-${name}`));
    document.querySelectorAll(".sn-nav-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  }

  document.addEventListener("click", (event) => {
    const navBtn = event.target.closest(".sn-nav-btn[data-view]");
    if (navBtn) {
      setView(navBtn.dataset.view);
      setSidebar(false);
    }
    const projectItem = event.target.closest("#project-list li[data-id]");
    if (projectItem) {
      window.__sonicSelectProject?.(projectItem.dataset.id);
      setSidebar(false);
    }
  });

  window.__sonicSetView = setView;
})();
