/**
 * Fullscreen control for Leaflet maps (day map + lifetime map).
 *
 * Why not only zoom: in-page maps stay compact; owners want a one-click
 * large view without leaving the page. Uses the Fullscreen API when
 * available, otherwise a fixed CSS overlay fallback.
 */
(function (global) {
    "use strict";

    function isNativeFullscreen(el) {
        var current =
            document.fullscreenElement ||
            document.webkitFullscreenElement ||
            document.msFullscreenElement;
        return current === el;
    }

    function requestFs(el) {
        var req =
            el.requestFullscreen ||
            el.webkitRequestFullscreen ||
            el.msRequestFullscreen;
        if (!req) {
            return Promise.resolve(false);
        }
        try {
            var result = req.call(el);
            if (result && typeof result.then === "function") {
                return result.then(function () {
                    return true;
                }).catch(function () {
                    return false;
                });
            }
            return Promise.resolve(true);
        } catch (err) {
            return Promise.resolve(false);
        }
    }

    function exitFs() {
        var exit =
            document.exitFullscreen ||
            document.webkitExitFullscreen ||
            document.msExitFullscreen;
        if (
            !exit ||
            !(document.fullscreenElement || document.webkitFullscreenElement)
        ) {
            return Promise.resolve(false);
        }
        try {
            var result = exit.call(document);
            if (result && typeof result.then === "function") {
                return result.then(function () {
                    return true;
                }).catch(function () {
                    return false;
                });
            }
            return Promise.resolve(true);
        } catch (err) {
            return Promise.resolve(false);
        }
    }

    function refreshMap(map) {
        if (!map) return;
        // Leaflet measures the container; must rerun after size change
        setTimeout(function () {
            try {
                map.invalidateSize({ animate: false });
            } catch (err) {
                /* ignore */
            }
        }, 60);
        setTimeout(function () {
            try {
                map.invalidateSize({ animate: false });
            } catch (err) {
                /* ignore */
            }
        }, 250);
    }

    function setButtonState(button, active, labels) {
        if (!button) return;
        var enter = (labels && labels.enter) || "Fullscreen";
        var exit = (labels && labels.exit) || "Exit fullscreen";
        button.setAttribute("aria-pressed", active ? "true" : "false");
        button.setAttribute("title", active ? exit : enter);
        button.setAttribute("aria-label", active ? exit : enter);
        button.classList.toggle("is-active", !!active);
        // Leaflet bar control only — header expand keeps its SVG
        if (button.classList.contains("matesla-map-fs-btn")) {
            button.innerHTML = active
                ? '<span class="matesla-map-fs-icon" aria-hidden="true">✕</span>'
                : '<span class="matesla-map-fs-icon" aria-hidden="true">⛶</span>';
        }
    }

    /**
     * @param {L.Map} map
     * @param {HTMLElement} shellEl  Element that goes fullscreen (wraps the map div)
     * @param {{enter?: string, exit?: string, position?: string}} labels
     * @returns {{toggle: Function, isActive: Function}|null}
     */
    function attachMapFullscreen(map, shellEl, labels) {
        if (!map || !shellEl || typeof L === "undefined") return null;
        labels = labels || {};
        var position = labels.position || "topleft";
        var buttons = [];

        function isActive() {
            return (
                isNativeFullscreen(shellEl) ||
                shellEl.classList.contains("matesla-map-shell--fs")
            );
        }

        function syncAllButtons() {
            var active = isActive();
            buttons.forEach(function (btn) {
                setButtonState(btn, active, labels);
            });
            document.body.classList.toggle("matesla-map-fs-open", active);
            refreshMap(map);
        }

        function enterCssFallback() {
            shellEl.classList.add("matesla-map-shell--fs");
            syncAllButtons();
        }

        function exitCssFallback() {
            shellEl.classList.remove("matesla-map-shell--fs");
            syncAllButtons();
        }

        function toggle() {
            if (isActive()) {
                if (isNativeFullscreen(shellEl)) {
                    exitFs().then(function () {
                        // fullscreenchange will sync; ensure CSS fallback cleared
                        exitCssFallback();
                    });
                } else {
                    exitCssFallback();
                }
                return;
            }
            requestFs(shellEl).then(function (entered) {
                // Prefer native FS; CSS overlay if the API is missing or denied
                if (entered || isNativeFullscreen(shellEl)) {
                    syncAllButtons();
                } else {
                    enterCssFallback();
                }
            });
        }

        function onFsChange() {
            if (!isNativeFullscreen(shellEl)) {
                shellEl.classList.remove("matesla-map-shell--fs");
            }
            syncAllButtons();
        }

        document.addEventListener("fullscreenchange", onFsChange);
        document.addEventListener("webkitfullscreenchange", onFsChange);

        // Escape exits CSS fallback (native FS already handles Esc)
        function onKey(event) {
            if (event.key === "Escape" && shellEl.classList.contains("matesla-map-shell--fs")) {
                exitCssFallback();
            }
        }
        document.addEventListener("keydown", onKey);

        var Control = L.Control.extend({
            options: { position: position },
            onAdd: function () {
                var bar = L.DomUtil.create(
                    "div",
                    "leaflet-bar leaflet-control matesla-map-fs-control"
                );
                var link = L.DomUtil.create("a", "matesla-map-fs-btn", bar);
                link.href = "#";
                link.setAttribute("role", "button");
                setButtonState(link, false, labels);
                buttons.push(link);
                L.DomEvent.disableClickPropagation(bar);
                L.DomEvent.on(link, "click", function (event) {
                    L.DomEvent.preventDefault(event);
                    toggle();
                });
                return bar;
            },
        });
        map.addControl(new Control());

        // Optional external trigger (header expand button)
        if (labels.externalButton) {
            var external = labels.externalButton;
            if (typeof external === "string") {
                external = document.querySelector(external);
            }
            if (external) {
                setButtonState(external, false, labels);
                buttons.push(external);
                external.addEventListener("click", function (event) {
                    event.preventDefault();
                    toggle();
                });
            }
        }

        shellEl.classList.add("matesla-map-shell");
        return {
            toggle: toggle,
            isActive: isActive,
            refresh: function () {
                refreshMap(map);
            },
        };
    }

    global.mateslaAttachMapFullscreen = attachMapFullscreen;
})(window);
