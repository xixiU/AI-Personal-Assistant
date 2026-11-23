/**
 * 管理后台 - 用户管理页逻辑
 * 接口全部经 AdminApi（js/admin-api.js），本文件不直接拼路径。
 */
(async function () {
    'use strict';

    var me = await App.guard();
    if (!me) return;
    document.getElementById('layout').hidden = false;

    var $ = function (id) { return document.getElementById(id); };
    var ROLE_TEXT = { admin: '管理员', user: '普通用户' };
    var PAGE_SIZE = 20;
    var state = { page: 1, total: 0, role: '', status: '', keyword: '', users: [] };
    var editing = null;   // 当前编辑的用户；null 表示新建

    $('sideName').textContent = me.display_name || me.username || '';
    $('navData').href = App.url.adminData();
    $('navUsers').href = App.url.adminUsers();
    $('logoutBtn').addEventListener('click', App.logout);

    // ---------- 弹窗 ----------
    function openModal(id) { $(id).hidden = false; }
    function closeModal(id) { $(id).hidden = true; }
    document.querySelectorAll('.modal-mask').forEach(function (mask) {
        mask.addEventListener('click', function (e) {
            if (e.target === mask || e.target.hasAttribute('data-close')) mask.hidden = true;
        });
    });
    function showSecret(title, value) {
        $('secretTitle').textContent = title;
        $('secretValue').textContent = value;
        openModal('secretModal');
    }
    $('copySecret').addEventListener('click', function () {
        var v = $('secretValue').textContent;
        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(v).then(function () { App.toast('已复制', 'success'); });
        } else {
            // 内网 http 环境没有 clipboard API，选中文本让用户手动复制
            var range = document.createRange();
            range.selectNodeContents($('secretValue'));
            var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
            App.toast('已选中，请按 Ctrl+C 复制');
        }
    });

    // ---------- 列表 ----------
    function rowHtml(u) {
        var isSelf = u.user_id === me.user_id;
        var status = u.is_active
            ? '<span class="badge green">正常</span>'
            : '<span class="badge red">已停用</span>';
        var role = '<span class="badge ' + (u.role === 'admin' ? 'blue' : '') + '">' + App.escape(ROLE_TEXT[u.role] || u.role) + '</span>';
        var toggle = isSelf ? '' :
            '<button class="btn-link ' + (u.is_active ? 'danger' : '') + '" data-act="toggle">' + (u.is_active ? '停用' : '启用') + '</button>';
        return '<tr data-id="' + App.escape(u.user_id) + '">' +
            '<td>' + App.escape(u.username) + (isSelf ? ' <span class="badge">我</span>' : '') + '</td>' +
            '<td>' + App.escape(u.display_name || '—') + '</td>' +
            '<td>' + role + '</td>' +
            '<td>' + status + '</td>' +
            '<td>' + (u.feishu_bound ? '<span class="badge blue">已绑定</span>' : '<span class="badge">未绑定</span>') + '</td>' +
            '<td>' + App.escape(u.created_at || '—') + '</td>' +
            '<td>' + App.escape(u.last_login_at || '从未登录') + '</td>' +
            '<td class="actions">' +
                '<button class="btn-link" data-act="edit">编辑</button>' +
                '<button class="btn-link" data-act="reset">重置密码</button>' +
                toggle +
            '</td></tr>';
    }

    async function load() {
        var tbody = $('userRows');
        tbody.innerHTML = '<tr><td colspan="8" class="empty">加载中…</td></tr>';
        try {
            var r = await AdminApi.listUsers({
                page: state.page, page_size: PAGE_SIZE, role: state.role, status: state.status, keyword: state.keyword
            });
            state.users = r.users;
            state.total = r.total;
            tbody.innerHTML = r.users.length
                ? r.users.map(rowHtml).join('')
                : '<tr><td colspan="8" class="empty">没有符合条件的用户</td></tr>';
        } catch (err) {
            if (err.status === 401) return;
            tbody.innerHTML = '<tr><td colspan="8" class="empty">加载失败：' + App.escape(err.message) + '</td></tr>';
        }
        var pages = Math.max(1, Math.ceil(state.total / PAGE_SIZE));
        $('pagerInfo').textContent = '共 ' + state.total + ' 个用户，第 ' + state.page + ' / ' + pages + ' 页';
        $('prevBtn').disabled = state.page <= 1;
        $('nextBtn').disabled = state.page >= pages;
    }

    function search() {
        state.page = 1;
        state.keyword = $('keyword').value.trim();
        state.role = $('roleFilter').value;
        state.status = $('statusFilter').value;
        load();
    }
    $('searchBtn').addEventListener('click', search);
    $('roleFilter').addEventListener('change', search);
    $('statusFilter').addEventListener('change', search);
    $('keyword').addEventListener('keydown', function (e) { if (e.key === 'Enter') search(); });
    $('prevBtn').addEventListener('click', function () { if (state.page > 1) { state.page--; load(); } });
    $('nextBtn').addEventListener('click', function () { state.page++; load(); });

    // ---------- 新建 / 编辑 ----------
    function formErr(msg) { $('userFormError').textContent = msg; $('userFormError').hidden = !msg; }

    function openUserForm(user) {
        editing = user || null;
        formErr('');
        $('userModalTitle').textContent = user ? '编辑用户' : '新建用户';
        $('fUsername').value = user ? user.username : '';
        $('fUsername').readOnly = !!user;
        $('fDisplayName').value = user ? user.display_name : '';
        $('fRole').value = user ? user.role : 'user';
        // 不能改自己的角色，避免管理员把自己降级后被锁在后台外
        $('fRole').disabled = !!(user && user.user_id === me.user_id);
        $('fPassword').value = '';
        $('pwdField').hidden = !!user;
        openModal('userModal');
        (user ? $('fDisplayName') : $('fUsername')).focus();
    }
    $('createBtn').addEventListener('click', function () { openUserForm(null); });

    $('userForm').addEventListener('submit', async function (e) {
        e.preventDefault();
        var displayName = $('fDisplayName').value.trim();
        var role = $('fRole').value;
        var btn = $('userSubmit');

        btn.disabled = true;
        try {
            if (editing) {
                var body = { display_name: displayName };
                if (editing.user_id !== me.user_id) body.role = role;
                await AdminApi.updateUser(editing.user_id, body);
                closeModal('userModal');
                App.toast('已保存', 'success');
            } else {
                var username = $('fUsername').value.trim();
                var password = $('fPassword').value.trim();
                if (username.length < 3 || username.length > 32) { btn.disabled = false; return formErr('用户名长度必须在 3–32 之间'); }
                if (password && password.length < 6) { btn.disabled = false; return formErr('初始密码至少 6 位'); }
                var body2 = { username: username, display_name: displayName, role: role };
                if (password) body2.password = password;
                var res = await AdminApi.createUser(body2);
                closeModal('userModal');
                // 后端仅在自动生成密码时返回 data.password
                if (res.data && res.data.password) showSecret('用户 ' + username + ' 的初始密码', res.data.password);
                else App.toast('用户已创建', 'success');
            }
            load();
        } catch (err) {
            if (err.status !== 401) formErr(err.message);
        } finally {
            btn.disabled = false;
        }
    });

    // ---------- 重置密码 ----------
    var resetting = null;
    function resetErr(msg) { $('resetError').textContent = msg; $('resetError').hidden = !msg; }

    $('resetForm').addEventListener('submit', async function (e) {
        e.preventDefault();
        var pwd = $('rPassword').value.trim();
        if (pwd && pwd.length < 6) return resetErr('新密码至少 6 位');
        var btn = $('resetSubmit');
        btn.disabled = true;
        try {
            var res = await AdminApi.resetPassword(resetting.user_id, pwd);
            closeModal('resetModal');
            if (res.data && res.data.password) showSecret(resetting.username + ' 的新密码', res.data.password);
            else App.toast('密码已重置', 'success');
        } catch (err) {
            if (err.status !== 401) resetErr(err.message);
        } finally {
            btn.disabled = false;
        }
    });

    // ---------- 行内操作 ----------
    $('userRows').addEventListener('click', async function (e) {
        var btn = e.target.closest('[data-act]');
        if (!btn) return;
        var id = btn.closest('tr').dataset.id;
        var user = state.users.find(function (u) { return u.user_id === id; });
        if (!user) return;

        var act = btn.dataset.act;
        if (act === 'edit') {
            openUserForm(user);
        } else if (act === 'reset') {
            resetting = user;
            resetErr('');
            $('resetTarget').textContent = user.username;
            $('rPassword').value = '';
            openModal('resetModal');
            $('rPassword').focus();
        } else if (act === 'toggle') {
            var to = !user.is_active;
            if (!to && !confirm('确定停用用户 ' + user.username + '？停用后该账号无法登录。')) return;
            btn.disabled = true;
            try {
                await AdminApi.setUserActive(user.user_id, to);
                App.toast(to ? '已启用' : '已停用', 'success');
                load();
            } catch (err) {
                if (err.status !== 401) App.toast(err.message, 'error');
                btn.disabled = false;
            }
        }
    });

    load();
})();
