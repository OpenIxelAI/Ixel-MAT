// Ixel's colors, set before anything is drawn: a plain script in <head> (a module would run too late
// and the page would flash the other colors first). The server writes Settings > Appearance on
// <html data-appearance>: system, light or dark. This keeps data-theme, which style.css reads, on
// light or dark: the choice itself, or for System the computer's, switching when the computer does.
// Settings changes data-appearance and this follows at once.
{
  const root = document.documentElement;
  const light = window.matchMedia("(prefers-color-scheme: light)");
  let told = "";

  const apply = () => {
    const value = root.getAttribute("data-appearance");
    const wanted = value === "light" || value === "dark" ? value : "system";
    const theme = wanted === "system" ? (light.matches ? "light" : "dark") : wanted;
    if (root.getAttribute("data-theme") !== theme) root.setAttribute("data-theme", theme);
    // Edge and Chrome color an app window's title bar from theme-color
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", theme === "light" ? "#ffffff" : "#0e0f11");  // style.css's --bg
    // Ixel's Mac app: its title bar and the file chooser follow too (in a browser there's no such handler)
    if (wanted !== told) {
      told = wanted;
      window.webkit?.messageHandlers?.ixelAppearance?.postMessage(wanted);
    }
  };

  apply();
  light.addEventListener("change", apply);
  new MutationObserver(apply).observe(root, { attributes: true, attributeFilter: ["data-appearance"] });
}
