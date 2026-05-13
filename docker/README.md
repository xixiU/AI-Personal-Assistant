# Docker 部署指南

## 快速开始

```bash
# 1. 进入 docker 目录
cd docker

# 2. 创建配置文件（在项目根目录）
cd ..
cp config.example.yaml config.yaml
# 编辑 config.yaml

# 3. 返回 docker 目录并构建
cd docker

# GPU 版本
docker-compose build ai-assistant-gpu
docker-compose up -d ai-assistant-gpu

# 或 CPU 版本
docker-compose build ai-assistant-cpu
docker-compose up -d ai-assistant-cpu

# 4. 查看日志
docker-compose logs -f
```

## 镜像说明

### GPU 版本
- 基础镜像：`nvidia/cuda:11.8.0-runtime-ubuntu22.04`
- Python：3.12
- 依赖：onnxruntime-gpu < 1.18.0
- 需要：nvidia-docker

### CPU 版本
- 基础镜像：`python:3.12-slim`
- 依赖：onnxruntime < 1.18.0
- 更小更快

## 配置说明

### 镜像源
- Ubuntu APT：清华源
- Python PyPI：清华源

### 挂载目录
- `config.yaml`：配置文件（只读）
- `logs/`：日志目录
- `data/`：数据目录（缓存、向量数据库）
- `models/`：模型目录（可选）

### 端口
- `11111`：Webhook 服务端口

## 常用命令

```bash
# 启动
docker-compose up -d ai-assistant-gpu

# 停止
docker-compose down

# 重启
docker-compose restart

# 查看日志
docker-compose logs -f

# 查看状态
docker-compose ps

# 进入容器
docker-compose exec ai-assistant-gpu bash

# 重新构建
docker-compose build --no-cache
```

## GPU 支持

### 安装 nvidia-docker

```bash
# Ubuntu/Debian
distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
curl -s -L https://nvidia.github.io/nvidia-docker/gpgkey | sudo apt-key add -
curl -s -L https://nvidia.github.io/nvidia-docker/$distribution/nvidia-docker.list | sudo tee /etc/apt/sources.list.d/nvidia-docker.list

sudo apt-get update
sudo apt-get install -y nvidia-docker2
sudo systemctl restart docker
```

### 验证 GPU 可用

```bash
docker run --rm --gpus all nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi
```

## 故障排查

### 1. 构建失败

```bash
# 清理缓存重新构建
docker-compose build --no-cache
```

### 2. GPU 不可用

```bash
# 检查 nvidia-docker
docker run --rm --gpus all nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi

# 检查容器内 GPU
docker-compose exec ai-assistant-gpu nvidia-smi
```

### 3. 配置文件找不到

确保 `config.yaml` 在项目根目录（docker 目录的上一级）

### 4. 端口被占用

修改 `docker-compose.yml` 中的端口映射：
```yaml
ports:
  - "8080:11111"  # 宿主机:容器
```
