/* ==========================================================================
   BetterCallTrader — shared console runtime
   - injects the shared top bar / nav into #bctHeader
   - language switch (HR/EN) persisted in localStorage
   - dashboard token handling, JSON api() helper, toasts, formatters
   Pages call BCT.init({ page, i18n, onRefresh, poll }) once, at the end of
   their inline script.
   ========================================================================== */
(function () {
    "use strict";

    const LANG_KEY = "bettercalltrader_lang";
    const TOKEN_KEY = "bettercalltrader_token";

    const NAV = [
        { key: "command", href: "/", icon: "fa-gauge-high" },
        { key: "analytics", href: "/analytics", icon: "fa-chart-line" },
        { key: "risk", href: "/risk", icon: "fa-shield-halved" },
        { key: "wallets", href: "/wallets", icon: "fa-wallet" },
        { key: "backtest", href: "/backtest", icon: "fa-flask-vial" },
        { key: "ml", href: "/ml", icon: "fa-brain" },
        { key: "system", href: "/system", icon: "fa-server" },
        { key: "settings", href: "/settings", icon: "fa-sliders" }
    ];

    const I18N = {
        hr: {
            "nav.command": "Command Center",
            "nav.analytics": "Analitika",
            "nav.risk": "Risk desk",
            "nav.wallets": "Novčanici",
            "nav.backtest": "Backtest lab",
            "nav.ml": "ML / AI lab",
            "nav.system": "Sustav / ops",
            "nav.settings": "Postavke",

            "app.version": "v12 · paper",
            "app.tagline": "Solana paper trading · risk engine, forenzika i analitika u jednom konzolu",

            "action.token": "Token",
            "action.refresh": "Osvježi",

            "status.connecting": "Spajam se",
            "status.live": "Live sync",
            "status.offline": "Offline",

            "common.loading": "Učitavam…",
            "common.empty": "Nema podataka",
            "common.error": "Greška",
            "common.save": "Spremi",
            "common.cancel": "Odustani",
            "common.remove": "Ukloni",
            "common.add": "Dodaj",
            "common.refresh": "Osvježi",
            "common.total": "Ukupno",
            "common.name": "Naziv",
            "common.value": "Vrijednost",
            "common.status": "Status",
            "common.time": "Vrijeme",
            "common.never": "nikad",
            "common.na": "N/A",
            "common.on": "UKLJ",
            "common.off": "ISKLJ",
            "common.yes": "da",
            "common.no": "ne",

            "toast.tokenSaved": "Token spremljen. Osvježavam…",
            "toast.unauthorized": "Neovlašteno: postavi dashboard token",
            "toast.totpInvalid": "Neispravan 2FA kod",
            "prompt.totp": "2FA kod (6 znamenki):",
            "toast.syncFail": "Sinkronizacija nije uspjela",
            "toast.failed": "Radnja nije uspjela",
            "toast.saved": "Spremljeno",

            "footer.paper": "Paper mode · live trgovanje traži eksplicitnu env potvrdu",
            "footer.auth": "auth",
            "footer.sync": "sync",

            "risk.high": "HIGH RISK",
            "risk.standard": "STANDARD"
        },
        en: {
            "nav.command": "Command Center",
            "nav.analytics": "Analytics",
            "nav.risk": "Risk desk",
            "nav.wallets": "Wallets",
            "nav.backtest": "Backtest lab",
            "nav.ml": "ML / AI lab",
            "nav.system": "System / ops",
            "nav.settings": "Settings",

            "app.version": "v12 · paper",
            "app.tagline": "Solana paper trading · risk engine, forensics and analytics in one console",

            "action.token": "Token",
            "action.refresh": "Refresh",

            "status.connecting": "Connecting",
            "status.live": "Live sync",
            "status.offline": "Offline",

            "common.loading": "Loading…",
            "common.empty": "No data",
            "common.error": "Error",
            "common.save": "Save",
            "common.cancel": "Cancel",
            "common.remove": "Remove",
            "common.add": "Add",
            "common.refresh": "Refresh",
            "common.total": "Total",
            "common.name": "Name",
            "common.value": "Value",
            "common.status": "Status",
            "common.time": "Time",
            "common.never": "never",
            "common.na": "N/A",
            "common.on": "ON",
            "common.off": "OFF",
            "common.yes": "yes",
            "common.no": "no",

            "toast.tokenSaved": "Token saved. Reloading…",
            "toast.unauthorized": "Unauthorized: set the dashboard token",
            "toast.totpInvalid": "Invalid 2FA code",
            "prompt.totp": "2FA code (6 digits):",
            "toast.syncFail": "Sync failed",
            "toast.failed": "Action failed",
            "toast.saved": "Saved",

            "footer.paper": "Paper mode · live trading requires explicit env confirmation",
            "footer.auth": "auth",
            "footer.sync": "sync",

            "risk.high": "HIGH RISK",
            "risk.standard": "STANDARD"
        }
    };

    let currentLang = "hr";
    try {
        const stored = localStorage.getItem(LANG_KEY);
        if (I18N[stored]) currentLang = stored;
    } catch (err) { /* ignore */ }

    function token() {
        try {
            const fromUrl = new URL(window.location.href).searchParams.get("token");
            if (fromUrl) {
                localStorage.setItem(TOKEN_KEY, fromUrl);
                return fromUrl;
            }
            return localStorage.getItem(TOKEN_KEY) || "";
        } catch (err) {
            return "";
        }
    }

    function authHeaders() {
        const value = token();
        return value ? { "X-Auth-Token": value } : {};
    }

    function t(key, vars) {
        const bucket = I18N[currentLang] || {};
        let text = bucket[key] ?? (I18N.hr ? I18N.hr[key] : undefined) ?? key;
        if (vars && typeof text === "string") {
            Object.keys(vars).forEach((name) => {
                text = text.split("{" + name + "}").join(String(vars[name]));
            });
        }
        return text;
    }

    function extendI18n(extra) {
        if (!extra) return;
        ["hr", "en"].forEach((lang) => {
            if (!I18N[lang] || !extra[lang]) return;
            Object.keys(extra[lang]).forEach((key) => { I18N[lang][key] = extra[lang][key]; });
        });
    }

    function applyI18n() {
        document.querySelectorAll("[data-i18n]").forEach((el) => {
            el.textContent = t(el.dataset.i18n);
        });
        document.querySelectorAll("[data-i18n-title]").forEach((el) => {
            el.title = t(el.dataset.i18nTitle);
        });
        document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
            el.placeholder = t(el.dataset.i18nPlaceholder);
        });
        document.documentElement.lang = currentLang;
        document.querySelectorAll("[data-lang]").forEach((btn) => {
            const active = btn.dataset.lang === currentLang;
            btn.classList.toggle("active", active);
            btn.setAttribute("aria-pressed", String(active));
        });
    }

    /* ------------------------------------------------------------ markup */
    function navHref(item) {
        // Opened straight from disk, server routes do not exist; point at the
        // sibling HTML files instead (same fallback the two original pages use).
        try {
            if (window.location.protocol === "file:") {
                return item.key === "command" ? "command_center.html" : `${item.key}.html`;
            }
        } catch (err) { /* ignore */ }
        return item.href;
    }

    function navMarkup(page) {
        return NAV.map((item) => {
            const active = item.key === page;
            return `<a href="${navHref(item)}" class="nav-tab${active ? " active" : ""}" data-nav="${item.key}"${active ? ' aria-current="page"' : ""}>` +
                `<i class="fa-solid ${item.icon}"></i><span data-i18n="nav.${item.key}"></span></a>`;
        }).join("");
    }

    function headerMarkup(page) {
        return `
        <div class="max-w-[1600px] mx-auto px-4 lg:px-6 py-3">
            <div class="flex flex-wrap items-center justify-between gap-3">
                <a href="/" class="flex items-center gap-3 min-w-0">
                    <div class="w-10 h-10 rounded-xl bg-gradient-to-br from-sol-purple to-sol-green p-[1px] flex items-center justify-center shadow-lg shrink-0">
                        <div class="w-full h-full bg-sol-dark rounded-[11px] flex items-center justify-center">
                            <i class="fa-solid fa-robot text-lg sol-gradient-text"></i>
                        </div>
                    </div>
                    <div class="min-w-0">
                        <div class="flex items-center gap-2 flex-wrap">
                            <h1 class="font-bold text-base sm:text-lg tracking-tight text-white">BetterCallTrader</h1>
                            <span class="text-[10px] font-mono uppercase px-2 py-0.5 rounded-full bg-sol-purple/20 text-sol-purple border border-sol-purple/40 font-semibold" data-i18n="app.version">v12 · paper</span>
                            <span id="connPill" class="pill"><i class="dot"></i><span id="connText" data-i18n="status.connecting"></span></span>
                        </div>
                        <p class="text-[11px] text-slate-400 mt-0.5 truncate" data-i18n="app.tagline"></p>
                    </div>
                </a>
                <div class="flex flex-wrap items-center gap-2">
                    <div class="flex items-center rounded-lg border border-sol-border bg-sol-dark overflow-hidden" role="group" aria-label="Language">
                        <button type="button" class="lang-btn" data-lang="hr">HR</button>
                        <button type="button" class="lang-btn" data-lang="en">EN</button>
                    </div>
                    <button type="button" id="tokenBtn" class="btn" title="Set dashboard token">
                        <i class="fa-solid fa-key text-sol-purple"></i><span data-i18n="action.token"></span>
                    </button>
                    <button type="button" id="refreshBtn" class="btn">
                        <i class="fa-solid fa-rotate text-sol-green"></i><span data-i18n="action.refresh"></span>
                    </button>
                </div>
            </div>
            <div class="flex flex-wrap items-center justify-between gap-2 mt-2.5 pt-2.5 border-t border-sol-border/60">
                <nav class="flex flex-wrap items-center gap-1" aria-label="Main">${navMarkup(page)}</nav>
                <div id="headerMeta" class="tiny-mono"></div>
            </div>
        </div>`;
    }

    /* ------------------------------------------------------------ helpers */
    function fmtSol(value, digits = 4, signed = false) {
        const n = Number(value);
        if (!Number.isFinite(n)) return "—";
        const text = n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
        return signed && n > 0 ? `+${text}` : text;
    }

    function fmtPct(value, digits = 1, signed = false) {
        const n = Number(value);
        if (!Number.isFinite(n)) return "—";
        const text = `${n.toFixed(digits)}%`;
        return signed && n > 0 ? `+${text}` : text;
    }

    function fmtInt(value) {
        const n = Number(value);
        return Number.isFinite(n) ? n.toLocaleString("en-US") : "—";
    }

    function escapeHtml(value) {
        return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    }

    function signedClass(value) {
        const n = Number(value);
        if (!Number.isFinite(n) || n === 0) return "";
        return n > 0 ? "pos" : "neg";
    }

    function toast(message) {
        let node = document.getElementById("toast");
        if (!node) {
            node = document.createElement("div");
            node.id = "toast";
            node.className = "toast";
            node.setAttribute("role", "status");
            node.setAttribute("aria-live", "polite");
            document.body.appendChild(node);
        }
        node.textContent = message;
        node.classList.add("show");
        clearTimeout(node._t);
        node._t = setTimeout(() => node.classList.remove("show"), 3200);
    }

    async function api(path, options = {}) {
        const retried = Boolean(options._totpRetried);
        const headers = Object.assign({}, options.headers || {}, authHeaders());
        const init = Object.assign({ cache: "no-store" }, options, { headers });
        delete init._totpRetried;
        const response = await fetch(path, init);
        let body = null;
        try { body = await response.json(); } catch (err) { /* non-JSON */ }
        if (response.status === 401) {
            const errorCode = body && body.error;
            if (!retried && (errorCode === "totp_required" || errorCode === "invalid_totp")) {
                const supplied = prompt(t("prompt.totp"), "");
                if (supplied && /^\d{6}$/.test(supplied.trim())) {
                    return api(path, Object.assign({}, options, {
                        _totpRetried: true,
                        headers: Object.assign({}, options.headers || {}, { "X-TOTP": supplied.trim() })
                    }));
                }
                toast(t("toast.totpInvalid"));
            }
            const error = new Error(t("toast.unauthorized"));
            error.status = 401;
            throw error;
        }
        if (!response.ok) {
            const error = new Error((body && body.error) || `${path} → HTTP ${response.status}`);
            error.status = response.status;
            error.body = body;
            throw error;
        }
        return body;
    }

    function setConnection(ok) {
        const pill = document.getElementById("connPill");
        if (!pill) return;
        pill.classList.toggle("live", Boolean(ok));
        pill.classList.toggle("offline", !ok);
        const text = document.getElementById("connText");
        if (text) text.textContent = ok ? t("status.live") : t("status.offline");
    }

    function setLang(lang) {
        currentLang = I18N[lang] ? lang : "hr";
        try { localStorage.setItem(LANG_KEY, currentLang); } catch (err) { /* ignore */ }
        applyI18n();
        document.title = `BetterCallTrader — ${t("nav." + runtime.page)}`;
        if (typeof runtime.onLangChange === "function") runtime.onLangChange(currentLang);
        if (typeof runtime.onRefresh === "function") runtime.run();
    }

    /* ------------------------------------------------------------ runtime */
    const runtime = {
        page: "command",
        onRefresh: null,
        onLangChange: null,
        poll: 0,
        busy: false,
        timer: null,
        async run() {
            if (this.busy || typeof this.onRefresh !== "function") return;
            this.busy = true;
            const btn = document.getElementById("refreshBtn");
            if (btn) btn.disabled = true;
            try {
                await this.onRefresh();
                this.setConnection(true);
            } catch (err) {
                this.setConnection(false);
                toast(err.message || t("toast.syncFail"));
            } finally {
                this.busy = false;
                if (btn) btn.disabled = false;
                const meta = document.getElementById("headerMeta");
                if (meta) meta.textContent = `${t("footer.sync")}: ${new Date().toLocaleTimeString()}`;
                const footerSync = document.getElementById("footerSync");
                if (footerSync) footerSync.textContent = new Date().toLocaleTimeString();
            }
        },
        setConnection
    };

    function init(options = {}) {
        runtime.page = options.page || "command";
        runtime.onRefresh = typeof options.onRefresh === "function" ? options.onRefresh : null;
        runtime.onLangChange = typeof options.onLangChange === "function" ? options.onLangChange : null;
        runtime.poll = Number(options.poll) || 0;
        extendI18n(options.i18n);

        const host = document.getElementById("bctHeader");
        if (host) host.innerHTML = headerMarkup(runtime.page);
        applyI18n();
        document.title = `BetterCallTrader — ${t("nav." + runtime.page)}`;

        document.querySelectorAll("[data-lang]").forEach((btn) => {
            btn.addEventListener("click", () => setLang(btn.dataset.lang));
        });

        const tokenBtn = document.getElementById("tokenBtn");
        if (tokenBtn) {
            tokenBtn.addEventListener("click", () => {
                const current = localStorage.getItem(TOKEN_KEY) || "";
                const next = prompt("Dashboard token (leave empty to clear):", current);
                if (next === null) return;
                if (next.trim()) localStorage.setItem(TOKEN_KEY, next.trim());
                else localStorage.removeItem(TOKEN_KEY);
                toast(t("toast.tokenSaved"));
                setTimeout(() => window.location.reload(), 400);
            });
        }

        const refreshBtn = document.getElementById("refreshBtn");
        if (refreshBtn) refreshBtn.addEventListener("click", () => runtime.run());

        if (runtime.poll) {
            clearInterval(runtime.timer);
            runtime.timer = setInterval(() => {
                if (!document.hidden) runtime.run();
            }, runtime.poll);
        }

        document.addEventListener("visibilitychange", () => {
            if (!document.hidden) runtime.run();
        });

        runtime.run();
        return BCT;
    }

    const BCT = {
        t,
        init,
        api,
        toast,
        token,
        authHeaders,
        applyI18n,
        extendI18n,
        setLang,
        setConnection,
        refresh: () => runtime.run(),
        fmtSol,
        fmtPct,
        fmtInt,
        escapeHtml,
        signedClass,
        get lang() { return currentLang; },
        get runtime() { return runtime; }
    };

    window.BCT = BCT;
})();
