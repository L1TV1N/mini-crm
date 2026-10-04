"use strict";
const $ = (s) => document.querySelector(s);
const ref = new URLSearchParams(location.search).get("ref") || "";
$("#form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = $("#name").value.trim(), contact = $("#contact").value.trim();
  if (!name || !contact) { $("#err").textContent = "Укажите имя и как с вами связаться."; return; }
  $("#send").disabled = true; $("#err").textContent = "";
  try {
    const r = await fetch("/api/public/lead", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, contact, request: $("#request").value, website: $("#website").value, ref }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || "Не удалось отправить, попробуйте ещё раз");
    $("#form").hidden = true; $("#done").hidden = false; e.target.reset();
  } catch (x) {
    // «Failed to fetch» посетителю ничего не говорит
    $("#err").textContent = x instanceof TypeError ? "Нет связи с сервером, попробуйте ещё раз." : x.message;
  }
  $("#send").disabled = false;
});
$("#again").addEventListener("click", () => { $("#done").hidden = true; $("#form").hidden = false; $("#name").focus(); });
