// Fills a saved login into the current page without returning any
// value. Called as (secret, expectedOrigin) through CDP Runtime.evaluate; the
// result names only which fields were filled.
(function (secret, expectedOrigin) {
  if (location.origin !== expectedOrigin) {
    return { ok: false, filled: [], error: "the tab is no longer on " + expectedOrigin };
  }

  function usable(el) {
    if (el.disabled || el.readOnly) return false;
    const style = getComputedStyle(el);
    const box = el.getBoundingClientRect();
    return box.width > 0 && box.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  }

  function describe(el) {
    return [el.autocomplete, el.name, el.id, el.placeholder, el.getAttribute("aria-label")]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
  }

  // React and friends track the native value setter, so assigning `.value`
  // alone leaves their state empty.
  function set(el, value) {
    el.focus();
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.blur();
  }

  const fields = Array.from(document.querySelectorAll("input")).filter(usable);
  const textLike = (el) => el instanceof HTMLInputElement && ["text", "email", "tel", ""].includes(el.type);
  const filled = [];

  if (secret.kind === "login") {
    const password =
      fields.find((el) => el.type === "password" && el.autocomplete === "current-password") ||
      fields.find((el) => el.type === "password");
    let user = fields.find((el) => textLike(el) && /\b(username|email)\b/.test(el.autocomplete || ""));
    if (!user && password) {
      const before = fields.slice(0, fields.indexOf(password)).filter(textLike);
      user = before[before.length - 1];
    }
    if (!user && !password) {
      user = fields.find((el) => textLike(el) && /user|email|login|phone|mobile|account/.test(describe(el)));
    }
    if (user && secret.username) {
      set(user, secret.username);
      filled.push("username");
    }
    if (password && secret.password) {
      set(password, secret.password);
      filled.push("password");
    }
  }

  if (filled.length === 0) {
    return { ok: false, filled, error: "no sign-in fields on the page" };
  }
  return { ok: true, filled };
})
