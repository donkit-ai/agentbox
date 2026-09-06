if (document.body.dataset.view === "opening") {
  // Fragments never reach HTTP logs or Referer headers. Remove it before any request.
  const token = location.hash.slice(1);
  history.replaceState(null, "", location.pathname);
  const status = document.getElementById("status");
  if (!token) {
    status.textContent = "Откройте полную ссылку, которую передал агент.";
  } else {
    fetch(location.pathname + "/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    })
      .then((response) => {
        if (response.ok) location.reload();
        else
          status.textContent =
            "Ссылка недействительна или уже использована. Попросите агента создать новую.";
      })
      .catch(() => {
        status.textContent =
          "Не удалось связаться с Agentbox. Проверьте соединение и откройте исходную ссылку снова.";
      });
  }
}
document.querySelectorAll("form").forEach((form) => {
  form.addEventListener("submit", () => {
    const button = form.querySelector('button[type="submit"]');
    if (!button) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.dataset.original = button.textContent;
    button.textContent = button.dataset.busy || "Подключаем…";
  });
});
// Restore buttons when Back returns from the provider via the browser cache.
window.addEventListener("pageshow", () => {
  document.querySelectorAll('button[aria-busy="true"]').forEach((button) => {
    button.disabled = false;
    button.removeAttribute("aria-busy");
    button.textContent = button.dataset.original;
  });
});
