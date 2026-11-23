/**
 * 前端公共模块：登录态、请求封装、401 跳转、Toast
 *
 * 鉴权方式：服务端 cookie（HttpOnly，前端读不到）。
 *   - 是否已登录、是什么角色，一律以 GET /api/auth/me 的返回为准；
 *     前端不在 localStorage 里保存 token 或 role。
 *   - 写操作（POST/PUT/DELETE，含 login）带 CSRF 请求头：
 *     读非 HttpOnly cookie ai_csrf，写入 X-CSRF-Token（接口对照表 v1 第一节）。
 *
 * 页面通过 <body data-scope="user|admin"> 声明所属入口：
 *   - user  ：普通用户入口（/），401 回用户登录页
 *   - admin ：管理员入口（路径由后端配置），401 回管理员登录页
 * 管理员入口路径（配置项 system.admin_path，记为 A）不写死，也不由后端注入：
 *   管理页只会出现在 A/、A/users、A/data，前端从 location.pathname 推算 A。
 *   A/ 是管理员登录页。
 */
(function (global) {
    'use strict';

    // CSRF：double-submit（对照表 v1：cookie ai_csrf → 请求头 X-CSRF-Token）
    var CSRF = { cookie: 'ai_csrf', header: 'X-CSRF-Token' };
    var UNSAFE = { POST: 1, PUT: 1, PATCH: 1, DELETE: 1 };

    function stripSlash(p) { return p.replace(/\/+$/, ''); }
    function readCookie(name) {
        var m = document.cookie.match(new RegExp('(?:^|;\\s*)' + name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '=([^;]*)'));
        return m ? decodeURIComponent(m[1]) : '';
    }

    var currentUser = null;   // 仅存在内存里，刷新页面后重新向 /api/auth/me 获取

    var App = {
        scope: function () {
            return (document.body && document.body.dataset.scope) === 'admin' ? 'admin' : 'user';
        },

        /** 管理员入口根路径 A，如 "/admin"（无尾斜杠）。A/ → "/admin"，A/users → "/admin" */
        adminBase: function () {
            var path = location.pathname;
            return stripSlash(path.substring(0, path.lastIndexOf('/')));
        },

        url: {
            userLogin:   function () { return '/login'; },
            userChat:    function () { return '/'; },
            userProfile: function () { return '/profile'; },
            adminLogin:  function () { return App.adminBase() + '/'; },
            adminHome:   function () { return App.adminBase() + '/data'; },
            adminUsers:  function () { return App.adminBase() + '/users'; },
            adminData:   function () { return App.adminBase() + '/data'; }
        },

        loginUrl: function () {
            return App.scope() === 'admin' ? App.url.adminLogin() : App.url.userLogin();
        },

        user: function () { return currentUser; },

        /** 跳回当前入口的登录页，带上来源页以便登录后返回 */
        toLogin: function () {
            currentUser = null;
            var here = location.pathname + location.search;
            var target = App.loginUrl();
            if (here !== target) target += '?next=' + encodeURIComponent(here);
            location.replace(target);
        },

        /** 查询当前登录用户；未登录返回 null（不跳转） */
        fetchMe: async function () {
            try {
                var res = await App.get('/api/auth/me', { noRedirect: true });
                currentUser = (res && res.data) || null;
            } catch (e) {
                if (e.status !== 401) throw e;
                currentUser = null;
            }
            return currentUser;
        },

        /**
         * 页面守卫：以 /api/auth/me 为准。未登录 → 当前入口登录页；
         * 管理员入口要求 role=admin。真正的权限仍由后端接口校验。
         */
        guard: async function () {
            var me;
            try { me = await App.fetchMe(); }
            catch (e) { App.toast('无法获取登录状态：' + e.message, 'error'); return null; }
            if (!me) { App.toLogin(); return null; }
            if (App.scope() === 'admin' && me.role !== 'admin') { App.toLogin(); return null; }
            return me;
        },

        /** 写操作需要附带的头（聊天页等自行 fetch 时也用它） */
        csrfHeaders: function () {
            var h = {};
            var t = readCookie(CSRF.cookie);
            if (t) h[CSRF.header] = t;
            return h;
        },

        /**
         * 统一请求。后端格式 {success, message, data, code}；
         * 旧的聊天/反馈接口返回 {error}，一起兼容。
         * 401 → 跳当前入口登录页（opts.noRedirect 时只抛错）。
         */
        request: async function (method, path, body, opts) {
            opts = opts || {};
            var headers = { 'Accept': 'application/json' };
            if (body !== undefined) headers['Content-Type'] = 'application/json';
            if (UNSAFE[method]) Object.assign(headers, App.csrfHeaders());

            var resp;
            try {
                resp = await fetch(path, {
                    method: method,
                    headers: headers,
                    credentials: 'same-origin',
                    body: body === undefined ? undefined : JSON.stringify(body)
                });
            } catch (e) {
                throw new ApiError('网络异常，请检查连接后重试', 0, 'NETWORK_ERROR');
            }

            var payload = null;
            try { payload = await resp.json(); } catch (e) { payload = null; }

            if (resp.status === 401 && !opts.noRedirect) {
                App.toLogin();
                throw new ApiError((payload && payload.message) || '登录已过期，请重新登录', 401, 'UNAUTHORIZED');
            }
            if (!resp.ok || (payload && payload.success === false)) {
                var msg = (payload && (payload.message || payload.error)) || ('请求失败（HTTP ' + resp.status + '）');
                throw new ApiError(msg, resp.status, payload && payload.code);
            }
            return payload;
        },

        get:  function (path, opts) { return App.request('GET', path, undefined, opts); },
        post: function (path, body, opts) { return App.request('POST', path, body === undefined ? {} : body, opts); },
        put:  function (path, body, opts) { return App.request('PUT', path, body === undefined ? {} : body, opts); },
        del:  function (path, opts) { return App.request('DELETE', path, undefined, opts); },

        qs: function (params) {
            var parts = [];
            Object.keys(params || {}).forEach(function (k) {
                var v = params[k];
                if (v === undefined || v === null || v === '') return;
                parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(v));
            });
            return parts.length ? '?' + parts.join('&') : '';
        },

        logout: async function () {
            try { await App.post('/api/auth/logout', {}, { noRedirect: true }); } catch (e) { /* 照常退出 */ }
            currentUser = null;
            location.replace(App.loginUrl());
        },

        toast: function (message, type) {
            var host = document.querySelector('.toast-host');
            if (!host) {
                host = document.createElement('div');
                host.className = 'toast-host';
                document.body.appendChild(host);
            }
            var el = document.createElement('div');
            el.className = 'toast ' + (type || '');
            el.textContent = message;
            host.appendChild(el);
            setTimeout(function () { el.remove(); }, 3200);
        },

        escape: function (s) {
            return String(s == null ? '' : s)
                .replace(/&/g, '&amp;').replace(/</g, '&lt;')
                .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        },

        /** 登录后返回地址：只接受同源相对路径，防止开放重定向 */
        nextUrl: function (fallback) {
            var next = new URLSearchParams(location.search).get('next');
            if (next && next.charAt(0) === '/' && next.charAt(1) !== '/' && next.indexOf('\\') < 0) return next;
            return fallback;
        }
    };

    function ApiError(message, status, code) {
        this.name = 'ApiError';
        this.message = message;
        this.status = status;
        this.code = code;
    }
    ApiError.prototype = Object.create(Error.prototype);

    global.App = App;
    global.ApiError = ApiError;
})(window);
