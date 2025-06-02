from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
import uvicorn
from cozepy import COZE_CN_BASE_URL, Coze, TokenAuth, Message, ChatEventType
from collections import defaultdict
import uuid
import json
import time
import asyncio
import logging
from typing import Dict, List, Set, Any, Optional
import threading
from contextlib import asynccontextmanager
import os
from concurrent.futures import ThreadPoolExecutor
from aioitertools import iter
from datetime import datetime
import socket
import queue
import asyncio.queues

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("aibot")

# 创建用户输入日志记录器
user_logger = logging.getLogger("user_input")
user_logger.setLevel(logging.INFO)
# 创建文件处理器，确保日志文件按日期命名
os.makedirs("logs", exist_ok=True)
user_log_handler = logging.FileHandler(f"logs/user_chat_{datetime.now().strftime('%Y%m%d')}.log", encoding="utf-8")
user_log_formatter = logging.Formatter('%(asctime)s - %(message)s')
user_log_handler.setFormatter(user_log_formatter)
user_logger.addHandler(user_log_handler)
# 确保用户输入日志不会传播到根日志记录器
user_logger.propagate = False

# 异步日志队列，避免日志操作阻塞主流程
log_queue = asyncio.queues.Queue(maxsize=10000)


# 处理日志队列的异步任务
async def process_log_queue():
    while True:
        try:
            log_data = await log_queue.get()
            user_logger.info(log_data)
            log_queue.task_done()
        except Exception as e:
            logger.error(f"处理日志队列时出错: {str(e)}")
            await asyncio.sleep(1)  # 出错时暂停一下，避免死循环消耗资源


# 记录用户输入的函数
async def log_user_message(ip: str, bot_name: str, message: str):
    """异步方式记录用户输入到专用日志文件"""
    try:
        log_data = f"IP: {ip} | 机器人: {bot_name} | 消息: {message}"
        await log_queue.put(log_data)
    except Exception as e:
        logger.error(f"添加日志到队列失败: {str(e)}")


# 轮询分配API令牌的选择器
class TokenSelector:
    def __init__(self, tokens):
        self.tokens = list(tokens.keys())
        self.index = 0
        self.lock = threading.Lock()

    def next_token(self):
        with self.lock:
            token = self.tokens[self.index]
            self.index = (self.index + 1) % len(self.tokens)
            return token


# API请求信号量 - 使用信号量代替锁提高并发
API_SEMAPHORES = {
    'token1': asyncio.Semaphore(20),  # 允许并发20个请求
    'token2a': asyncio.Semaphore(20),
    'token2b': asyncio.Semaphore(20)
}

# 创建令牌选择器

# 性能统计数据
STATS = {
    "active_connections": 0,
    "total_connections": 0,
    "messages_processed": 0,
    "errors": 0,
    "start_time": time.time(),
    "concurrent_requests": 0,
    "max_concurrent_requests": 0,
    "response_times": [],  # 响应时间列表，用于计算平均值
    "cache_hits": 0,  # 缓存命中次数
    "api_calls": 0  # API调用次数
}


# 连接管理器
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.lock = asyncio.Lock()

    async def connect(self, user_id: str, websocket: WebSocket):
        await websocket.accept()
        async with self.lock:
            self.active_connections[user_id] = websocket
            STATS["active_connections"] += 1
            STATS["total_connections"] += 1

    async def disconnect(self, user_id: str):
        async with self.lock:
            if user_id in self.active_connections:
                del self.active_connections[user_id]
                STATS["active_connections"] -= 1

    async def send_message(self, user_id: str, message: str):
        if user_id in self.active_connections:
            await self.active_connections[user_id].send_text(message)

    def get_active_connections_count(self):
        return len(self.active_connections)


# 缓存管理
class ResponseCache:
    def __init__(self, max_size=1000, ttl=3600):  # 默认缓存1小时
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.max_size = max_size
        self.ttl = ttl
        self.lock = threading.Lock()

    def set(self, key: str, value: str):
        with self.lock:
            # 如果缓存已满，删除最早的条目
            if len(self.cache) >= self.max_size:
                oldest_key = min(self.cache.keys(), key=lambda k: self.cache[k]["timestamp"])
                del self.cache[oldest_key]

            self.cache[key] = {
                "value": value,
                "timestamp": time.time()
            }

    def get(self, key: str) -> str:
        with self.lock:
            if key not in self.cache:
                return None

            entry = self.cache[key]
            # 检查是否过期
            if time.time() - entry["timestamp"] > self.ttl:
                del self.cache[key]
                return None

            return entry["value"]

    def cleanup(self):
        """删除过期缓存"""
        with self.lock:
            current_time = time.time()
            keys_to_delete = [
                k for k, v in self.cache.items()
                if current_time - v["timestamp"] > self.ttl
            ]
            for key in keys_to_delete:
                del self.cache[key]


# 启动和关闭事件处理
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时执行
    logger.info("服务器启动中...")

    # 创建定时任务
    app.state.cache_cleanup_task = asyncio.create_task(periodic_cache_cleanup(app))

    # 创建会话清理任务
    app.state.session_cleanup_task = asyncio.create_task(cleanup_inactive_sessions())

    # 创建日志处理任务
    app.state.log_queue_task = asyncio.create_task(process_log_queue())

    # 创建线程池 - 增大线程池容量以处理更多并发请求
    app.state.executor = ThreadPoolExecutor(max_workers=50)

    yield

    # 关闭时执行
    logger.info("服务器关闭中...")
    app.state.cache_cleanup_task.cancel()
    app.state.session_cleanup_task.cancel()
    app.state.log_queue_task.cancel()
    app.state.executor.shutdown(wait=False)
    try:
        await app.state.cache_cleanup_task
        await app.state.session_cleanup_task
        await app.state.log_queue_task
    except asyncio.CancelledError:
        pass


