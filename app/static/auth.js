/* Site login prompt (see app/siteauth.py).
 * Wraps window.fetch: when the API answers 401 {error:"auth_required"}, show a
 * password dialog and, once logged in, re-send the SAME request. The dashboard's
 * many fetch() call sites stay unchanged. Styles are inline on purpose — Tailwind
 * only compiles classes it finds in the HTML files. */
(function () {
    const origFetch = window.fetch.bind(window);
    let prompt = null;   // one dialog for many concurrent 401s

    const css = `
    #nw-auth{position:fixed;inset:0;z-index:200;background:rgba(0,0,0,.72);backdrop-filter:blur(4px);display:flex;align-items:center;justify-content:center;padding:16px}
    #nw-auth .card{width:100%;max-width:380px;background:#0b1424;border:1px solid rgba(255,255,255,.12);border-radius:16px;padding:22px;color:#e2e8f0;font:14px system-ui,sans-serif;box-shadow:0 20px 60px rgba(0,0,0,.5)}
    #nw-auth h2{margin:0 0 6px;font-size:18px;font-weight:700}
    #nw-auth p{margin:0 0 14px;color:#94a3b8;line-height:1.45}
    #nw-auth code{font-size:12px;background:#050b16;border:1px solid rgba(255,255,255,.1);border-radius:6px;padding:6px 8px;display:block;margin-top:8px;color:#a5f3fc;word-break:break-all}
    #nw-auth input{width:100%;box-sizing:border-box;padding:11px 12px;border-radius:10px;border:1px solid rgba(255,255,255,.15);background:#050b16;color:#e2e8f0;font-size:15px}
    #nw-auth .err{color:#fda4af;min-height:18px;margin:8px 0 4px;font-size:13px}
    #nw-auth .row{display:flex;gap:8px;justify-content:flex-end;margin-top:6px}
    #nw-auth button{padding:9px 16px;border-radius:10px;border:1px solid rgba(255,255,255,.15);background:rgba(255,255,255,.06);color:#e2e8f0;font-weight:600;cursor:pointer}
    #nw-auth button.pri{background:#0891b2;border-color:#22d3ee;color:#fff}`;

    function esc(s) { return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

    function ask(info) {
        if (prompt) return prompt;
        prompt = new Promise(resolve => {
            if (!document.getElementById('nw-auth-css')) {
                const st = document.createElement('style');
                st.id = 'nw-auth-css'; st.textContent = css;
                document.head.appendChild(st);
            }
            const wrap = document.createElement('div');
            wrap.id = 'nw-auth';
            wrap.setAttribute('role', 'dialog');
            wrap.setAttribute('aria-modal', 'true');
            wrap.setAttribute('aria-labelledby', 'nw-auth-title');
            if (info.password_set === false) {
                wrap.innerHTML = `<div class="card"><h2 id="nw-auth-title">&#128274; Pi password needed</h2>
                    <p>Saved logins and settings on this Pi are locked, and no password has been set yet.
                    Set one from the central hub (site page &rarr; <b>Pi password</b>), or on the Pi:</p>
                    <code>docker exec -it netwatch python siteauth.py set-password</code>
                    <div class="row" style="margin-top:16px"><button type="button" data-x>Close</button></div></div>`;
            } else {
                wrap.innerHTML = `<form class="card"><h2 id="nw-auth-title">&#128274; Log in to this Pi</h2>
                    <p>Saved device logins and Pi settings need the site password.</p>
                    <input type="password" autocomplete="current-password" placeholder="Site password" aria-label="Site password" required>
                    <div class="err" aria-live="polite"></div>
                    <div class="row"><button type="button" data-x>Cancel</button><button class="pri" type="submit">Log in</button></div></form>`;
            }
            document.body.appendChild(wrap);
            const done = ok => { wrap.remove(); document.removeEventListener('keydown', onKey); prompt = null; resolve(ok); };
            const onKey = e => { if (e.key === 'Escape') done(false); };
            document.addEventListener('keydown', onKey);
            wrap.querySelector('[data-x]').onclick = () => done(false);
            wrap.addEventListener('click', e => { if (e.target === wrap) done(false); });
            const form = wrap.querySelector('form');
            if (!form) { wrap.querySelector('[data-x]').focus(); return; }
            const input = form.querySelector('input'), err = form.querySelector('.err'), btn = form.querySelector('.pri');
            input.focus();
            form.onsubmit = async e => {
                e.preventDefault();
                btn.disabled = true; err.textContent = '';
                try {
                    const r = await origFetch('/api/auth/login', {method: 'POST', credentials: 'same-origin',
                        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({password: input.value})});
                    const j = await r.json().catch(() => ({}));
                    if (r.ok && j.ok) return done(true);
                    err.textContent = j.error || ('Login failed (' + r.status + ')');
                } catch (x) { err.textContent = 'Could not reach the Pi.'; }
                btn.disabled = false; input.select();
            };
        });
        return prompt;
    }

    window.fetch = async function (input, init) {
        const res = await origFetch(input, init);
        if (res.status !== 401) return res;
        let body;
        try { body = await res.clone().json(); } catch (e) { return res; }
        if (!body || body.error !== 'auth_required') return res;
        if (!(await ask(body))) return res;
        return origFetch(input, init);
    };

    window.nwAuth = {
        state: () => origFetch('/api/auth/state').then(r => r.json()),
        logout: () => origFetch('/api/auth/logout', {method: 'POST'}).then(() => true),
        login: () => window.nwAuth.state().then(s => s.logged_in || ask(s)),
    };
})();
