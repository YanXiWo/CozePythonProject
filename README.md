# AI 聊天机器人服务

一个基于 FastAPI 和 WebSocket 的实时 AI 聊天机器人服务，使用 Coze API 提供智能对话能力。

<<<<<<< HEAD
=======
## 演示

### 聊天界面
![聊天界面演示](images/演示%201.png)

### 多机器人切换
![多机器人切换演示](images/演示%202.png)

### 性能监控
![性能监控界面](images/演示%203.png)

>>>>>>> c0f3d9d (Initial commit - add main.py and PyCharm config)
## 功能特点

- 🤖 多机器人支持：可在不同专业领域的 AI 助手之间切换
- 💬 实时对话：基于 WebSocket 的流式响应，提供打字机效果
- 📱 响应式设计：完美适配桌面和移动设备
- 🔄 自动重连：网络中断时自动尝试重新连接
- 📊 性能监控：内置统计接口，监控系统运行状态
- 🗄️ 响应缓存：缓存常见问题的回答，提高响应速度
- 📝 Markdown 支持：AI 回复支持 Markdown 格式，包括代码高亮
- 🔄 负载均衡：多 API 令牌轮询，提高并发处理能力
- 📋 会话管理：自动清理长时间不活跃的会话
- 📊 用户输入日志：记录用户查询，便于分析和改进

## 技术栈

- **后端**：FastAPI, WebSocket, asyncio
- **AI 接口**：Coze API
- **前端**：HTML, CSS, JavaScript (内嵌于后端)
- **并发处理**：asyncio, 信号量控制
- **缓存系统**：自定义内存缓存

## 安装与设置

### 前提条件

- Python 3.8+
- Coze API 访问令牌

### 安装步骤

1. 克隆仓库：

```bash
git clone <repository-url>
cd <repository-directory>
```

2. 安装依赖：

```bash
pip install -r requirements.txt
```

3. 配置 Coze API 令牌：

在 `main.py` 中更新 `coze_api_tokens` 字典，替换占位符为您的实际 API 令牌：

```python
coze_api_tokens = {
    'token1': '您的第一个API令牌',
    'token2a': '您的第二个API令牌',
    'token2b': '您的第三个API令牌'
}
```

4. 配置机器人：

根据需要在 `bot_configs` 列表中更新机器人配置：

```python
bot_configs = [
    {
        'id': '您的机器人ID',
        'name': '机器人名称',
        'token_key': 'token1',  # 使用哪个API令牌
        'icon': 'fa-robot',  # Font Awesome图标
        'color': '#3f6ad8',
        'gradient': 'linear-gradient(135deg, #3f6ad8, #5d87f5)'
    },
    # 更多机器人配置...
]
```

## 运行服务

### 开发模式

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 521
```

### 生产模式

```bash
uvicorn main:app --host 0.0.0.0 --port 521 --workers 4
```

您可以根据服务器 CPU 核心数调整 workers 数量。

## 使用方法

1. 启动服务后，访问 `http://localhost:521` 或您的服务器地址
2. 在聊天界面输入问题并发送
3. 使用右下角的切换按钮可以在不同的 AI 助手之间切换

## 项目结构

```
.
├── main.py          # 主应用程序文件
└── logs/            # 用户输入日志目录
```

## 配置说明

### 日志配置

日志文件按日期保存在 `logs` 目录中，格式为 `user_chat_YYYYMMDD.log`。

### 缓存配置

可以在 `main.py` 中调整缓存大小和过期时间：

```python
app.state.response_cache = ResponseCache(max_size=5000, ttl=7200)  # 缓存2小时
```

### 性能监控

访问 `/stats` 端点可以查看服务运行状态，包括：
- 活跃连接数
- 总连接数
- 处理的消息数
- 错误数
- 运行时间
- 并发请求数
- 缓存命中率
- 等其他性能指标

## 贡献指南

1. Fork 项目
2. 创建您的特性分支 (`git checkout -b feature/amazing-feature`)
3. 提交您的更改 (`git commit -m 'Add some amazing feature'`)
4. 推送到分支 (`git push origin feature/amazing-feature`)
5. 打开一个 Pull Request

## 许可证

[MIT](LICENSE)
