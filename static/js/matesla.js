(function () {
    "use strict";

    var header = document.querySelector("[data-app-header]");
    function syncHeader() {
        if (header) {
            header.classList.toggle("is-scrolled", window.scrollY > 12);
        }
    }

    syncHeader();
    window.addEventListener("scroll", syncHeader, { passive: true });

    document.addEventListener("click", function (event) {
        var trigger = event.target.closest("[data-confirm]");
        if (!trigger) return;
        if (!window.confirm(trigger.getAttribute("data-confirm"))) {
            event.preventDefault();
        }
    });

    document.querySelectorAll(".mobile-menu a").forEach(function (link) {
        link.addEventListener("click", function () {
            var menu = link.closest("details");
            if (menu) menu.removeAttribute("open");
        });
    });

    bindVehicleSwitcher(document.querySelector("[data-vehicle-switcher]"));

    function bindVehicleSwitcher(form) {
        if (!form || form.getAttribute("data-vehicle-switcher-bound")) return;
        form.setAttribute("data-vehicle-switcher-bound", "1");

        var select = form.querySelector("#vehicle_api_id");
        var status = form.querySelector("[data-vehicle-switch-status]");
        var lastValue = select ? select.value : "";
        var timeoutId = 0;
        var allowNativeSubmit = false;
        var PENDING_MS = 90000;

        function statusText(kind, name) {
            var key = kind === "timeout" ? "data-i18n-timeout" : "data-i18n-switching";
            var template = form.getAttribute(key) || "";
            if (kind !== "timeout" && name) {
                return template.replace("{name}", name);
            }
            if (kind !== "timeout") {
                return form.getAttribute("data-i18n-switching-fallback") || template;
            }
            return template;
        }

        function selectedName() {
            if (!select || select.selectedIndex < 0) return "";
            var option = select.options[select.selectedIndex];
            var label = (option.getAttribute("data-label") || option.textContent || "").trim();
            return label.split(" · ")[0].trim();
        }

        function setStatus(text) {
            if (!status) return;
            if (!text) {
                status.hidden = true;
                status.textContent = "";
                return;
            }
            status.hidden = false;
            status.textContent = text;
        }

        function clearPending() {
            if (timeoutId) {
                window.clearTimeout(timeoutId);
                timeoutId = 0;
            }
            allowNativeSubmit = false;
            form.dataset.pending = "";
            form.classList.remove("is-pending");
            form.removeAttribute("aria-busy");
            if (select) select.removeAttribute("aria-disabled");
            document.body.classList.remove("vehicle-switch-pending");
            setStatus("");
        }

        function beginPending() {
            if (form.dataset.pending === "1") return false;
            form.dataset.pending = "1";
            form.classList.add("is-pending");
            form.setAttribute("aria-busy", "true");
            if (select) select.setAttribute("aria-disabled", "true");
            document.body.classList.add("vehicle-switch-pending");
            setStatus(statusText("switching", selectedName()));
            timeoutId = window.setTimeout(function () {
                timeoutId = 0;
                form.dataset.pending = "";
                form.classList.remove("is-pending");
                form.removeAttribute("aria-busy");
                if (select) select.removeAttribute("aria-disabled");
                document.body.classList.remove("vehicle-switch-pending");
                setStatus(statusText("timeout"));
            }, PENDING_MS);
            return true;
        }

        form.addEventListener("submit", function (event) {
            if (form.dataset.pending === "1" && !allowNativeSubmit) {
                event.preventDefault();
                return;
            }
            allowNativeSubmit = false;
            beginPending();
        });

        if (select) {
            select.addEventListener("change", function () {
                if (form.dataset.pending === "1") {
                    select.value = lastValue;
                    return;
                }
                lastValue = select.value;
                if (!beginPending()) return;
                // native submit() does not fire "submit" in most browsers; allow
                // it through if a listener does run (jQuery / some WebViews).
                allowNativeSubmit = true;
                form.submit();
            });
        }

        window.addEventListener("pageshow", function () {
            clearPending();
            lastValue = select ? select.value : lastValue;
        });
    }
})();
