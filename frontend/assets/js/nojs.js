// Runs before paint: marks JS as available and applies the visitor's saved theme (no flash).
document.documentElement.classList.remove("no-js");
try {
  const t = localStorage.getItem("lp_theme");
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
} catch (e) {}
