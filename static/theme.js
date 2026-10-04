// тема выставляется до отрисовки, иначе при загрузке мигает светлый фон
(function () {
  var t = null;
  try { t = localStorage.getItem("theme"); } catch (e) {}
  if (!t) t = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  document.documentElement.dataset.theme = t;
})();
