/**
 * 管理后台 - 数据统计页逻辑
 * 接口全部经 AdminApi（js/admin-api.js），本文件不直接拼路径。
 * 每个指标都来自后端接口，接口失败时显示错误，不填充假数据。
 */
(async function () {
    'use strict';

    var me = await App.guard();
    if (!me) return;
    document.getElementById('layout').hidden = false;

    var $ = function (id) { return document.getElementById(id); };
    var esc = App.escape;
    var PAGE_SIZE = 20;
    var SOURCE_TEXT = { web: 'Web', feishu: '飞书', wechat: '微信' };
    var state = { days: 7, tab: 'history', page: 1, total: 0, rows: [] };

    $('sideName').textContent = me.display_name || me.name || me.username || '';
    $('navData').href = App.url.adminData();
    $('navUsers').href = App.url.adminUsers();
    $('logoutBtn').addEventListener('click', App.logout);

    $('detailModal').addEventListener('click', function (e) {
        if (e.target.id === 'detailModal' || e.target.hasAttribute('data-close')) $('detailModal').hidden = true;
    });

    // 加载用户列表到下拉框
    async function loadUserList() {
        try {
            var res = await AdminApi.listUsers({ page: 1, page_size: 100 });
            var users = res.users || [];
            var options = '<option value="">全部用户</option>';
            users.forEach(function (u) {
                var label = u.display_name || u.username || u.user_id;
                options += '<option value="' + esc(u.user_id) + '">' + esc(label) + '</option>';
            });
            $('fUserId').innerHTML = options;
        } catch (err) {
            console.error('加载用户列表失败:', err);
        }
    }
    loadUserList();

    function fmtNum(n) { return (n === undefined || n === null) ? '-' : Number(n).toLocaleString(); }
    function fmtRate(r) { return (r === undefined || r === null) ? '-' : (Number(r) * 100).toFixed(1) + '%'; }
    function failBox(el, err) { el.innerHTML = '<div class="empty">加载失败：' + esc(err.message) + '</div>'; }

    /**
     * 明细表的起始日期（对照表 v1 #12/#13：YYYY-MM-DD）。
     * 优先用 /api/admin/stats 返回的 range.start_date，保证与统计卡片口径一致；
     * stats 尚未返回时按本地日期推算（区间包含今天）。
     */
    var statsRange = null;
    function startTime(days) {
        if (statsRange && statsRange.days === days && statsRange.start_date) return statsRange.start_date;
        var d = new Date();
        d.setDate(d.getDate() - (days - 1));
        var p = function (n) { return String(n).padStart(2, '0'); };
        return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
    }

    // ---------- 统计（对照表 v1 #10：GET /api/admin/stats 一个接口返回全部）----------
    var STAT_IDS = ['sQueries', 'sActive', 'sLikeRate', 'sDislike', 'sSessions'];
    var BOX_IDS = ['trendChart', 'topList', 'topUsers', 'sourceList', 'wordCloud'];

    async function loadStats() {
        var days = state.days;
        STAT_IDS.forEach(function (id) { $(id).textContent = '…'; });
        BOX_IDS.forEach(function (id) { $(id).innerHTML = '<div class="empty">加载中…</div>'; });
        try {
            var d = await AdminApi.stats(days);
            if (days !== state.days) return;   // 用户已切换时间范围，丢弃过期结果
            var s = d.summary || {};
            statsRange = d.range ? { days: days, start_date: d.range.start_date } : null;

            $('sQueries').textContent = fmtNum(s.total_questions);
            $('sSessions').textContent = fmtNum(s.total_sessions);
            $('sActive').textContent = fmtNum(s.active_users);
            $('sUsers').textContent = '注册用户 ' + fmtNum(s.total_users);
            $('sLikeRate').textContent = fmtRate(s.like_rate);
            $('sLikeDetail').textContent = '点赞 ' + fmtNum(s.like_count) + ' / 点踩 ' + fmtNum(s.dislike_count);
            $('sDislike').textContent = fmtNum(s.dislike_count);
            $('rangeText').textContent = d.range ? (d.range.start_date + ' ~ ' + d.range.end_date) : '';

            // API 返回简化格式 {date, count}，映射为 {date, questions: count}
            var dailyData = (d.daily || []).map(function (item) {
                return { date: item.date, questions: item.count };
            });
            Charts.line($('trendChart'), dailyData, [
                { key: 'questions', label: '问答量', color: '#2563eb' }
            ]);
            Charts.bars($('topList'), d.top_questions || []);
            Charts.bars($('topUsers'), (d.top_users || []).map(function (u) {
                return { query: u.user_name || u.user_id || '（未关联账号）', count: u.count };
            }));
            var src = d.sources || {};
            Charts.bars($('sourceList'), Object.keys(SOURCE_TEXT).map(function (k) {
                return { query: SOURCE_TEXT[k], count: +src[k] || 0 };
            }).filter(function (x) { return x.count > 0; }));
        } catch (err) {
            if (err.status === 401) return;
            STAT_IDS.forEach(function (id) { $(id).textContent = '-'; });
            BOX_IDS.forEach(function (id) { failBox($(id), err); });
        }
    }

    // ---------- 词云 ----------
    async function loadWordCloud() {
        var days = state.days;
        $('wordCloud').innerHTML = '<div class="empty">加载中…</div>';
        try {
            var data = await AdminApi.wordcloud(days);
            if (days !== state.days) return;   // 用户已切换时间范围，丢弃过期结果

            if (!data || data.length === 0) {
                $('wordCloud').innerHTML = '<div class="empty">暂无数据</div>';
                return;
            }

            var chart = echarts.init($('wordCloud'));

            var option = {
                series: [{
                    type: 'wordCloud',
                    shape: 'circle',
                    sizeRange: [12, 60],
                    rotationRange: [-90, 90],
                    rotationStep: 45,
                    gridSize: 8,
                    drawOutOfBound: false,
                    textStyle: {
                        fontFamily: 'sans-serif',
                        fontWeight: 'bold',
                        color: function () {
                            var colors = ['#5470c6', '#91cc75', '#fac858', '#ee6666', '#73c0de'];
                            return colors[Math.floor(Math.random() * colors.length)];
                        }
                    },
                    data: data.map(function (item) {
                        return {
                            name: item.word,
                            value: item.count
                        };
                    })
                }]
            };

            chart.setOption(option);
        } catch (err) {
            if (err.status === 401) return;
            failBox($('wordCloud'), err);
        }
    }

    // ---------- 明细表 ----------
    var HEADS = {
        history: ['时间', '用户', '来源', '问题', '回答', '反馈', ''],
        feedback: ['时间', '用户', '来源', '类型', '问题', '反馈内容', '']
    };

    function renderHead() {
        $('detailHead').innerHTML = '<tr>' + HEADS[state.tab].map(function (h) { return '<th>' + h + '</th>'; }).join('') + '</tr>';
    }

    function userCell(r) { return esc(r.user_name || r.user_id || '（未关联账号）'); }
    /** #12 feedback_type: 'like' | 'dislike' | null */
    function feedbackBadge(t) {
        if (t === 'like') return '<span class="badge green">点赞</span>';
        if (t === 'dislike') return '<span class="badge red">点踩</span>';
        return '<span class="badge">无</span>';
    }

    function renderRows() {
        var cols = HEADS[state.tab].length;
        if (!state.rows.length) {
            $('detailRows').innerHTML = '<tr><td colspan="' + cols + '" class="empty">暂无数据</td></tr>';
            return;
        }
        $('detailRows').innerHTML = state.rows.map(function (r, i) {
            var cells;
            if (state.tab === 'history') {
                cells = [
                    esc(r.created_at), userCell(r), esc(SOURCE_TEXT[r.source] || r.source),
                    '<div class="clip">' + esc(r.query) + '</div>',
                    '<div class="clip">' + esc(r.answer) + '</div>',
                    feedbackBadge(r.feedback_type)
                ];
            } else {
                var like = r.feedback_type === 'like';
                cells = [
                    esc(r.created_at), userCell(r), esc(SOURCE_TEXT[r.source] || r.source),
                    like ? '<span class="badge green">点赞</span>' : '<span class="badge red">点踩</span>',
                    '<div class="clip">' + esc(r.query) + '</div>',
                    '<div class="clip">' + esc(r.feedback_text || '-') + '</div>'
                ];
            }
            cells.push('<button class="btn-link" type="button" data-idx="' + i + '">详情</button>');
            return '<tr>' + cells.map(function (c) { return '<td>' + c + '</td>'; }).join('') + '</tr>';
        }).join('');
    }

    function renderPager() {
        var pages = Math.max(1, Math.ceil(state.total / PAGE_SIZE));
        $('pagerInfo').textContent = '共 ' + state.total + ' 条，第 ' + state.page + ' / ' + pages + ' 页';
        $('prevBtn').disabled = state.page <= 1;
        $('nextBtn').disabled = state.page >= pages;
    }

    async function loadDetail() {
        renderHead();
        var cols = HEADS[state.tab].length;
        $('detailRows').innerHTML = '<tr><td colspan="' + cols + '" class="empty">加载中…</td></tr>';
        var params = {
            page: state.page,
            page_size: PAGE_SIZE,
            source: $('fSource').value,
            user_id: $('fUserId').value
        };
        // 时间区间：优先使用用户选择的日期，否则使用时间范围选项卡
        var startDate = $('fStartDate').value;
        var endDate = $('fEndDate').value;
        if (startDate) {
            params.start_time = startDate;
        } else {
            params.start_time = startTime(state.days);
        }
        if (endDate) {
            params.end_time = endDate;
        }
        var tab = state.tab, page = state.page;
        try {
            var res;
            if (state.tab === 'history') {
                params.keyword = $('fKeyword').value.trim();   // #12 支持 keyword，#13 不支持
                res = await AdminApi.chatHistory(params);
                state.rows = res.records;
            } else {
                params.feedback_type = $('fType').value;
                res = await AdminApi.feedback(params);
                state.rows = res.feedbacks;
            }
            if (tab !== state.tab || page !== state.page) return;   // 已切换，丢弃过期结果
            state.total = res.total;
            renderRows();
        } catch (err) {
            if (err.status === 401) return;
            state.rows = [];
            state.total = 0;
            $('detailRows').innerHTML = '<tr><td colspan="' + cols + '" class="empty">加载失败：' + esc(err.message) + '</td></tr>';
        }
        renderPager();
    }

    $('detailRows').addEventListener('click', function (e) {
        var btn = e.target.closest('[data-idx]');
        if (!btn) return;
        var r = state.rows[+btn.dataset.idx];
        if (!r) return;
        var html =
            '<div class="field"><label>时间 / 用户 / 来源</label><div>' + esc(r.created_at) + ' · ' + userCell(r) + ' · ' +
            esc(SOURCE_TEXT[r.source] || r.source) + (r.mode ? ' · 模式 ' + esc(r.mode) : '') + '</div></div>' +
            '<div class="field"><label>会话 ID</label><div style="font-family:var(--mono);font-size:12px">' + esc(r.session_id) + '</div></div>' +
            '<div class="field"><label>问题</label><div class="pre-block">' + esc(r.query) + '</div></div>' +
            '<div class="field"><label>回答</label><div class="pre-block">' + esc(r.answer) + '</div></div>' +
            (r.latency_ms != null ? '<div class="field"><label>耗时</label><div>' + esc(r.latency_ms) + ' ms</div></div>' : '');
        if (state.tab === 'feedback') {
            html += '<div class="field"><label>反馈</label><div class="pre-block">' +
                (r.feedback_type === 'like' ? '👍 点赞' : '👎 点踩') +
                (r.feedback_text ? '\n' + esc(r.feedback_text) : '') + '</div></div>';
        }
        $('detailBody').innerHTML = html;
        $('detailModal').hidden = false;
    });

    // ---------- 交互 ----------
    $('rangeTabs').addEventListener('click', function (e) {
        var b = e.target.closest('[data-days]');
        if (!b) return;
        $('rangeTabs').querySelectorAll('button').forEach(function (x) { x.classList.toggle('active', x === b); });
        state.days = +b.dataset.days;
        state.page = 1;
        loadAll();
    });

    $('detailTabs').addEventListener('click', function (e) {
        var b = e.target.closest('[data-tab]');
        if (!b) return;
        $('detailTabs').querySelectorAll('button').forEach(function (x) { x.classList.toggle('active', x === b); });
        state.tab = b.dataset.tab;
        state.page = 1;
        $('fType').hidden = state.tab !== 'feedback';
        $('fKeyword').hidden = state.tab !== 'history';
        loadDetail();
    });

    $('fSearch').addEventListener('click', function () { state.page = 1; loadDetail(); });
    ['fKeyword'].forEach(function (id) {
        $(id).addEventListener('keydown', function (e) { if (e.key === 'Enter') { state.page = 1; loadDetail(); } });
    });
    $('prevBtn').addEventListener('click', function () { if (state.page > 1) { state.page--; loadDetail(); } });
    $('nextBtn').addEventListener('click', function () { state.page++; loadDetail(); });

    function loadAll() {
        loadStats();
        loadWordCloud();
        loadDetail();
    }

    loadAll();
})();
