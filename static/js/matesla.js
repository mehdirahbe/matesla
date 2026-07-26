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
})();