# 定期清理缓存的异步任务
async def periodic_cache_cleanup(app):
    while True:
        try:
            await asyncio.sleep(300)  # 每5分钟清理一次
            app.state.response_cache.cleanup()
            logger.info(f"缓存清理完成，当前缓存条目数: {len(app.state.response_cache.cache)}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"缓存清理出错: {str(e)}")


# 定期清理长时间不活跃的会话
async def cleanup_inactive_sessions():
    while True:
        try:
            await asyncio.sleep(3600)  # 每小时执行一次
            current_time = time.time()
            inactive_threshold = 7200  # 2小时不活跃视为过期

            inactive_users = []
            for user_id, session in user_sessions.items():
                if current_time - session["last_activity"] > inactive_threshold:
                    inactive_users.append(user_id)

            for user_id in inactive_users:
                del user_sessions[user_id]

            if inactive_users:
                logger.info(f"清理了 {len(inactive_users)} 个不活跃会话")

        except Exception as e:
            logger.error(f"清理会话时出错: {str(e)}")


# 创建FastAPI应用
app = FastAPI(lifespan=lifespan)

# Coze API 配置
coze_api_tokens = {
    'token1': 'Coze API',
    'token2a': 'Coze API',
    'token2b': 'Coze API'
}

# 创建多个Coze客户端
coze_clients = {}
for key, token in coze_api_tokens.items():
    coze_clients[key] = Coze(auth=TokenAuth(token=token), base_url=COZE_CN_BASE_URL)

# 创建令牌选择器
token_selector = TokenSelector(coze_api_tokens)

# 机器人配置
bot_configs = [
    {
        'id': '7436265511035518985',
        'name': '智能全能顾问',
        'token_key': 'token1',
        'icon': 'fa-robot',
        'color': '#3f6ad8',
        'gradient': 'linear-gradient(135deg, #3f6ad8, #5d87f5)'
    },
    {
        'id': '7472199166358831140',
        'name': 'AI建议顾问',
        'token_key': 'token2a',  # 使用独立的客户端实例
        'icon': 'fa-lightbulb',
        'color': '#ff9800',
        'gradient': 'linear-gradient(135deg, #ff9800, #ffb74d)'
    },
    {
        'id': '7473373856972554292',
        'name': 'AI产品顾问',
        'token_key': 'token2b',  # 使用另一个独立的客户端实例
        'icon': 'fa-shopping-cart',
        'color': '#4caf50',
        'gradient': 'linear-gradient(135deg, #4caf50, #81c784)'
    }
]

# 默认机器人
default_bot = bot_configs[0]

# 初始化连接管理器和缓存
manager = ConnectionManager()
app.state.response_cache = ResponseCache(max_size=5000, ttl=7200)  # 缓存2小时

# 用户会话存储
user_sessions = defaultdict(lambda: {
    "conversation_id": None,
    "history": [],
    "last_activity": time.time(),
    "current_bot": default_bot
})

html = """
<!DOCTYPE html>
<html lang="zh">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
  <title>智能全能顾问 | 在线咨询</title>
  <link rel="stylesheet" href="https://cdn.bootcdn.net/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <link rel="stylesheet" href="https://hxkecheng.oss-cn-beijing.aliyuncs.com/cozebot/bot_XHEnT/github-markdown-light.css">
  <link rel="stylesheet" href="https://hxkecheng.oss-cn-beijing.aliyuncs.com/cozebot/bot_XHEnT/github.min.css">
  <style>
    /* 现代化设计变量 */
    :root {
      --primary-color: #3f6ad8;
      --primary-gradient: linear-gradient(135deg, #3f6ad8, #5d87f5);
      --primary-dark: #2a4fbf;
      --primary-light: #e5edff;
      --success-color: #2ed47a;
      --danger-color: #f64e60;
      --warning-color: #ffa800;
      --neutral-bg: #f7f9fc;
      --text-color: #272b41;
      --text-light: #696a7e;
      --text-muted: #9ea0aa;
      --border-radius-sm: 8px;
      --border-radius: 16px;
      --border-radius-lg: 24px;
      --shadow-sm: 0 2px 10px rgba(0, 0, 0, 0.03);
      --shadow: 0 5px 20px rgba(0, 0, 0, 0.08);
      --shadow-lg: 0 10px 30px rgba(62, 65, 159, 0.1);
      --transition: all 0.25s cubic-bezier(0.645, 0.045, 0.355, 1);
      --font-primary: 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;

      /* 机器人颜色主题 */
      --bot1-color: #3f6ad8;
      --bot1-gradient: linear-gradient(135deg, #3f6ad8, #5d87f5);
      --bot2-color: #ff9800;
      --bot2-gradient: linear-gradient(135deg, #ff9800, #ffb74d);
      --bot3-color: #4caf50;
      --bot3-gradient: linear-gradient(135deg, #4caf50, #81c784);
    }

    /* 重置与基础样式 */
    * {
      margin: 0;
      padding: 0;
      box-sizing: border-box;
    }

    html {
      font-size: 16px;
      scroll-behavior: smooth;
    }

    body {
      font-family: var(--font-primary);
      line-height: 1.6;
      color: var(--text-color);
      background: var(--neutral-bg);
      min-height: 100vh;
      display: flex;
      justify-content: center;
      align-items: center;
      padding: 16px;
      position: relative;
      overflow-x: hidden;
    }

    /* 漂亮的背景装饰 */
    body::before {
      content: '';
      position: fixed;
      top: 0;
      right: 0;
      width: 100%;
      height: 100%;
      background-image: 
        radial-gradient(circle at 20% 35%, rgba(63, 106, 216, 0.15) 0%, transparent 50%),
        radial-gradient(circle at 75% 75%, rgba(46, 212, 122, 0.1) 0%, transparent 50%);
      z-index: -1;
    }

    /* 装饰元素 */
    .decoration {
      position: fixed;
      border-radius: 50%;
      filter: blur(60px);
      opacity: 0.15;
      z-index: -1;
    }

    .decoration-1 {
      width: 600px;
      height: 600px;
      background: #3f6ad8;
      top: -300px;
      right: -100px;
    }

    .decoration-2 {
      width: 400px;
      height: 400px;
      background: #2ed47a;
      bottom: -200px;
      left: -100px;
    }

    /* 聊天容器样式优化 */
    .chat-container {
      width: 100%;
      max-width: 900px;
      background: #fff;
      border-radius: var(--border-radius);
      box-shadow: var(--shadow-lg);
      display: flex;
      flex-direction: column;
      height: 85vh;
      max-height: 800px;
      overflow: hidden;
      position: relative;
      transition: var(--transition);
      backdrop-filter: blur(5px);
      -webkit-backdrop-filter: blur(5px);
      border: 1px solid rgba(255, 255, 255, 0.2);
    }

    /* 顶部标题栏设计 */
    .chat-header {
      background: var(--primary-gradient);
      color: #fff;
      padding: 18px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      position: relative;
      border-radius: var(--border-radius) var(--border-radius) 0 0;
      box-shadow: 0 4px 12px rgba(63, 106, 216, 0.15);
      z-index: 10;
    }

    .chat-header-title {
      display: flex;
      align-items: center;
      font-weight: 600;
      font-size: 1.25rem;
    }

    .chat-header-title i {
      margin-right: 12px;
      font-size: 1.4rem;
      background: rgba(255, 255, 255, 0.2);
      width: 36px;
      height: 36px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
    }

    /* 标题徽章 */
    .chat-header .badge {
      font-size: 0.7rem;
      background: rgba(255, 255, 255, 0.15);
      padding: 3px 8px;
      border-radius: 20px;
      margin-left: 8px;
      font-weight: normal;
    }

    /* 状态指示器 */
    .status-indicator {
      display: flex;
      align-items: center;
      font-size: 0.85rem;
      font-weight: 500;
      background: rgba(255, 255, 255, 0.2);
      padding: 6px 12px;
      border-radius: 30px;
    }

    .status-dot {
      display: inline-block;
      width: 8px;
      height: 8px;
      background: #4caf50;
      border-radius: 50%;
      margin-right: 8px;
      position: relative;
    }

    .status-dot::after {
      content: '';
      position: absolute;
      width: 100%;
      height: 100%;
      background: inherit;
      border-radius: inherit;
      opacity: 0.5;
      animation: pulse 1.5s ease-out infinite;
    }

    @keyframes pulse {
      0% { transform: scale(1); opacity: 0.5; }
      70% { transform: scale(2.5); opacity: 0; }
      100% { transform: scale(1); opacity: 0; }
    }

    /* 消息区域优化 */
    .chat-messages {
      flex: 1;
      padding: 24px;
      overflow-y: auto;
      background: #fff;
      background-image: 
        radial-gradient(rgba(63, 106, 216, 0.05) 2px, transparent 2px),
        radial-gradient(rgba(63, 106, 216, 0.05) 2px, transparent 2px);
      background-size: 40px 40px;
      background-position: 0 0, 20px 20px;
      scroll-behavior: smooth;
    }

    /* 美化滚动条 */
    .chat-messages::-webkit-scrollbar {
      width: 8px;
    }

    .chat-messages::-webkit-scrollbar-track {
      background: rgba(247, 249, 252, 0.8);
      border-radius: 10px;
    }

    .chat-messages::-webkit-scrollbar-thumb {
      background: rgba(63, 106, 216, 0.2);
      border-radius: 10px;
      border: 3px solid transparent;
    }

    .chat-messages::-webkit-scrollbar-thumb:hover {
      background: rgba(63, 106, 216, 0.3);
    }

    /* 消息样式 */
    .message {
      margin-bottom: 20px;
      display: flex;
      align-items: flex-end;
      animation: fadeIn 0.3s ease;
      position: relative;
      max-width: 85%;
    }

    @keyframes fadeIn {
      from { opacity: 0; transform: translateY(10px); }
      to { opacity: 1; transform: translateY(0); }
    }

    .message.user {
      justify-content: flex-end;
      margin-left: auto;
    }

    .message-content {
      display: flex;
      align-items: flex-end;
    }

    .message-avatar {
      width: 40px;
      height: 40px;
      border-radius: 50%;
      display: flex;
      justify-content: center;
      align-items: center;
      font-size: 16px;
      flex-shrink: 0;
      margin-right: 12px;
      box-shadow: var(--shadow-sm);
    }

    .robot .message-avatar {
      background: var(--primary-light);
      color: var(--primary-color);
    }

    .user .message-avatar {
      background: var(--primary-color);
      color: white;
      margin-right: 0;
      margin-left: 12px;
      order: 1;
    }

    /* 消息气泡优化 */
    .message-bubble {
      padding: 14px 18px;
      border-radius: 18px;
      line-height: 1.5;
      word-wrap: break-word;
      position: relative;
      box-shadow: var(--shadow-sm);
      font-size: 0.95rem;
    }

    /* 打字效果的光标 */
    .message-bubble.typing::after {
      content: '|';
      display: inline-block;
      opacity: 1;
      margin-left: 2px;
      animation: cursor-blink 0.8s infinite;
      font-weight: normal;
      color: var(--primary-color);
    }

    @keyframes cursor-blink {
      0%, 100% { opacity: 1; }
      50% { opacity: 0; }
    }

    .user .message-bubble {
      background: var(--primary-gradient);
      color: #fff;
      border-bottom-right-radius: 4px;
    }

    .robot .message-bubble {
      background: #fff;
      color: var(--text-color);
      border-bottom-left-radius: 4px;
      border: 1px solid rgba(63, 106, 216, 0.1);
    }

    /* 输入预览气泡 */
    .preview-message {
      justify-content: flex-end;
      margin-left: auto;
      opacity: 0.7;
      margin-bottom: 10px;
      display: none; /* 默认隐藏 */
    }

    .preview-message .message-bubble {
      background: var(--primary-gradient);
      color: #fff;
      border-bottom-right-radius: 4px;
      font-style: italic;
    }

    .preview-message .message-avatar {
      background: var(--primary-color);
      color: white;
      margin-right: 0;
      margin-left: 12px;
      order: 1;
    }

    .preview-message.active {
      display: flex; /* 激活时显示 */
    }

    /* 消息时间 */
    .message-time {
      font-size: 0.7rem;
      color: var(--text-muted);
      margin-top: 6px;
      text-align: center;
      opacity: 0.8;
    }

    /* 页脚样式 */
    .chat-footer {
      padding: 12px 16px;
      text-align: center;
      font-size: 0.75rem;
      color: var(--text-muted);
      background: #fff;
      border-top: 1px solid rgba(63, 106, 216, 0.1);
    }

    .chat-footer a {
      color: var(--primary-color);
      text-decoration: none;
      transition: var(--transition);
    }

    .chat-footer a:hover {
      text-decoration: underline;
    }

    /* 优化的输入区域 */
    .chat-input {
      display: flex;
      align-items: center;
      background: #fff;
      border-top: 1px solid rgba(63, 106, 216, 0.1);
      padding: 16px 24px;
      position: relative;
      z-index: 5;
    }

    .chat-input-wrapper {
      flex: 1;
      position: relative;
      display: flex;
      align-items: center;
      background: rgba(247, 249, 252, 0.8);
      border-radius: var(--border-radius-lg);
      padding: 0 14px;
      transition: var(--transition);
      box-shadow: inset 0 0 0 1px rgba(63, 106, 216, 0.1);
    }

    .chat-input-wrapper:focus-within {
      box-shadow: inset 0 0 0 2px rgba(63, 106, 216, 0.3);
      background: #fff;
    }

    .chat-input input {
      flex: 1;
      padding: 14px 16px;
      border: none;
      font-size: 0.95rem;
      background: transparent;
      color: var(--text-color);
      caret-color: var(--primary-color);
    }

    .chat-input input:focus {
      outline: none;
    }

    .chat-input button {
      background: var(--primary-gradient);
      border: none;
      color: #fff;
      padding: 12px 24px;
      border-radius: var(--border-radius-lg);
      margin-left: 12px;
      cursor: pointer;
      font-weight: 600;
      font-size: 0.95rem;
      transition: var(--transition);
      display: flex;
      align-items: center;
      box-shadow: 0 4px 12px rgba(63, 106, 216, 0.2);
    }

    .chat-input button:hover {
      transform: translateY(-2px);
      box-shadow: 0 6px 15px rgba(63, 106, 216, 0.3);
    }

    .chat-input button:active {
      transform: translateY(1px);
      box-shadow: 0 2px 8px rgba(63, 106, 216, 0.2);
    }

    .chat-input button i {
      margin-left: 8px;
    }

    /* 输入框内图标 */
    .input-icon {
      margin: 0 6px;
      color: var(--text-muted);
      cursor: pointer;
      width: 36px;
      height: 36px;
      display: flex;
      align-items: center;
      justify-content: center;
      border-radius: 50%;
      transition: var(--transition);
    }

    .input-icon:hover {
      background: rgba(63, 106, 216, 0.1);
      color: var(--primary-color);
    }

    /* 添加新消息动画 */
    @keyframes pop {
      0% { transform: scale(0.8); }
      50% { transform: scale(1.1); }
      100% { transform: scale(1); }
    }

    .message-new {
      animation: pop 0.4s ease;
    }

    /* 消息加载动画 */
    .typing-indicator {
      display: flex;
      align-items: center;
      padding: 12px 18px;
      border-radius: 20px;
      background: rgba(247, 249, 252, 0.8);
      width: fit-content;
      margin-bottom: 16px;
      box-shadow: var(--shadow-sm);
    }

    .typing-indicator span {
      width: 7px;
      height: 7px;
      background: var(--primary-color);
      border-radius: 50%;
      display: inline-block;
      margin: 0 3px;
      opacity: 0.4;
    }

    .typing-indicator span:nth-child(1) {
      animation: pulse-typing 1s infinite 0s;
    }

    .typing-indicator span:nth-child(2) {
      animation: pulse-typing 1s infinite 0.2s;
    }

    .typing-indicator span:nth-child(3) {
      animation: pulse-typing 1s infinite 0.4s;
    }

    @keyframes pulse-typing {
      0% { opacity: 0.4; transform: scale(1); }
      50% { opacity: 1; transform: scale(1.2); }
      100% { opacity: 0.4; transform: scale(1); }
    }

    /* 系统消息样式 */
    .message.system .message-bubble {
      background: rgba(255, 245, 230, 0.8);
      color: #a05e03;
      border: 1px solid rgba(255, 168, 0, 0.2);
      margin: 0 auto;
    }

    /* 响应式调整 */
    @media (max-width: 768px) {
      body {
        padding: 0;
        height: 100vh;
        overflow: hidden;
        position: fixed;
        width: 100%;
      }

      .chat-container {
        height: 100vh;
        max-height: 100vh;
        border-radius: 0;
        width: 100%;
        max-width: 100%;
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        bottom: 0;
        display: flex;
        flex-direction: column;
      }

      .chat-header {
        border-radius: 0;
        padding: 12px 16px;
        flex-shrink: 0;
      }

      .chat-messages {
        flex: 1;
        overflow-y: auto;
        padding: 16px;
        overscroll-behavior-y: contain; /* 防止iOS回弹滚动 */
        -webkit-overflow-scrolling: touch; /* 增强iOS滚动体验 */
        padding-bottom: 50px; /* 增加底部内边距，防止内容被键盘遮挡 */
      }

      .chat-input {
        padding: 10px 16px;
        flex-shrink: 0;
        position: fixed; /* 改为固定定位 */
        bottom: 0; /* 固定在底部 */
        left: 0;
        right: 0;
        z-index: 100; /* 增加z-index确保在最上层 */
        background: #fff;
        border-top: 1px solid rgba(63, 106, 216, 0.1);
        transform: translateZ(0); /* 启用硬件加速，防止iOS键盘导致的闪烁 */
      }

      /* 当键盘打开时的特殊样式 */
      body.keyboard-open .chat-messages {
        padding-bottom: 80px; /* 键盘打开时增加更多底部内边距 */
      }

      .message {
        max-width: 90%;
      }

      .chat-input button span {
        display: none;
      }

      .chat-input button {
        padding: 10px 12px;
      }

      .chat-input button i {
        margin-left: 0;
      }

      .chat-footer {
        padding: 8px 10px;
        font-size: 0.7rem;
        flex-shrink: 0;
      }
    }

    /* 小屏幕设备进一步优化 */
    @media (max-width: 480px) {
      html, body {
        position: fixed;
        width: 100%;
        height: 100%;
        overflow: hidden;
      }

      .message-avatar {
        width: 32px;
        height: 32px;
        font-size: 14px;
      }

      .message-bubble {
        padding: 10px 12px;
        font-size: 0.9rem;
      }

      .chat-header-title {
        font-size: 1rem;
      }

      .status-indicator {
        font-size: 0.7rem;
        padding: 3px 8px;
      }

      .chat-header .badge {
        font-size: 0.65rem;
        padding: 2px 6px;
      }

      .chat-input-wrapper {
        padding: 0 8px;
      }

      .chat-input input {
        padding: 10px 12px;
        font-size: 0.9rem;
      }
    }

    /* iOS 设备特殊处理 */
    @supports (-webkit-touch-callout: none) {
      .chat-container {
        height: 100vh;
        height: -webkit-fill-available;
      }

      .chat-input {
        padding-bottom: calc(10px + env(safe-area-inset-bottom) / 2);
        /* 添加底部安全区域，确保在全面屏iPhone上不被遮挡 */
        bottom: env(safe-area-inset-bottom, 0);
      }

      .chat-footer {
        padding-bottom: calc(8px + env(safe-area-inset-bottom));
      }

      /* 修复iOS键盘问题的特殊处理 */
      body.keyboard-open .chat-messages {
        height: calc(100vh - 150px);
      }

      /* 处理iOS键盘弹出时的特殊调整 */
      body.keyboard-open .chat-input {
        transform: translateY(0) !important;
        position: sticky !important;
      }
    }

    /* 性能优化: 减少动画对性能的影响 */
    @media (prefers-reduced-motion: reduce) {
      * {
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.01ms !important;
        scroll-behavior: auto !important;
      }
    }

    /* Markdown 样式优化 */
    .message-bubble.markdown-body {
      padding: 14px 18px;
    }

    .message-bubble.markdown-body h1 {
      font-size: 1.5rem;
      margin-top: 0.5rem;
      margin-bottom: 0.5rem;
      padding-bottom: 0.3rem;
      border-bottom: 1px solid rgba(0,0,0,0.1);
    }

    .message-bubble.markdown-body h2 {
      font-size: 1.25rem;
      margin-top: 0.5rem;
      margin-bottom: 0.5rem;
    }

    .message-bubble.markdown-body h3, 
    .message-bubble.markdown-body h4, 
    .message-bubble.markdown-body h5, 
    .message-bubble.markdown-body h6 {
      font-size: 1.1rem;
      margin-top: 0.5rem;
      margin-bottom: 0.5rem;
    }

    .message-bubble.markdown-body p {
      margin-bottom: 0.75rem;
    }

    .message-bubble.markdown-body ul, 
    .message-bubble.markdown-body ol {
      margin-bottom: 0.75rem;
      padding-left: 1.5rem;
    }

    .message-bubble.markdown-body li {
      margin-bottom: 0.25rem;
    }

    .message-bubble.markdown-body code {
      background-color: rgba(63, 106, 216, 0.1);
      padding: 0.1rem 0.3rem;
      border-radius: 3px;
      font-family: SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace;
      font-size: 0.85em;
    }

    .message-bubble.markdown-body pre {
      background-color: #f6f8fa;
      border-radius: 6px;
      padding: 10px;
      overflow-x: auto;
      margin-bottom: 0.75rem;
      position: relative;
    }

    .message-bubble.markdown-body pre::after {
      content: attr(data-language);
      position: absolute;
      top: 0;
      right: 0;
      color: #8c8c8c;
      font-size: 0.7rem;
      padding: 3px 7px;
      background: rgba(0,0,0,0.05);
      border-radius: 0 6px 0 6px;
    }

    .message-bubble.markdown-body pre code {
      background-color: transparent;
      padding: 0;
      font-size: 0.85em;
      white-space: pre;
    }

    .message-bubble.markdown-body blockquote {
      border-left: 3px solid rgba(63, 106, 216, 0.3);
      padding-left: 0.75rem;
      color: #646464;
      margin-left: 0;
      margin-right: 0;
    }

    .message-bubble.markdown-body table {
      border-collapse: collapse;
      width: 100%;
      margin-bottom: 0.75rem;
      font-size: 0.9em;
    }

    .message-bubble.markdown-body table th,
    .message-bubble.markdown-body table td {
      padding: 6px 10px;
      border: 1px solid #e1e4e8;
    }

    .message-bubble.markdown-body table th {
      background-color: #f6f8fa;
    }

    /* 改善移动设备上的Markdown显示 */
    @media (max-width: 768px) {
      .message-bubble.markdown-body {
        font-size: 0.9rem;
        padding: 12px 14px;
      }

      .message-bubble.markdown-body h1 {
        font-size: 1.3rem;
      }

      .message-bubble.markdown-body h2 {
        font-size: 1.15rem;
      }

      .message-bubble.markdown-body h3, 
      .message-bubble.markdown-body h4, 
      .message-bubble.markdown-body h5, 
      .message-bubble.markdown-body h6 {
        font-size: 1rem;
      }

      .message-bubble.markdown-body pre {
        max-height: 200px;
      }

      .message-bubble.markdown-body table {
        font-size: 0.8em;
      }

      .message-bubble.markdown-body ul, 
      .message-bubble.markdown-body ol {
        padding-left: 1.2rem;
      }
    }

    /* 确保用户消息中的Markdown不会被渲染 */
    .user .message-bubble {
      white-space: pre-wrap;
    }

    /* 浮动按钮与模态窗口样式 */
    .floating-button {
      position: fixed;
      bottom: 300px;
      right: 20px;
      width: 56px;
      height: 56px;
      border-radius: 50%;
      background: var(--primary-gradient);
      box-shadow: var(--shadow);
      display: flex;
      align-items: center;
      justify-content: center;
      color: white;
      font-size: 24px;
      cursor: pointer;
      z-index: 1000;
      transition: var(--transition);
    }

    .floating-button:hover {
      transform: translateY(-3px);
      box-shadow: var(--shadow-lg);
    }

    .floating-button:active {
      transform: translateY(1px);
    }

    /* 模态窗样式 */
    .modal-overlay {
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      bottom: 0;
      background: rgba(0, 0, 0, 0.5);
      backdrop-filter: blur(3px);
      -webkit-backdrop-filter: blur(3px);
      z-index: 1001;
      display: flex;
      align-items: center;
      justify-content: center;
      opacity: 0;
      visibility: hidden;
      transition: opacity 0.3s ease, visibility 0.3s ease;
    }

    .modal-overlay.active {
      opacity: 1;
      visibility: visible;
    }

    .modal-container {
      background: white;
      border-radius: var(--border-radius);
      width: 90%;
      max-width: 400px;
      padding: 24px;
      box-shadow: var(--shadow-lg);
      transform: translateY(20px);
      opacity: 0;
      transition: transform 0.3s ease, opacity 0.3s ease;
    }

    .modal-overlay.active .modal-container {
      transform: translateY(0);
      opacity: 1;
    }

    .modal-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 16px;
      padding-bottom: 16px;
      border-bottom: 1px solid rgba(0, 0, 0, 0.1);
    }

    .modal-header h3 {
      font-size: 1.25rem;
      font-weight: 600;
      color: var(--text-color);
      margin: 0;
    }

    .modal-close {
      cursor: pointer;
      font-size: 1.5rem;
      color: var(--text-muted);
      transition: var(--transition);
    }

    .modal-close:hover {
      color: var(--danger-color);
    }

    .bot-switcher {
      display: grid;
      grid-template-columns: 1fr;
      gap: 12px;
    }

    .bot-option {
      display: flex;
      align-items: center;
      padding: 12px 16px;
      border-radius: var(--border-radius);
      border: 1px solid rgba(0, 0, 0, 0.1);
      cursor: pointer;
      transition: var(--transition);
      position: relative;
      overflow: hidden;
    }

    .bot-option.active {
      border-color: transparent;
    }

    .bot-option.active::before {
      content: '';
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      bottom: 0;
      z-index: -1;
      opacity: 0.1;
    }

    .bot-option:hover {
      transform: translateY(-2px);
      box-shadow: var(--shadow);
    }

    .bot-option:active {
      transform: translateY(1px);
    }

    .bot-option .bot-icon {
      width: 42px;
      height: 42px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      margin-right: 12px;
      font-size: 18px;
      color: white;
      flex-shrink: 0;
    }

    .bot-option .bot-info {
      flex-grow: 1;
    }

    .bot-option .bot-name {
      font-weight: 600;
      font-size: 1rem;
      margin-bottom: 2px;
    }

    .bot-option .bot-desc {
      font-size: 0.8rem;
      color: var(--text-muted);
    }

    .bot-option .bot-selected {
      width: 20px;
      height: 20px;
      border-radius: 50%;
      border: 2px solid var(--primary-color);
      margin-left: 8px;
      position: relative;
      flex-shrink: 0;
      overflow: hidden;
      display: none;
    }

    .bot-option.active .bot-selected {
      display: block;
    }

    .bot-option.active .bot-selected::after {
      content: '';
      position: absolute;
      top: 50%;
      left: 50%;
      transform: translate(-50%, -50%);
      width: 12px;
      height: 12px;
      border-radius: 50%;
    }

    /* 机器人特定颜色 */
    .bot-option:nth-child(1) .bot-icon {
      background: var(--bot1-gradient);
    }

    .bot-option:nth-child(2) .bot-icon {
      background: var(--bot2-gradient);
    }

    .bot-option:nth-child(3) .bot-icon {
      background: var(--bot3-gradient);
    }

    .bot-option:nth-child(1).active .bot-selected {
      border-color: var(--bot1-color);
    }

    .bot-option:nth-child(2).active .bot-selected {
      border-color: var(--bot2-color);
    }

    .bot-option:nth-child(3).active .bot-selected {
      border-color: var(--bot3-color);
    }

    .bot-option:nth-child(1).active .bot-selected::after {
      background: var(--bot1-color);
    }

    .bot-option:nth-child(2).active .bot-selected::after {
      background: var(--bot2-color);
    }

    .bot-option:nth-child(3).active .bot-selected::after {
      background: var(--bot3-color);
    }

    .bot-option:nth-child(1).active::before {
      background: var(--bot1-color);
    }

    .bot-option:nth-child(2).active::before {
      background: var(--bot2-color);
    }

    .bot-option:nth-child(3).active::before {
      background: var(--bot3-color);
    }

    /* 响应式样式 */
    @media (max-width: 768px) {
      .floating-button {
        bottom: 200px;
        right: 16px;
        width: 50px;
        height: 50px;
        font-size: 20px;
      }

      .bot-option {
        padding: 10px 14px;
      }

      .bot-option .bot-icon {
        width: 36px;
        height: 36px;
        font-size: 16px;
      }

      .modal-container {
        padding: 18px;
      }
    }

    /* 小屏幕适配 */
    @media (max-width: 480px) {
      .floating-button {
        bottom: 280px;
        right: 330px;
        width: 45px;
        height: 45px;
        font-size: 18px;
      }
    }

    /* 避免iOS Safari底部栏遮挡浮动按钮 */
    @supports (-webkit-touch-callout: none) {
      .floating-button {
        bottom: calc(20px + env(safe-area-inset-bottom));
      }
    }
  </style>
</head>
<body>
  <!-- 背景装饰 -->
  <div class="decoration decoration-1"></div>
  <div class="decoration decoration-2"></div>

  <!-- 浮动按钮 -->
  <div class="floating-button" id="botSwitchButton">
    <i class="fas fa-exchange-alt"></i>
  </div>

  <!-- 机器人切换模态窗口 -->
  <div class="modal-overlay" id="botSwitchModal">
    <div class="modal-container">
      <div class="modal-header">
        <h3>选择AI助手</h3>
        <div class="modal-close" id="closeModal">
          <i class="fas fa-times"></i>
        </div>
      </div>
      <div class="bot-switcher" id="botOptions">
        <!-- 机器人选项将由JS动态加载 -->
      </div>
    </div>
  </div>

  <div class="chat-container">
    <div class="chat-header">
      <div class="chat-header-title">
        <i class="fas fa-robot" id="botIcon"></i> <span id="botName">智能全能顾问</span> <span class="badge">AI助手</span>
      </div>
      <div class="status-indicator">
        <span class="status-dot"></span>
        <span>在线</span>
      </div>
    </div>
    <div class="chat-messages" id="chat">
      <!-- 预览气泡将在欢迎消息后动态添加 -->
    </div>
    <div class="chat-input">
      <div class="chat-input-wrapper">
        <input type="text" id="messageInput" placeholder="请输入您的问题..." autocomplete="off">
      </div>
      <button onclick="sendMessage()"><span>发送</span> <i class="fas fa-paper-plane"></i></button>
    </div>

  </div>

  <script src="https://hxkecheng.oss-cn-beijing.aliyuncs.com/cozebot/bot_XHEnT/marked.min.js"></script>
  <script src="https://hxkecheng.oss-cn-beijing.aliyuncs.com/cozebot/bot_XHEnT/highlight.min.js"></script>
  <script>
    // 配置Marked以支持表格渲染
    function setupMarked() {
      // 配置Marked选项
      marked.setOptions({
        gfm: true, // 启用GitHub风格Markdown
        breaks: true, // 允许回车换行
        tables: true, // 支持表格
        sanitize: false, // 允许HTML标签
        highlight: function(code, lang) {
          // 使用highlight.js进行代码高亮
          if (lang && hljs.getLanguage(lang)) {
            try {
              return hljs.highlight(code, { language: lang }).value;
            } catch (e) {
              console.error("高亮错误:", e);
            }
          }
          return code;
        }
      });
    }

    // 检测消息是否包含Markdown并渲染
    function tryRenderMarkdown(element) {
      if (!element) return;

      // 确保marked已配置
      setupMarked();

      const content = element.textContent;

      // 检查是否包含Markdown表格标记或其他Markdown特征
      const hasMarkdown = 
        content.includes('|') && content.includes('|-') || // 表格
        content.includes('##') || // 标题
        content.includes('```') || // 代码块
        content.includes('*') || // 列表或强调
        content.includes('[') && content.includes(']('); // 链接

      if (hasMarkdown) {
        try {
          // 修复安全问题：使用DOMPurify或转义HTML
          const escapedContent = content
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');

          // 设置安全选项，确保代码块内内容被转义
          marked.setOptions({
            gfm: true,
            breaks: true,
            tables: true,
            sanitize: false, // 现代marked版本不再使用sanitize选项
            renderer: new marked.Renderer(),
            highlight: function(code, lang) {
              // 使用highlight.js进行代码高亮，确保代码被转义
              if (lang && hljs.getLanguage(lang)) {
                try {
                  return hljs.highlight(code, { language: lang }).value;
                } catch (e) {
                  console.error("高亮错误:", e);
                }
              }
              // 确保代码被转义
              return hljs.highlightAuto(code).value;
            }
          });

          // 保存到变量以避免重复解析
          const html = marked.parse(escapedContent);

          // 添加markdown-body类以应用样式
          element.classList.add('markdown-body');

          // 更新内容为渲染后的HTML
          element.innerHTML = html;

          // 对代码块应用高亮，但确保内容已被转义
          element.querySelectorAll('pre code').forEach(block => {
            // 不要重新高亮，让marked的highlight选项处理
            // 只设置样式和语言标记
            const parent = block.parentElement;
            if (block.className) {
              const match = block.className.match(/language-(\w+)/);
              if (match && match[1]) {
                parent.dataset.language = match[1];
              }
            }
          });

          // 优化表格显示
          enhanceTableDisplay(element);

          return true;
        } catch (e) {
          console.error("Markdown渲染错误:", e);
          return false;
        }
      }

      return false;
    }

    // 优化表格显示效果
    function enhanceTableDisplay(element) {
      const tables = element.querySelectorAll('table');
      tables.forEach(table => {
        // 添加Bootstrap表格类
        table.classList.add('table', 'table-bordered', 'table-striped', 'table-sm');

        // 添加响应式包装器
        const wrapper = document.createElement('div');
        wrapper.classList.add('table-responsive');
        wrapper.style.overflow = 'auto';
        wrapper.style.maxWidth = '100%';

        // 将表格放入包装器
        table.parentNode.insertBefore(wrapper, table);
        wrapper.appendChild(table);

        // 优化表格内容
        const headerRow = table.querySelector('thead tr');
        if (headerRow) {
          headerRow.style.backgroundColor = 'rgba(63, 106, 216, 0.1)';
          headerRow.style.fontWeight = 'bold';
        }

        // 使表格行更易读
        const rows = table.querySelectorAll('tbody tr');
        rows.forEach((row, index) => {
          if (index % 2 === 0) {
            row.style.backgroundColor = 'rgba(247, 249, 252, 0.5)';
          }
        });
      });
    }

    // 机器人配置
    const botConfigs = [
      {
        id: '7436265511035518985',
        name: '智能全能顾问',
        description: '解答各类问题的全能AI助手',
        icon: 'fa-robot',
        color: '#3f6ad8',
        gradient: 'linear-gradient(135deg, #3f6ad8, #5d87f5)'
      },
      {
        id: '7472199166358831140',
        name: 'AI建议顾问',
        description: '提供专业建议和指导',
        icon: 'fa-lightbulb',
        color: '#ff9800',
        gradient: 'linear-gradient(135deg, #ff9800, #ffb74d)'
      },
      {
        id: '7473373856972554292',
        name: 'AI产品顾问',
        description: '专注于产品相关咨询服务',
        icon: 'fa-shopping-cart',
        color: '#4caf50',
        gradient: 'linear-gradient(135deg, #4caf50, #81c784)'
      }
    ];

    // 当前选中的机器人
    let currentBotId = '7436265511035518985';

    // 获取当前机器人配置
    function getCurrentBotConfig() {
      return botConfigs.find(bot => bot.id === currentBotId) || botConfigs[0];
    }

    // 生成唯一用户ID
    function generateUUID() {
      return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
        const r = Math.random() * 16 | 0, v = c == 'x' ? r : (r & 0x3 | 0x8);
        return v.toString(16);
      });
    }

    // 获取或创建用户ID
    let userId = localStorage.getItem('cozeUserId');
    if (!userId) {
      userId = 'user_' + generateUUID();
      localStorage.setItem('cozeUserId', userId);
    }

    // WebSocket连接
    let ws = null;
    let wsRetries = 0;
    const maxRetries = 5;
    let currentResponseElem = null;
    let waitingForResponse = false;
    let pingInterval = null;
    let isFirstToken = true; // 跟踪是否是首次token

    // 连接WebSocket
    function connectWebSocket() {
      ws = new WebSocket(`wss://${location.host}/ws?user_id=${encodeURIComponent(userId)}`);

      ws.onopen = function() {
        console.log("✅ WebSocket连接成功！");
        document.querySelector('.status-dot').style.background = '#4caf50';
        if (pingInterval) clearInterval(pingInterval);
        pingInterval = setInterval(sendPing, 30000);
        wsRetries = 0;
        isFirstToken = true; // 重置首次token标记
      };

      ws.onmessage = function(event) {
        if (event.data === "pong") return;

        try {
          // 尝试解析为JSON，可能是机器人切换响应
          const jsonData = JSON.parse(event.data);
          if (jsonData.type === "bot_switched") {
            // 处理机器人切换事件
            handleBotSwitched(jsonData.bot);
            return;
          }
        } catch (e) {
          // 不是JSON，按正常消息处理
        }

        console.log("📩 收到消息:", event.data);

        if (document.querySelector('.typing-indicator')) {
          document.querySelector('.typing-indicator').remove();
        }

        if (currentResponseElem) {
          // 检查是否是完成消息
          if (event.data === "__COMPLETE__") {
            // 消息完成，移除打字中状态
            currentResponseElem.classList.remove('typing');
            waitingForResponse = false;

            // 检测消息是否包含Markdown格式，并进行渲染
            tryRenderMarkdown(currentResponseElem);

            // 添加小提示，鼓励用户继续提问
            let hint = document.createElement("div");
            hint.className = "message-hint";
            hint.innerHTML = "您可以继续提问或了解更多信息～";
            hint.style.fontSize = "0.75rem";
            hint.style.color = "var(--text-muted)";
            hint.style.textAlign = "center";
            hint.style.margin = "10px 0";
            hint.style.opacity = "0";
            document.getElementById("chat").appendChild(hint);

            // 淡入效果
            setTimeout(() => {
              hint.style.transition = "opacity 0.5s ease";
              hint.style.opacity = "0.7";

              // 确保滚动到最底部
              ensureScrollToBottom();
            }, 500);

            return;
          }

          // 打字机效果：以自然的方式添加文本
          const newText = event.data;

          // 模拟自然打字的过程
          if (newText.length > 0) {
            // 将waitingForResponse设为false以允许继续接收更多消息
            waitingForResponse = false;

            // 添加字符
            currentResponseElem.textContent += newText;

            // 添加闪烁的光标效果 - 仅在第一个token时添加
            if (isFirstToken && !currentResponseElem.classList.contains('typing')) {
              currentResponseElem.classList.add('typing');
              isFirstToken = false;
            }

            // 自动滚动到最新消息
            ensureScrollToBottom();
          }
        }
      };

      ws.onerror = function(error) {
        console.error("❌ WebSocket错误:", error);
        document.querySelector('.status-dot').style.background = '#f64e60';
      };

      ws.onclose = function(event) {
        console.log("WebSocket连接关闭", event.code, event.reason);
        document.querySelector('.status-dot').style.background = '#ffa800';

        if (pingInterval) {
          clearInterval(pingInterval);
          pingInterval = null;
        }

        // 自动重连逻辑
        if (wsRetries < maxRetries) {
          wsRetries++;
          const delay = Math.min(1000 * Math.pow(2, wsRetries), 30000);
          console.log(`尝试在${delay}ms后重新连接...`);
          setTimeout(connectWebSocket, delay);
        } else {
          let chatDiv = document.getElementById("chat");
          let disconnectMsg = document.createElement("div");
          disconnectMsg.className = "message system";
          disconnectMsg.innerHTML = '<div class="message-bubble">连接已断开，请刷新页面重试</div>';
          chatDiv.appendChild(disconnectMsg);
        }
      };
    }

    // 首次连接
    connectWebSocket();

    // 定期发送ping保持连接
    function sendPing() {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send("ping");
      }
    }

    // 发送消息
    function sendMessage() {
      let inputElem = document.getElementById("messageInput");
      let input = inputElem.value.trim();

      if (!input || waitingForResponse) return;

      // 检查WebSocket状态
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        alert("连接已断开，请刷新页面重试");
        return;
      }

      console.log("📤 发送消息:", input);
      waitingForResponse = true;

      // 隐藏预览气泡
      const previewMessage = document.getElementById("previewMessage");
      if (previewMessage) {
        previewMessage.remove(); // 完全移除，而不只是隐藏
      }

      let chatDiv = document.getElementById("chat");
      let now = new Date();
      let timeStr = now.getHours().toString().padStart(2, '0') + ':' + 
                    now.getMinutes().toString().padStart(2, '0');

      // 添加用户消息
      let userMsgElem = document.createElement("div");
      userMsgElem.className = "message user message-new";
      userMsgElem.innerHTML = `
        <div class="message-content">
          <div class="message-bubble">${escapeHtml(input)}</div>
          <div class="message-avatar"><i class="fas fa-user"></i></div>
        </div>
      `;
      chatDiv.appendChild(userMsgElem);

      // 添加时间标记
      let timeElem = document.createElement("div");
      timeElem.className = "message-time";
      timeElem.textContent = timeStr;
      chatDiv.appendChild(timeElem);

      // 清空输入框
      inputElem.value = "";

      // 添加机器人"正在输入"提示
      let typingElem = document.createElement("div");
      typingElem.className = "typing-indicator";
      typingElem.innerHTML = '<span></span><span></span><span></span>';
      chatDiv.appendChild(typingElem);

      // 添加机器人消息容器
      let robotMsgElem = document.createElement("div");
      robotMsgElem.className = "message robot";
      robotMsgElem.innerHTML = `
        <div class="message-content">
          <div class="message-avatar"><i class="fas ${getCurrentBotConfig().icon}"></i></div>
          <div class="message-bubble"></div>
        </div>
      `;
      chatDiv.appendChild(robotMsgElem);

      // 获取气泡元素用于添加响应内容
      currentResponseElem = robotMsgElem.querySelector(".message-bubble");

      // 标记为首次接收
      isFirstToken = true;

      // 发送消息到服务器
      ws.send(input);

      // 自动滚动到底部
      ensureScrollToBottom();

      // 设置焦点回到输入框
      inputElem.focus();
    }

    // 确保滚动到底部的函数
    function ensureScrollToBottom() {
      const chatDiv = document.getElementById("chat");
      // 立即滚动一次
      chatDiv.scrollTop = chatDiv.scrollHeight;

      // 延迟一点再滚动，确保在DOM更新后
      setTimeout(() => {
        chatDiv.scrollTop = chatDiv.scrollHeight;
      }, 50);

      // 再次延迟以适应动画和渲染
      setTimeout(() => {
        chatDiv.scrollTop = chatDiv.scrollHeight;
      }, 150);
    }

    // 安全处理HTML内容
    function escapeHtml(text) {
      const div = document.createElement('div');
      div.textContent = text;
      return div.innerHTML;
    }

    // 响应Enter键
    document.getElementById("messageInput").addEventListener("keypress", function(e) {
      if (e.key === "Enter") {
        e.preventDefault();
        sendMessage();
      }
    });

    // 切换机器人
    function switchBot(botId) {
      if (botId === currentBotId) {
        closeModal();
        return;
      }

      if (waitingForResponse) {
        alert("正在等待响应，请稍后再切换");
        return;
      }

      // 发送切换机器人请求
      if (ws && ws.readyState === WebSocket.OPEN) {
        const switchRequest = {
          action: 'switch_bot',
          bot_id: botId
        };

        // 清空聊天区域
        document.getElementById("chat").innerHTML = '';

        // 设置当前机器人ID
        currentBotId = botId;

        // 更新UI
        updateBotUI();

        // 发送切换请求
        ws.send(JSON.stringify(switchRequest));

        // 添加系统消息与欢迎消息，然后重新设置键盘检测
        setTimeout(() => {
          let botConfig = getCurrentBotConfig();
          addSystemMessage(`已切换到${botConfig.name}，开始新的对话`);

          // 添加欢迎消息
          addWelcomeMessage(botConfig);

          // 重新初始化键盘检测，确保预览气泡功能在切换后依然可用
          setupKeyboardDetection();
        }, 300);

        // 关闭模态窗
        closeModal();
      } else {
        alert("连接已断开，请刷新页面重试");
      }
    }

    // 添加欢迎消息的独立函数 - 便于在切换机器人和初始加载时复用
    function addWelcomeMessage(botConfig) {
      let chatDiv = document.getElementById("chat");

      let welcomeMsg = document.createElement("div");
      welcomeMsg.className = "message robot message-new";
      welcomeMsg.innerHTML = `
        <div class="message-content">
          <div class="message-avatar"><i class="fas ${botConfig.icon}"></i></div>
          <div class="message-bubble">
            您好！我是您的${botConfig.name}。<br>
            请问有什么问题需要咨询吗？请详细说明！<br>
            ⚠️ 例如:最近一周睡眠质量差，凌晨3-4点总会醒，白天喝3杯咖啡，想改善睡眠...<br>
            <small style="display: block; margin-top: 8px; opacity: 0.7;">我可以为您解答各类问题和提供建议，您可以点击右下角的切换按钮选择其他AI助手！</small>
          </div>
        </div>
      `;
      chatDiv.appendChild(welcomeMsg);

      // 添加时间
      let now = new Date();
      let timeStr = now.getHours().toString().padStart(2, '0') + ':' + 
                   now.getMinutes().toString().padStart(2, '0');

      let timeElem = document.createElement("div");
      timeElem.className = "message-time";
      timeElem.textContent = timeStr;
      chatDiv.appendChild(timeElem);

      // 确保滚动到底部
      ensureScrollToBottom();
    }

    // 处理机器人切换响应
    function handleBotSwitched(bot) {
      console.log("机器人已切换:", bot);
      // 根据需要处理其他逻辑
    }

    // 更新机器人UI
    function updateBotUI() {
      const botConfig = getCurrentBotConfig();

      // 更新顶部标题和图标
      document.getElementById("botName").textContent = botConfig.name;
      document.getElementById("botIcon").className = `fas ${botConfig.icon}`;

      // 更新主题颜色
      document.documentElement.style.setProperty('--primary-color', botConfig.color);
      document.documentElement.style.setProperty('--primary-gradient', botConfig.gradient);

      // 更新模态窗中选中状态
      const options = document.querySelectorAll('.bot-option');
      options.forEach(option => {
        if (option.dataset.botId === currentBotId) {
          option.classList.add('active');
        } else {
          option.classList.remove('active');
        }
      });
    }

    // 添加系统消息
    function addSystemMessage(message) {
      let chatDiv = document.getElementById("chat");
      let systemMsg = document.createElement("div");
      systemMsg.className = "message system";
      systemMsg.innerHTML = `<div class="message-bubble">${message}</div>`;
      chatDiv.appendChild(systemMsg);

      // 自动滚动到底部
      ensureScrollToBottom();
    }

    // 浮动按钮点击事件
    document.addEventListener('DOMContentLoaded', function() {
      document.getElementById("botSwitchButton").addEventListener("click", function() {
        openModal();
      });

      // 关闭按钮点击事件
      document.getElementById("closeModal").addEventListener("click", function() {
        closeModal();
      });

      // 点击模态窗背景关闭
      document.getElementById("botSwitchModal").addEventListener("click", function() {
        closeModal();
      });
    });

    // 打开模态窗
    function openModal() {
      const modal = document.getElementById("botSwitchModal");
      modal.classList.add("active");

      // 初始化机器人选项
      const optionsContainer = document.getElementById("botOptions");

      // 清空现有选项
      optionsContainer.innerHTML = '';

      // 添加机器人选项
      botConfigs.forEach(bot => {
        const option = document.createElement("div");
        option.className = `bot-option ${bot.id === currentBotId ? 'active' : ''}`;
        option.dataset.botId = bot.id;
        option.innerHTML = `
          <div class="bot-icon">
            <i class="fas ${bot.icon}"></i>
          </div>
          <div class="bot-info">
            <div class="bot-name">${bot.name}</div>
            <div class="bot-desc">${bot.description}</div>
          </div>
          <div class="bot-selected"></div>
        `;
        option.addEventListener("click", function() {
          switchBot(bot.id);
        });
        optionsContainer.appendChild(option);
      });

      // 阻止冒泡以防点击内部元素关闭模态窗
      modal.querySelector(".modal-container").addEventListener("click", function(e) {
        e.stopPropagation();
      });
    }

    // 关闭模态窗
    function closeModal() {
      document.getElementById("botSwitchModal").classList.remove("active");
    }

    // 在按ESC键时关闭模态窗
    document.addEventListener("keydown", function(e) {
      if (e.key === "Escape") {
        closeModal();
      }
    });

    // 添加欢迎消息
    window.addEventListener('load', function() {
      setTimeout(function() {
        const botConfig = getCurrentBotConfig();

        // 使用提取的公共方法添加欢迎消息
        addWelcomeMessage(botConfig);

        // 初始化UI
        updateBotUI();

        // 设置键盘检测
        setupKeyboardDetection();
      }, 500);
    });

    // 检测键盘状态并调整界面
    function setupKeyboardDetection() {
      // 先移除旧的预览气泡元素(如果存在)
      const oldPreview = document.getElementById("previewMessage");
      if (oldPreview) {
        oldPreview.remove();
      }

      const inputElem = document.getElementById("messageInput");
      let previewMessage = null; // 重置为null确保创建新的预览气泡
      let previewBubble;
      const chatInputContainer = document.querySelector(".chat-input");
      const originalViewportHeight = window.visualViewport ? window.visualViewport.height : window.innerHeight;

      // 创建预览气泡元素
      function createPreviewBubble() {
        if (!previewMessage || !document.getElementById("previewMessage")) {
          const chatDiv = document.getElementById("chat");

          // 创建预览消息元素
          previewMessage = document.createElement("div");
          previewMessage.className = "message preview-message";
          previewMessage.id = "previewMessage";
          previewMessage.innerHTML = `
            <div class="message-content">
              <div class="message-bubble" id="previewBubble"></div>
              <div class="message-avatar"><i class="fas fa-user"></i></div>
            </div>
          `;

          // 查找聊天区域中最后一条消息
          const messages = chatDiv.querySelectorAll(".message:not(.preview-message)");
          if (messages.length > 0) {
            // 找到最后一条消息
            const lastMessage = messages[messages.length - 1];
            if (lastMessage.nextSibling && lastMessage.nextSibling.className === "message-time") {
              // 如果最后一条消息后有时间元素，在时间元素后插入
              chatDiv.insertBefore(previewMessage, lastMessage.nextSibling.nextSibling);
            } else {
              // 否则直接在最后一条消息后插入
              chatDiv.insertBefore(previewMessage, lastMessage.nextSibling);
            }
          } else {
            // 如果没有其他消息，直接添加到聊天区域
            chatDiv.appendChild(previewMessage);
          }

          // 获取预览气泡元素
          previewBubble = document.getElementById("previewBubble");
        }
        return previewMessage;
      }

      // 立即创建预览气泡
      createPreviewBubble();
      previewBubble = document.getElementById("previewBubble");

      // 检测视窗大小变化（键盘弹出会改变视窗）
      function onViewportResize() {
        const currentViewportHeight = window.visualViewport ? window.visualViewport.height : window.innerHeight;

        // 如果当前视窗高度明显小于原始高度，则认为键盘打开
        if (currentViewportHeight < originalViewportHeight * 0.8) {
          document.body.classList.add('keyboard-open');

          // 确保预览气泡存在
          if (!previewMessage || !document.getElementById("previewMessage")) {
            createPreviewBubble();
            previewBubble = document.getElementById("previewBubble");
          }

          // 键盘打开时显示预览气泡
          updatePreviewBubble();
          setTimeout(ensureInputVisible, 300);

          // 处理iOS的特殊情况，防止输入框被遮挡
          if (/iPhone|iPad|iPod/.test(navigator.userAgent)) {
            // 获取视窗底部位置相对于页面的偏移
            if (window.visualViewport) {
              const offsetY = window.visualViewport.offsetTop + window.visualViewport.height;
              chatInputContainer.style.transform = `translateY(-${window.innerHeight - offsetY}px)`;
            }
          }
        } else {
          document.body.classList.remove('keyboard-open');
          chatInputContainer.style.transform = '';
          // 键盘关闭时隐藏预览气泡
          if (previewMessage) {
            previewMessage.classList.remove('active');
          }
        }
      }

      // 更新预览气泡内容
      function updatePreviewBubble() {
        // 移除旧的预览气泡
        const oldPreview = document.getElementById("previewMessage");
        if (oldPreview) {
          oldPreview.remove();
        }

        // 重新创建预览气泡并放置在最新位置
        createPreviewBubble();
        previewBubble = document.getElementById("previewBubble");

        const inputText = inputElem.value.trim();

        if (inputText && document.body.classList.contains('keyboard-open')) {
          previewBubble.textContent = inputText;
          previewMessage.classList.add('active');

          // 确保预览气泡可见
          previewMessage.scrollIntoView({ behavior: 'smooth', block: 'end' });

          // 额外确保滚动到底部
          setTimeout(() => {
            ensureScrollToBottom();
          }, 50);
        } else if (previewMessage) {
          previewMessage.classList.remove('active');
        }
      }

      // 确保输入框可见
      function ensureInputVisible() {
        // 滚动容器到适当位置以确保输入框和发送按钮同时可见
        const chatMessagesContainer = document.querySelector(".chat-messages");
        chatMessagesContainer.scrollTop = chatMessagesContainer.scrollHeight;

        // 确保整个输入区域在视图中，包括发送按钮
        if (chatInputContainer) {
          chatInputContainer.scrollIntoView({ behavior: 'smooth', block: 'end' });
        }
      }

      // 输入框失去焦点时隐藏预览气泡
      inputElem.addEventListener('blur', function() {
        // 短暂延迟，以防用户点击发送按钮
        setTimeout(() => {
          const preview = document.getElementById("previewMessage");
          if (preview) {
            preview.classList.remove('active');
          }
        }, 300);
      });

      // 处理输入时自动滚动和更新预览气泡
      inputElem.addEventListener('input', function() {
        if (document.body.classList.contains('keyboard-open')) {
          updatePreviewBubble();
        }
      });

      // 清除之前可能存在的事件监听器
      if (window.viewportResizeHandler) {
        if (window.visualViewport) {
          window.visualViewport.removeEventListener('resize', window.viewportResizeHandler);
          window.visualViewport.removeEventListener('scroll', window.viewportResizeHandler);
        } else {
          window.removeEventListener('resize', window.viewportResizeHandler);
        }
      }

      // 保存新的事件处理函数引用以便将来可以移除
      window.viewportResizeHandler = onViewportResize;

      // 使用visualViewport API（如果可用）
      if (window.visualViewport) {
        window.visualViewport.addEventListener('resize', window.viewportResizeHandler);
        window.visualViewport.addEventListener('scroll', window.viewportResizeHandler);
      } else {
        // 降级方案：使用窗口resize事件和输入框聚焦事件
        window.addEventListener('resize', window.viewportResizeHandler);
      }

      // 输入框聚焦时确保其可见
      inputElem.addEventListener('focus', function() {
        // 添加短延迟，等待键盘弹出
        setTimeout(() => {
          ensureInputVisible();
          updatePreviewBubble(); // 重新创建并定位预览气泡
        }, 400);
        // 额外延迟一次以处理某些设备上的慢速键盘动画
        setTimeout(() => {
          ensureInputVisible();
          updatePreviewBubble(); // 再次确保预览气泡正确放置
        }, 1000);
      });

      // 初始检查键盘状态
      onViewportResize();

      console.log("键盘检测和预览气泡功能已重新初始化");
    }
  </script>
</body>
</html>
"""


@app.get("/")
async def get():
    return HTMLResponse(html)


@app.get("/stats")
async def get_stats():
    """返回服务器统计信息，仅用于监控"""
    uptime = time.time() - STATS["start_time"]
    avg_response_time = sum(STATS["response_times"]) / len(STATS["response_times"]) if STATS["response_times"] else 0

    stats = {
        **STATS,
        "uptime": f"{uptime:.2f}秒",
        "uptime_hours": f"{uptime / 3600:.2f}小时",
        "avg_response_time": f"{avg_response_time:.2f}秒",
        "cache_size": len(app.state.response_cache.cache),
        "cache_hit_rate": f"{(STATS['cache_hits'] / STATS['messages_processed'] * 100):.2f}%" if STATS[
                                                                                                     "messages_processed"] > 0 else "0%",
        "sessions": len(user_sessions)
    }

    # 删除不需要序列化的字段
    if "response_times" in stats:
        del stats["response_times"]

    return stats


# 在线程池中处理Coze API请求
async def process_coze_stream(bot_id, user_id, conversation_id, additional_messages, token_key='token1'):
    """异步执行Coze API调用，返回完整响应和会话信息"""
    try:
        # 选择对应的coze客户端
        current_coze = coze_clients[token_key]

        # 创建API请求信号量键值
        sem = API_SEMAPHORES[token_key]

        # 获取API请求信号量
        async with sem:
            # 执行API调用
            stream_response = current_coze.chat.stream(
                bot_id=bot_id,
                user_id=user_id,
                conversation_id=conversation_id,
                additional_messages=additional_messages,
            )

            full_response = ""
            new_conversation_id = conversation_id
            bot_message = None

            for event in stream_response:
                if event.event == ChatEventType.CONVERSATION_MESSAGE_DELTA:
                    token = event.message.content
                    # 返回token用于发送
                    yield {
                        "type": "token",
                        "content": token
                    }
                    full_response += token
                elif event.event == ChatEventType.CONVERSATION_CHAT_COMPLETED:
                    new_conversation_id = event.chat.conversation_id
                    bot_message = event.message

            # 返回完整响应和会话信息
            yield {
                "type": "complete",
                "full_response": full_response,
                "conversation_id": new_conversation_id,
                "bot_message": bot_message
            }

    except Exception as e:
        logger.error(f"处理Coze API请求时出错: {str(e)}")
        yield {
            "type": "error",
            "error": str(e)
        }


# 处理generator的异步迭代器包装
async def async_generator_wrapper(gen):
    """将同步生成器包装为异步生成器，以便与asyncio一起使用"""
    # 使用aioitertools提供的iter函数来处理生成器
    async for item in iter(gen):
        yield item


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # 获取用户ID
    user_id = websocket.query_params.get("user_id", f"user_{uuid.uuid4()}")

    # 获取客户端IP
    client_ip = websocket.client.host
    if client_ip in ('127.0.0.1', '::1', 'localhost'):
        # 尝试从X-Forwarded-For或其他代理头部获取真实IP
        headers = websocket.headers
        forwarded_for = headers.get('x-forwarded-for')
        if forwarded_for:
            client_ip = forwarded_for.split(',')[0].strip()

    try:
        # 建立连接
        await manager.connect(user_id, websocket)
        logger.info(f"用户 {user_id} 已连接 | IP: {client_ip} | 当前连接数: {manager.get_active_connections_count()}")

        # 确保用户会话存在
        if user_id not in user_sessions:
            user_sessions[user_id] = {
                "conversation_id": None,
                "history": [],
                "last_activity": time.time(),
                "current_bot": default_bot
            }

        # 更新用户会话的最后活动时间
        user_sessions[user_id]["last_activity"] = time.time()

        # 处理消息
        while True:
            message_data = await websocket.receive_text()

            # 处理心跳
            if message_data == "ping":
                await websocket.send_text("pong")
                continue

            # 尝试解析JSON消息
            try:
                message_json = json.loads(message_data)
                # 检查是否是切换机器人的命令
                if message_json.get('action') == 'switch_bot':
                    bot_id = message_json.get('bot_id')
                    # 查找对应的机器人配置
                    selected_bot = None
                    for bot in bot_configs:
                        if bot['id'] == bot_id:
                            selected_bot = bot
                            break

                    if selected_bot:
                        # 切换机器人并重置会话
                        user_sessions[user_id]["current_bot"] = selected_bot
                        user_sessions[user_id]["conversation_id"] = None
                        user_sessions[user_id]["history"] = []
                        await websocket.send_text(json.dumps({
                            "type": "bot_switched",
                            "bot": selected_bot
                        }))
                        logger.info(f"用户 {user_id} (IP: {client_ip}) 切换到机器人: {selected_bot['name']}")
                    continue

            except json.JSONDecodeError:
                # 不是JSON格式，作为普通文本消息处理
                user_input = message_data
            else:
                # 如果是JSON但不是切换机器人命令，则取text字段
                user_input = message_json.get('text', message_data)

            # 记录用户输入日志(使用用户IP而非ID)
            current_bot = user_sessions[user_id]["current_bot"]
            await log_user_message(client_ip, current_bot['name'], user_input)

            # 更新统计数据
            STATS["messages_processed"] += 1

            # 更新最后活动时间
            user_sessions[user_id]["last_activity"] = time.time()

            # 获取当前机器人配置
            current_bot = user_sessions[user_id]["current_bot"]

            # 尝试从缓存获取响应
            cache_key = f"{user_id}:{current_bot['id']}:{user_input}"
            cached_response = app.state.response_cache.get(cache_key)

            if cached_response:
                logger.info(f"从缓存返回响应: {user_id} | IP: {client_ip}")
                STATS["cache_hits"] += 1
                # 为了模拟流式响应效果，将缓存的响应分成小块发送
                for i in range(0, len(cached_response), 3):
                    chunk = cached_response[i:i + 3]
                    if chunk:
                        await websocket.send_text(chunk)
                        await asyncio.sleep(0.01)  # 添加小延迟，模拟真实打字效果

                # 发送完成消息
                await websocket.send_text("__COMPLETE__")
                continue

            # 构造用户消息
            user_message = Message.build_user_question_text(user_input)

            # 准备会话历史
            session = user_sessions[user_id]
            additional_messages = (session["history"] + [user_message]) if session["conversation_id"] is None else [
                user_message]

            # 更新并发请求计数
            STATS["concurrent_requests"] += 1
            STATS["max_concurrent_requests"] = max(STATS["max_concurrent_requests"], STATS["concurrent_requests"])
            STATS["api_calls"] += 1

            try:
                # 直接调用异步API处理函数
                full_response = ""
                start_time = time.time()

                # 使用动态选择的token_key提高负载均衡能力
                token_key = current_bot['token_key']

                # 调用异步API处理函数
                stream_gen = process_coze_stream(
                    bot_id=current_bot['id'],
                    user_id=user_id,
                    conversation_id=session["conversation_id"],
                    additional_messages=additional_messages,
                    token_key=token_key
                )

                # 实时处理生成器的输出
                async for event in stream_gen:
                    if event["type"] == "token":
                        # 立即发送每个token
                        await websocket.send_text(event["content"])
                        full_response += event["content"]
                    elif event["type"] == "complete":
                        # 发送完成消息，前端会处理光标移除
                        await websocket.send_text("__COMPLETE__")

                        # 处理完成事件
                        session["conversation_id"] = event["conversation_id"]
                        bot_message = event["bot_message"]
                        logger.debug(f"用户 {user_id} (IP: {client_ip}) 会话ID更新: {session['conversation_id']}")

                        # 更新历史记录
                        if session["conversation_id"] is not None and bot_message is not None:
                            # 限制历史记录长度，防止过长
                            if len(session["history"]) > 10:  # 保留最近的5轮对话(10条消息)
                                session["history"] = session["history"][-10:]

                            # 只有在历史记录为空时或上一轮对话的最后一条消息不是当前用户消息时才添加
                            if not session["history"] or session["history"][-1].content != user_message.content:
                                session["history"].append(user_message)

                            # 添加机器人回复
                            if bot_message.content:  # 确保消息有内容
                                session["history"].append(bot_message)

                            logger.debug(
                                f"用户 {user_id} (IP: {client_ip}) 更新历史记录，当前长度: {len(session['history'])}")

                        # 缓存完整响应
                        if full_response:
                            app.state.response_cache.set(cache_key, full_response)
                    elif event["type"] == "error":
                        # 处理错误
                        await websocket.send_text(f"[系统] 服务暂时不可用，请稍后再试: {event['error']}")

                # 记录响应时间
                response_time = time.time() - start_time
                STATS["response_times"].append(response_time)
                # 只保留最近100个响应时间
                if len(STATS["response_times"]) > 100:
                    STATS["response_times"] = STATS["response_times"][-100:]
                logger.debug(f"用户 {user_id} (IP: {client_ip}) 响应时间: {response_time:.2f}秒")

            except Exception as e:
                logger.error(f"处理消息时出错: {str(e)}")
                STATS["errors"] += 1
                await websocket.send_text("[系统] 服务暂时不可用，请稍后再试")
            finally:
                # 减少并发请求计数
                STATS["concurrent_requests"] -= 1

    except WebSocketDisconnect:
        logger.info(f"用户 {user_id} (IP: {client_ip}) 断开连接")
    except Exception as e:
        logger.error(f"WebSocket错误: {str(e)}")
        STATS["errors"] += 1
    finally:
        await manager.disconnect(user_id)


if __name__ == "__main__":
    # 修复workers参数问题 - 改为直接导入app对象而不是传递字符串
    # 确保创建单一进程模式
    workers = min(os.cpu_count() * 2 + 1, 8)  # 根据CPU核心数确定工作进程数，最大8个

    # 如果需要单进程模式(调试用)，使用：
    # uvicorn.run(app, host="0.0.0.0", port=8000)

    # 如果需要多进程模式(生产用)，使用命令行启动：
    # 在终端运行: uvicorn aibot:app --host 0.0.0.0 --port 8000 --workers 4
    # 以下代码无法直接设置workers，仅作为单进程启动
    uvicorn.run(app, host="0.0.0.0", port=521)
    print(f"建议使用命令行启动多进程模式: uvicorn aibot:app --host 0.0.0.0 --port 521 --workers {workers}")