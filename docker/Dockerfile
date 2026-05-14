# 多阶段构建：最小化镜像大小
# Stage 1: 构建阶段
FROM dcb91c7cc18305198be1f6271ebdc596.d.1ms.run/nvidia/cuda:11.8.0-runtime-ubuntu22.04 AS builder

# 设置清华镜像源
RUN sed -i 's@//.*archive.ubuntu.com@//mirrors.tuna.tsinghua.edu.cn@g' /etc/apt/sources.list && \
    sed -i 's@//.*security.ubuntu.com@//mirrors.tuna.tsinghua.edu.cn@g' /etc/apt/sources.list

# 安装 Python 3.10（Ubuntu 22.04 自带）和构建工具
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-venv \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# 配置 pip 使用清华源
RUN mkdir -p /root/.pip && \
    echo "[global]\n\
index-url = https://pypi.tuna.tsinghua.edu.cn/simple\n\
trusted-host = pypi.tuna.tsinghua.edu.cn" > /root/.pip/pip.conf

# 创建虚拟环境
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 升级 pip
RUN pip install --no-cache-dir --upgrade pip

# 复制依赖文件并安装（GPU 版本）
COPY requirements.txt requirements-gpu.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements-gpu.txt

# Stage 2: 运行阶段
FROM dcb91c7cc18305198be1f6271ebdc596.d.1ms.run/nvidia/cuda:11.8.0-runtime-ubuntu22.04

# 设置清华镜像源
RUN sed -i 's@//.*archive.ubuntu.com@//mirrors.tuna.tsinghua.edu.cn@g' /etc/apt/sources.list && \
    sed -i 's@//.*security.ubuntu.com@//mirrors.tuna.tsinghua.edu.cn@g' /etc/apt/sources.list

# 只安装运行时需要的 Python
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    && rm -rf /var/lib/apt/lists/*

# 从构建阶段复制虚拟环境
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 设置工作目录
WORKDIR /app

# 复制代码
COPY src /app/src
COPY run.py /app/

# 创建必要的目录
RUN mkdir -p /app/logs /app/data

# 暴露端口
EXPOSE 11111

# 设置环境变量
ENV PYTHONUNBUFFERED=1
ENV CUDA_VISIBLE_DEVICES=0

# 启动命令
CMD ["python3", "run.py"]
