/**
 * 认证逻辑模块
 * 负责 Token 管理、用户信息存储、权限验证
 */

const AUTH_CONFIG = {
    TOKEN_KEY: 'ai_auth_token',
    USER_KEY: 'ai_user_info',
    API_BASE: '/api'  // API 基础路径
};

/**
 * 认证管理器
 */
const Auth = {
    /**
     * 保存 Token 到 localStorage
     */
    setToken(token) {
        localStorage.setItem(AUTH_CONFIG.TOKEN_KEY, token);
    },

    /**
     * 获取 Token
     */
    getToken() {
        return localStorage.getItem(AUTH_CONFIG.TOKEN_KEY);
    },

    /**
     * 移除 Token
     */
    removeToken() {
        localStorage.removeItem(AUTH_CONFIG.TOKEN_KEY);
    },

    /**
     * 保存用户信息
     */
    setUser(user) {
        localStorage.setItem(AUTH_CONFIG.USER_KEY, JSON.stringify(user));
    },

    /**
     * 获取用户信息
     */
    getUser() {
        const userStr = localStorage.getItem(AUTH_CONFIG.USER_KEY);
        return userStr ? JSON.parse(userStr) : null;
    },

    /**
     * 移除用户信息
     */
    removeUser() {
        localStorage.removeItem(AUTH_CONFIG.USER_KEY);
    },

    /**
     * 检查是否已登录
     */
    isLoggedIn() {
        return !!this.getToken();
    },

    /**
     * 检查是否是管理员
     */
    isAdmin() {
        const user = this.getUser();
        return user && user.role === 'admin';
    },

    /**
     * 登出
     */
    logout() {
        this.removeToken();
        this.removeUser();
        window.location.href = '/static/login.html';
    },

    /**
     * 登录 API
     * @param {string} username 用户名
     * @param {string} password 密码
     * @returns {Promise<{success: boolean, message: string, data?: object}>}
     */
    async login(username, password) {
        // TODO: 等待后端 API 实现，当前使用 mock 数据
        const USE_MOCK = true;

        if (USE_MOCK) {
            // Mock 数据：模拟登录
            return new Promise((resolve) => {
                setTimeout(() => {
                    if (username === 'admin' && password === 'admin123') {
                        const mockData = {
                            token: 'mock-token-' + Date.now(),
                            user: {
                                id: 1,
                                username: 'admin',
                                nickname: '管理员',
                                role: 'admin',
                                created_at: '2024-01-01T00:00:00Z'
                            }
                        };
                        resolve({ success: true, message: '登录成功', data: mockData });
                    } else if (username === 'user' && password === 'user123') {
                        const mockData = {
                            token: 'mock-token-' + Date.now(),
                            user: {
                                id: 2,
                                username: 'user',
                                nickname: '普通用户',
                                role: 'user',
                                created_at: '2024-01-01T00:00:00Z'
                            }
                        };
                        resolve({ success: true, message: '登录成功', data: mockData });
                    } else {
                        resolve({ success: false, message: '用户名或密码错误' });
                    }
                }, 500);
            });
        }

        // 真实 API 调用（等待后端实现）
        try {
            const response = await fetch(`${AUTH_CONFIG.API_BASE}/auth/login`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ username, password })
            });

            const data = await response.json();

            if (response.ok) {
                return { success: true, message: '登录成功', data };
            } else {
                return { success: false, message: data.error || '登录失败' };
            }
        } catch (error) {
            return { success: false, message: '网络错误：' + error.message };
        }
    },

    /**
     * 修改密码 API
     * @param {string} oldPassword 旧密码
     * @param {string} newPassword 新密码
     * @returns {Promise<{success: boolean, message: string}>}
     */
    async changePassword(oldPassword, newPassword) {
        // TODO: 等待后端 API 实现
        const USE_MOCK = true;

        if (USE_MOCK) {
            return new Promise((resolve) => {
                setTimeout(() => {
                    if (oldPassword === 'admin123' || oldPassword === 'user123') {
                        resolve({ success: true, message: '密码修改成功，请重新登录' });
                    } else {
                        resolve({ success: false, message: '旧密码错误' });
                    }
                }, 500);
            });
        }

        try {
            const response = await fetch(`${AUTH_CONFIG.API_BASE}/auth/change-password`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Authorization': 'Bearer ' + this.getToken()
                },
                body: JSON.stringify({ old_password: oldPassword, new_password: newPassword })
            });

            const data = await response.json();

            if (response.ok) {
                return { success: true, message: data.message || '密码修改成功' };
            } else {
                return { success: false, message: data.error || '密码修改失败' };
            }
        } catch (error) {
            return { success: false, message: '网络错误：' + error.message };
        }
    },

    /**
     * 获取用户信息 API（用于刷新用户数据）
     * @returns {Promise<{success: boolean, message: string, data?: object}>}
     */
    async getUserInfo() {
        // TODO: 等待后端 API 实现
        const USE_MOCK = true;

        if (USE_MOCK) {
            const user = this.getUser();
            return Promise.resolve({ success: true, data: user });
        }

        try {
            const response = await fetch(`${AUTH_CONFIG.API_BASE}/auth/me`, {
                method: 'GET',
                headers: {
                    'Authorization': 'Bearer ' + this.getToken()
                }
            });

            const data = await response.json();

            if (response.ok) {
                return { success: true, data };
            } else {
                return { success: false, message: data.error || '获取用户信息失败' };
            }
        } catch (error) {
            return { success: false, message: '网络错误：' + error.message };
        }
    },

    /**
     * 带 Token 的 fetch 封装
     */
    async fetchWithAuth(url, options = {}) {
        const token = this.getToken();
        if (!token) {
            throw new Error('未登录');
        }

        const headers = {
            'Content-Type': 'application/json',
            ...options.headers,
            'Authorization': 'Bearer ' + token
        };

        const response = await fetch(url, { ...options, headers });

        // Token 过期或无效，跳转登录
        if (response.status === 401) {
            this.logout();
            throw new Error('登录已过期，请重新登录');
        }

        return response;
    }
};

/**
 * 页面加载时的权限检查
 * 用法：在需要登录的页面调用 requireLogin()
 *      在需要管理员权限的页面调用 requireAdmin()
 */
function requireLogin() {
    if (!Auth.isLoggedIn()) {
        window.location.href = '/static/login.html';
        return false;
    }
    return true;
}

function requireAdmin() {
    if (!Auth.isLoggedIn()) {
        window.location.href = '/static/login.html';
        return false;
    }
    if (!Auth.isAdmin()) {
        alert('需要管理员权限');
        window.location.href = '/static/user-profile.html';
        return false;
    }
    return true;
}

/**
 * 显示 Toast 提示
 */
function showToast(message, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    document.body.appendChild(toast);

    setTimeout(() => toast.classList.add('show'), 10);
    setTimeout(() => {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}
