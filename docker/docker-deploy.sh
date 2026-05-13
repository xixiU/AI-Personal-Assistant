#!/bin/bash
# Docker 部署脚本

set -e

echo "=========================================="
echo "AI 自动回复助手 - Docker 部署"
echo "=========================================="
echo ""

# 检查 Docker
if ! command -v docker &> /dev/null; then
    echo "❌ Docker 未安装"
    exit 1
fi

# 检查配置文件
if [ ! -f "config.yaml" ]; then
    echo "❌ config.yaml 不存在，请先创建配置文件"
    echo "   cp config.example.yaml config.yaml"
    exit 1
fi

# 选择版本
echo "选择版本："
echo "  1) GPU 版本（需要 NVIDIA GPU + nvidia-docker）"
echo "  2) CPU 版本"
read -p "请选择 [1/2]: " choice

case $choice in
    1)
        echo ""
        echo "🚀 构建 GPU 版本..."

        # 检查 nvidia-docker
        if ! docker run --rm --gpus all nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi &> /dev/null; then
            echo "❌ nvidia-docker 不可用，请先安装"
            echo "   参考: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
            exit 1
        fi

        docker-compose build ai-assistant-gpu
        echo ""
        echo "✅ 构建完成"
        echo ""
        echo "启动服务："
        echo "  docker-compose up -d ai-assistant-gpu"
        echo ""
        echo "查看日志："
        echo "  docker-compose logs -f ai-assistant-gpu"
        ;;
    2)
        echo ""
        echo "🚀 构建 CPU 版本..."
        docker-compose build ai-assistant-cpu
        echo ""
        echo "✅ 构建完成"
        echo ""
        echo "启动服务："
        echo "  docker-compose up -d ai-assistant-cpu"
        echo ""
        echo "查看日志："
        echo "  docker-compose logs -f ai-assistant-cpu"
        ;;
    *)
        echo "❌ 无效选择"
        exit 1
        ;;
esac

echo ""
echo "其他命令："
echo "  停止服务: docker-compose down"
echo "  重启服务: docker-compose restart"
echo "  查看状态: docker-compose ps"
