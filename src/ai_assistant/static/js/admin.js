/**
 * 管理后台逻辑模块
 * 提供用户管理、数据查看等功能的 API 调用
 */

const AdminAPI = {
    /**
     * 获取用户列表
     * @param {number} page 页码（从1开始）
     * @param {number} pageSize 每页条数
     * @param {string} search 搜索关键词
     * @returns {Promise<{success: boolean, data?: object, message?: string}>}
     */
    async getUserList(page = 1, pageSize = 20, search = '') {
        // TODO: 等待后端 API 实现
        const USE_MOCK = true;

        if (USE_MOCK) {
            // Mock 数据
            return new Promise((resolve) => {
                setTimeout(() => {
                    const mockUsers = [
                        {
                            id: 1,
                            username: 'admin',
                            nickname: '管理员',
                            role: 'admin',
                            status: 'active',
                            created_at: '2024-01-01T00:00:00Z',
                            last_login: '2024-01-18T10:30:00Z'
                        },
                        {
                            id: 2,
                            username: 'user',
                            nickname: '普通用户',
                            role: 'user',
                            status: 'active',
                            created_at: '2024-01-05T00:00:00Z',
                            last_login: '2024-01-18T09:15:00Z'
                        },
                        {
                            id: 3,
                            username: 'test_user',
                            nickname: '测试用户',
                            role: 'user',
                            status: 'disabled',
                            created_at: '2024-01-10T00:00:00Z',
                            last_login: '2024-01-15T14:20:00Z'
                        }
                    ];

                    // 模拟搜索
                    let filtered = mockUsers;
                    if (search) {
                        filtered = mockUsers.filter(u =>
                            u.username.includes(search) ||
                            u.nickname.includes(search)
                        );
                    }

                    resolve({
                        success: true,
                        data: {
                            users: filtered,
                            total: filtered.length,
                            page,
                            page_size: pageSize
                        }
                    });
                }, 300);
            });
        }

        // 真实 API
        try {
            const url = `/api/admin/users?page=${page}&page_size=${pageSize}&search=${encodeURIComponent(search)}`;
            const response = await Auth.fetchWithAuth(url);
            const data = await response.json();

            if (response.ok) {
                return { success: true, data };
            } else {
                return { success: false, message: data.error || '获取用户列表失败' };
            }
        } catch (error) {
            return { success: false, message: '网络错误：' + error.message };
        }
    },

    /**
     * 创建用户
     * @param {object} userData {username, password, nickname, role}
     */
    async createUser(userData) {
        // TODO: 等待后端 API 实现
        const USE_MOCK = true;

        if (USE_MOCK) {
            return new Promise((resolve) => {
                setTimeout(() => {
                    resolve({ success: true, message: '用户创建成功' });
                }, 500);
            });
        }

        try {
            const response = await Auth.fetchWithAuth('/api/admin/users', {
                method: 'POST',
                body: JSON.stringify(userData)
            });
            const data = await response.json();

            if (response.ok) {
                return { success: true, message: '用户创建成功', data };
            } else {
                return { success: false, message: data.error || '创建用户失败' };
            }
        } catch (error) {
            return { success: false, message: '网络错误：' + error.message };
        }
    },

    /**
     * 更新用户
     * @param {number} userId 用户ID
     * @param {object} userData 更新的数据
     */
    async updateUser(userId, userData) {
        // TODO: 等待后端 API 实现
        const USE_MOCK = true;

        if (USE_MOCK) {
            return new Promise((resolve) => {
                setTimeout(() => {
                    resolve({ success: true, message: '用户更新成功' });
                }, 500);
            });
        }

        try {
            const response = await Auth.fetchWithAuth(`/api/admin/users/${userId}`, {
                method: 'PUT',
                body: JSON.stringify(userData)
            });
            const data = await response.json();

            if (response.ok) {
                return { success: true, message: '用户更新成功' };
            } else {
                return { success: false, message: data.error || '更新用户失败' };
            }
        } catch (error) {
            return { success: false, message: '网络错误：' + error.message };
        }
    },

    /**
     * 删除用户
     * @param {number} userId 用户ID
     */
    async deleteUser(userId) {
        // TODO: 等待后端 API 实现
        const USE_MOCK = true;

        if (USE_MOCK) {
            return new Promise((resolve) => {
                setTimeout(() => {
                    resolve({ success: true, message: '用户删除成功' });
                }, 500);
            });
        }

        try {
            const response = await Auth.fetchWithAuth(`/api/admin/users/${userId}`, {
                method: 'DELETE'
            });
            const data = await response.json();

            if (response.ok) {
                return { success: true, message: '用户删除成功' };
            } else {
                return { success: false, message: data.error || '删除用户失败' };
            }
        } catch (error) {
            return { success: false, message: '网络错误：' + error.message };
        }
    },

    /**
     * 重置用户密码
     * @param {number} userId 用户ID
     * @param {string} newPassword 新密码
     */
    async resetPassword(userId, newPassword) {
        // TODO: 等待后端 API 实现
        const USE_MOCK = true;

        if (USE_MOCK) {
            return new Promise((resolve) => {
                setTimeout(() => {
                    resolve({ success: true, message: '密码重置成功' });
                }, 500);
            });
        }

        try {
            const response = await Auth.fetchWithAuth(`/api/admin/users/${userId}/reset-password`, {
                method: 'POST',
                body: JSON.stringify({ new_password: newPassword })
            });
            const data = await response.json();

            if (response.ok) {
                return { success: true, message: '密码重置成功' };
            } else {
                return { success: false, message: data.error || '重置密码失败' };
            }
        } catch (error) {
            return { success: false, message: '网络错误：' + error.message };
        }
    },

    /**
     * 获取统计数据
     */
    async getStats() {
        // TODO: 等待后端 API 实现
        const USE_MOCK = true;

        if (USE_MOCK) {
            return new Promise((resolve) => {
                setTimeout(() => {
                    resolve({
                        success: true,
                        data: {
                            total_users: 156,
                            active_users: 142,
                            total_chats: 3420,
                            total_feedbacks: 856,
                            today_chats: 124,
                            today_users: 45
                        }
                    });
                }, 300);
            });
        }

        try {
            const response = await Auth.fetchWithAuth('/api/admin/stats');
            const data = await response.json();

            if (response.ok) {
                return { success: true, data };
            } else {
                return { success: false, message: data.error || '获取统计数据失败' };
            }
        } catch (error) {
            return { success: false, message: '网络错误：' + error.message };
        }
    },

    /**
     * 获取问答历史
     * @param {number} page 页码
     * @param {number} pageSize 每页条数
     * @param {object} filters 过滤条件 {user_id, start_date, end_date}
     */
    async getChatHistory(page = 1, pageSize = 20, filters = {}) {
        // TODO: 等待后端 API 实现
        const USE_MOCK = true;

        if (USE_MOCK) {
            return new Promise((resolve) => {
                setTimeout(() => {
                    const mockHistory = [
                        {
                            id: 1,
                            user_id: 2,
                            username: 'user',
                            question: '智慧法庭V4.0有哪些新功能？',
                            answer: '智慧法庭V4.0主要新增了以下功能：1. 笔录精简...',
                            created_at: '2024-01-18T10:30:00Z',
                            feedback_type: 'like'
                        },
                        {
                            id: 2,
                            user_id: 2,
                            username: 'user',
                            question: '如何配置笔录精简？',
                            answer: '笔录精简功能需要在配置文件中...',
                            created_at: '2024-01-18T10:25:00Z',
                            feedback_type: null
                        },
                        {
                            id: 3,
                            user_id: 3,
                            username: 'test_user',
                            question: '部署环境要求',
                            answer: '部署环境需要满足以下条件...',
                            created_at: '2024-01-18T09:15:00Z',
                            feedback_type: 'dislike'
                        }
                    ];

                    resolve({
                        success: true,
                        data: {
                            records: mockHistory,
                            total: mockHistory.length,
                            page,
                            page_size: pageSize
                        }
                    });
                }, 300);
            });
        }

        try {
            const params = new URLSearchParams({
                page: page.toString(),
                page_size: pageSize.toString(),
                ...filters
            });
            const response = await Auth.fetchWithAuth(`/api/admin/chat-history?${params}`);
            const data = await response.json();

            if (response.ok) {
                return { success: true, data };
            } else {
                return { success: false, message: data.error || '获取问答历史失败' };
            }
        } catch (error) {
            return { success: false, message: '网络错误：' + error.message };
        }
    },

    /**
     * 获取反馈统计
     * @param {object} filters 过滤条件 {start_date, end_date}
     */
    async getFeedbackStats(filters = {}) {
        // TODO: 等待后端 API 实现
        const USE_MOCK = true;

        if (USE_MOCK) {
            return new Promise((resolve) => {
                setTimeout(() => {
                    resolve({
                        success: true,
                        data: {
                            total: 856,
                            like: 720,
                            dislike: 136,
                            like_rate: 0.84,
                            recent_feedbacks: [
                                {
                                    id: 1,
                                    user_id: 2,
                                    username: 'user',
                                    feedback_type: 'dislike',
                                    feedback_text: '回答不够详细',
                                    question: '智慧法庭V4.0有哪些新功能？',
                                    created_at: '2024-01-18T10:30:00Z'
                                }
                            ]
                        }
                    });
                }, 300);
            });
        }

        try {
            const params = new URLSearchParams(filters);
            const response = await Auth.fetchWithAuth(`/api/admin/feedback-stats?${params}`);
            const data = await response.json();

            if (response.ok) {
                return { success: true, data };
            } else {
                return { success: false, message: data.error || '获取反馈统计失败' };
            }
        } catch (error) {
            return { success: false, message: '网络错误：' + error.message };
        }
    },

    /**
     * 导出数据
     * @param {string} type 导出类型 'users' | 'chat_history' | 'feedbacks'
     * @param {object} filters 过滤条件
     */
    async exportData(type, filters = {}) {
        // TODO: 等待后端 API 实现
        const USE_MOCK = true;

        if (USE_MOCK) {
            showToast('导出功能开发中（Mock）', 'info');
            return { success: false, message: '导出功能开发中' };
        }

        try {
            const params = new URLSearchParams(filters);
            const response = await Auth.fetchWithAuth(`/api/admin/export/${type}?${params}`);

            if (response.ok) {
                // 下载文件
                const blob = await response.blob();
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `${type}_${new Date().getTime()}.csv`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                window.URL.revokeObjectURL(url);

                return { success: true, message: '导出成功' };
            } else {
                const data = await response.json();
                return { success: false, message: data.error || '导出失败' };
            }
        } catch (error) {
            return { success: false, message: '网络错误：' + error.message };
        }
    }
};

/**
 * 格式化日期时间
 */
function formatDateTime(dateStr) {
    if (!dateStr) return '-';
    const date = new Date(dateStr);
    return date.toLocaleString('zh-CN', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit'
    });
}

/**
 * 格式化日期
 */
function formatDate(dateStr) {
    if (!dateStr) return '-';
    const date = new Date(dateStr);
    return date.toLocaleDateString('zh-CN');
}

/**
 * 截断文本
 */
function truncateText(text, maxLength = 50) {
    if (!text) return '-';
    if (text.length <= maxLength) return text;
    return text.substring(0, maxLength) + '...';
}
