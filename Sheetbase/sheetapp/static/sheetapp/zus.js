// The ZUS look (a joke copy of the ZUS portal), loaded only in that look: the page's name in the title bar,
// the breadcrumb tab and the status bar, and the session ticking away (only for show: nothing logs you out).
(() => {
  const pageName = document.title.split(" · ")[0];
  document.querySelectorAll(".zus-page-name").forEach(el => { el.textContent = pageName; });
  let left = 15;
  const counter = document.querySelector(".zus-session");
  setInterval(() => { left = left > 1 ? left - 1 : 15; if (counter) counter.textContent = left; }, 60000);
})();
