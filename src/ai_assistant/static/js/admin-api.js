/**
 * 管理后台接口集中定义（按 Fix-Auth-Agent 接口对照表 v1）
 *
 * 管理后台用到的路径、参数名、字段名全部集中在本文件，页面只调用这里的函数。
 * 后端接口有变更时只改这里。
 *
 *   #5  GET  /api/admin/users            ?page&page_size&keyword&role&status
 *   #6  POST /api/admin/users            {username, password?, display_name?, role?}
 *   #7  PUT  /api/admin/users/<id>       {display_name?, role?, is_active?}
 *   #8  POST /api/admin/users/<id>/reset-password  {new_password?}
 *   #10 GET  /api/admin/stats            ?days=7|30|90
 *   #12 GET  /api/chat-history           ?page&page_size&user_id&session_id&source&keyword&start_time&end_time
 *   #13 GET  /api/feedback               ?page&page_size&user_id&session_id&feedback_type&source&start_time&end_time
 *
 * 用户对象统一为 UserPublic：
 *   {user_id, username, display_name, role, is_active, feishu_bound, created_at, updated_at, last_login_at}
 * 问答字段名为 answer（不是 response）。
 */
(function (global) {
    'use strict';

    var App = global.App;

    function enc(id) { return encodeURIComponent(id); }

    var AdminApi = {
        // ---------- 用户管理 ----------
        listUsers: async function (p) {
            var res = await App.get('/api/admin/users' + App.qs({
                page: p.page, page_size: p.page_size,
                keyword: p.keyword, role: p.role, status: p.status
            }));
            var d = res.data || {};
            return { users: d.users || [], total: d.total || 0, page: d.page, page_size: d.page_size };
        },
        /** 201，data 为 UserPublic；仅自动生成密码时多一个 data.password */
        createUser: function (body) {
            return App.post('/api/admin/users', body);
        },
        /** 200，data 为 UserPublic */
        updateUser: function (userId, body) {
            return App.put('/api/admin/users/' + enc(userId), body);
        },
        /** 停用/启用走 PUT is_active（DELETE 返回 405） */
        setUserActive: function (userId, active) {
            return AdminApi.updateUser(userId, { is_active: !!active });
        },
        /** 200，data {password?}（仅自动生成时返回） */
        resetPassword: function (userId, newPassword) {
            return App.post('/api/admin/users/' + enc(userId) + '/reset-password',
                newPassword ? { new_password: newPassword } : {});
        },

        // ---------- 统计（单接口）----------
        /**
         * data: {range:{start_date,end_date},
         *        summary:{total_users, active_users, total_questions, total_sessions, like_count, dislike_count, like_rate},
         *        daily:[{date, questions, active_users, likes, dislikes}],
         *        top_questions:[{query, count}], top_users:[{user_id, user_name, count}],
         *        sources:{web, feishu, wechat}}
         */
        stats: async function (days) {
            var res = await App.get('/api/admin/stats' + App.qs({ days: days }));
            return res.data || {};
        },

        // ---------- 明细 ----------
        /** data {records:[{record_id, session_id, user_id, user_name, query, answer, mode, source, latency_ms, created_at, feedback_type}], total, page, page_size} */
        chatHistory: async function (params) {
            var res = await App.get('/api/data/chat-history' + App.qs(params));
            var d = res.data || {};
            return { records: d.records || [], total: d.total || 0 };
        },
        /** data {feedbacks:[{feedback_id, record_id, session_id, user_id, user_name, feedback_type, feedback_text, source, created_at, query, answer}], total, page, page_size, stats} */
        feedback: async function (params) {
            var res = await App.get('/api/data/feedback' + App.qs(params));
            var d = res.data || {};
            return { feedbacks: d.feedbacks || [], total: d.total || 0, stats: d.stats || {} };
        },

        // ---------- 词云 ----------
        /** data [{word, count}] */
        wordcloud: async function (days) {
            var res = await App.get('/api/admin/wordcloud' + App.qs({ days: days }));
            return res.data || [];
        }
    };

    global.AdminApi = AdminApi;
})(window);
